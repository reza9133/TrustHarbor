import fs from 'node:fs';
import path from 'node:path';
import { createClient, createAccount, generatePrivateKey } from 'genlayer-js';
import * as chains from 'genlayer-js/chains';
import { TransactionStatus } from 'genlayer-js/types';
import { errorText, lc, normaliseArgs, sleepMs } from './util.mjs';

const BAD_EXECUTION = new Set(['FINISHED_WITH_ERROR', 'ERROR']);
const BAD_STATUS = new Set(['UNDETERMINED', 'CANCELED', 'LEADER_TIMEOUT', 'VALIDATORS_TIMEOUT']);
const BAD_RESULT = new Set(['NO_MAJORITY', 'MAJORITY_DISAGREE', 'DISAGREE', 'TIMEOUT', 'DETERMINISTIC_VIOLATION']);
// numeric forms (Studio can return the enum numbers instead of names)
const BAD_RESULT_NUM = new Set(['2', '3', '4', '5', '7']);
// GenVM result kinds that mean "the call did not complete normally" (revert / rollback / crash)
const BAD_GENVM_STATUS = new Set(['rollback', 'contract_error', 'error', 'no_leaders']);

// Network hiccups (gateway returns an HTML error page instead of JSON, 429/5xx, resets, timeouts).
// These say nothing about the contract and must never be mistaken for a refusal.
const TRANSIENT = /<!DOCTYPE|Unexpected token '<'|is not valid JSON|unknown RPC error|\b(429|500|502|503|504)\b|bad gateway|service unavailable|gateway time-?out|too many requests|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|fetch failed|socket hang up|network error/i;
// RPC methods that run BEFORE a transaction is sent: failing there means nothing was submitted, so a retry is safe.
const PRE_SEND = /eth_(gasPrice|chainId|getBalance|getTransactionCount|estimateGas|blockNumber|call)|gen_(call|getContractSchema)/;
export const isTransient = (text) => TRANSIENT.test(String(text || ''));

async function retry(fn, { tries = 5, baseMs = 2000, canRetry = isTransient } = {}) {
  let last;
  for (let i = 0; i < tries; i++) {
    try {
      return await fn();
    } catch (error) {
      last = error;
      if (i === tries - 1 || !canRetry(errorText(error))) break;
      await sleepMs(baseMs * (i + 1));
    }
  }
  throw last;
}

/**
 * Reads success / failure out of a GenLayer transaction receipt.
 *
 * Important: on Studionet a reverted call is still a transaction that reaches
 * ACCEPTED / FINALIZED. genlayer-js does NOT fill txExecutionResultName or
 * resultName for Studio receipts, so the failure is only visible in
 * consensus_data.leader_receipt[0] (execution_result === "ERROR" and
 * result.status === "rollback" | "contract_error" | "error").
 */
export function judgeReceipt(receipt) {
  const status = receipt?.statusName ?? receipt?.status_name ?? receipt?.status;
  const exec =
    receipt?.txExecutionResultName ??
    (receipt?.txExecutionResult === 2 || receipt?.tx_execution_result === 2 ? 'FINISHED_WITH_ERROR' : undefined);
  const result = receipt?.resultName ?? receipt?.result_name;
  const resultNum = receipt?.result !== undefined && typeof receipt.result !== 'object' ? String(receipt.result) : '';

  const leaders = receipt?.consensus_data?.leader_receipt;
  const leader = Array.isArray(leaders) ? leaders[0] : leaders;
  const leaderExec = leader?.execution_result ?? leader?.executionResult;
  const leaderKind = leader?.result?.status;

  const reasons = [];
  if (BAD_EXECUTION.has(exec)) reasons.push(`exec=${exec}`);
  if (BAD_STATUS.has(String(status))) reasons.push(`status=${status}`);
  if (BAD_RESULT.has(String(result)) || (!result && BAD_RESULT_NUM.has(resultNum))) reasons.push(`consensus=${result ?? resultNum}`);
  if (BAD_EXECUTION.has(String(leaderExec))) reasons.push(`leader=${leaderExec}`);
  if (BAD_GENVM_STATUS.has(String(leaderKind))) reasons.push(`result=${leaderKind}`);
  const bad = reasons.length > 0;

  let detail = `status=${status ?? '?'} exec=${exec ?? leaderExec ?? '?'} consensus=${result ?? (resultNum || '?')}`;
  if (leaderKind) detail += ` result=${leaderKind}`;
  if (bad && leader) {
    const payload = leader.result?.payload;
    const text =
      (typeof payload === 'string' ? payload : payload?.readable) ||
      leader.genvm_result?.stderr ||
      leader.stderr ||
      JSON.stringify(leader.result ?? '');
    if (text) detail += ` · ${String(text).replace(/\s+/g, ' ').slice(0, 200)}`;
  }
  if (process.env.E2E_DEBUG_RECEIPT) {
    console.error(`[receipt ${bad ? 'BAD' : 'ok'}]`, JSON.stringify(receipt, (_k, v) => (typeof v === 'bigint' ? v.toString() : v)).slice(0, 2000));
  }
  return { ok: !bad, detail };
}

/**
 * Talks to a real GenLayer network (Studionet by default) through genlayer-js.
 * Accounts are throw-away keys generated for this run and funded through the
 * Studio's sim_fundAccount method.
 */
export class LiveDriver {
  constructor({ network = 'studionet', endpoint = '', contractsDir, intervalMs = 3000, retries = 200 }) {
    this.name = 'live';
    this.networkName = network;
    this.endpoint = endpoint;
    this.contractsDir = contractsDir;
    this.intervalMs = intervalMs;
    this.retries = retries;
    this.accounts = new Map();
  }

  async init() {
    const chain = chains[this.networkName];
    if (!chain) throw new Error(`unknown network "${this.networkName}" (try studionet or localnet)`);
    this.chain = chain;
    this.endpoint = this.endpoint || chain.rpcUrls?.default?.http?.[0] || '';
    this.client = createClient(this.endpoint ? { chain, endpoint: this.endpoint } : { chain });
    // fail early and clearly if the RPC is unreachable
    const chainId = await this.client.getChainId();
    return { network: `${this.networkName} (chain id ${chainId})`, endpoint: this.endpoint };
  }

  async newAccount(label) {
    const account = createAccount(generatePrivateKey());
    this.accounts.set(lc(account.address), account);
    return { address: account.address, label };
  }

  async fund(address, wei) {
    // sim_fundAccount takes a JSON number; the amounts used here (k * 10^n) are exact in a double
    await this.client.request({ method: 'sim_fundAccount', params: [address, Number(wei)] });
  }

  accountOf(address) {
    const account = this.accounts.get(lc(address));
    if (!account) throw new Error('no key for ' + address);
    return account;
  }

  async deploy(name, args, sender) {
    const code = fs.readFileSync(path.join(this.contractsDir, name + '.py'), 'utf8');
    const hash = await this.client.deployContract({
      account: this.accountOf(sender),
      code,
      args: normaliseArgs(args),
      leaderOnly: false,
    });
    const receipt = await this.client.waitForTransactionReceipt({
      hash,
      status: TransactionStatus.ACCEPTED,
      interval: this.intervalMs,
      retries: this.retries,
    });
    const verdict = judgeReceipt(receipt);
    if (!verdict.ok) throw new Error(`deploying ${name} failed: ${verdict.detail}`);
    const address = receipt?.data?.contract_address ?? receipt?.recipient ?? receipt?.to_address;
    if (!address) throw new Error(`deploying ${name}: no contract address in the receipt`);
    return { address, hash };
  }

  async write(address, fn, args, { sender, value = 0n }) {
    let hash = '';
    try {
      // retry only failures that happen before anything is sent (no duplicate transactions)
      hash = await retry(
        () =>
          this.client.writeContract({
            account: this.accountOf(sender),
            address,
            functionName: fn,
            args: normaliseArgs(args),
            value: BigInt(value),
            leaderOnly: false,
          }),
        { canRetry: (t) => isTransient(t) && PRE_SEND.test(t) },
      );
      // waiting for the receipt is read-only: safe to retry on network errors
      const receipt = await retry(() =>
        this.client.waitForTransactionReceipt({
          hash,
          status: TransactionStatus.ACCEPTED,
          interval: this.intervalMs,
          retries: this.retries,
        }),
      );
      const verdict = judgeReceipt(receipt);
      return { ok: verdict.ok, hash, error: verdict.ok ? '' : verdict.detail, detail: verdict.detail };
    } catch (error) {
      const text = errorText(error);
      // infra: the outcome is unknown (network failure), NOT a contract refusal
      return { ok: false, infra: isTransient(text), hash, error: text, detail: text };
    }
  }

  async read(address, fn, args = []) {
    return retry(() => this.client.readContract({ address, functionName: fn, args: normaliseArgs(args) }));
  }

  async balance(address) {
    return BigInt(await retry(() => this.client.getBalance({ address })));
  }

  /** Real time passes on a real network. */
  async sleep(seconds) {
    await sleepMs(seconds * 1000);
  }

  /** Payouts are emitted as finalized transfers: on a real network we have to wait for them. */
  get payoutsAreAsync() {
    return true;
  }

  async close() {}
}
