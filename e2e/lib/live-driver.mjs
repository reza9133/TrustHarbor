import fs from 'node:fs';
import path from 'node:path';
import { createClient, createAccount, generatePrivateKey } from 'genlayer-js';
import * as chains from 'genlayer-js/chains';
import { TransactionStatus } from 'genlayer-js/types';
import { errorText, lc, normaliseArgs, sleepMs } from './util.mjs';

const BAD_EXECUTION = new Set(['FINISHED_WITH_ERROR']);
const BAD_STATUS = new Set(['UNDETERMINED', 'CANCELED', 'LEADER_TIMEOUT', 'VALIDATORS_TIMEOUT']);
const BAD_RESULT = new Set(['NO_MAJORITY', 'MAJORITY_DISAGREE', 'DISAGREE', 'TIMEOUT', 'DETERMINISTIC_VIOLATION']);

/** Reads success / failure out of a GenLayer transaction receipt. */
export function judgeReceipt(receipt) {
  const status = receipt?.statusName ?? receipt?.status;
  const exec = receipt?.txExecutionResultName;
  const result = receipt?.resultName;
  const bad = BAD_EXECUTION.has(exec) || BAD_STATUS.has(String(status)) || BAD_RESULT.has(String(result));
  let detail = `status=${status ?? '?'} exec=${exec ?? '?'} consensus=${result ?? '?'}`;
  const leader = receipt?.consensus_data?.leader_receipt?.[0];
  if (bad && leader) {
    const text = leader.stderr || leader.result?.payload?.readable || JSON.stringify(leader.result ?? '');
    if (text) detail += ` · ${String(text).replace(/\s+/g, ' ').slice(0, 200)}`;
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
      hash = await this.client.writeContract({
        account: this.accountOf(sender),
        address,
        functionName: fn,
        args: normaliseArgs(args),
        value: BigInt(value),
        leaderOnly: false,
      });
      const receipt = await this.client.waitForTransactionReceipt({
        hash,
        status: TransactionStatus.ACCEPTED,
        interval: this.intervalMs,
        retries: this.retries,
      });
      const verdict = judgeReceipt(receipt);
      return { ok: verdict.ok, hash, error: verdict.ok ? '' : verdict.detail, detail: verdict.detail };
    } catch (error) {
      return { ok: false, hash, error: errorText(error), detail: errorText(error) };
    }
  }

  async read(address, fn, args = []) {
    return this.client.readContract({ address, functionName: fn, args: normaliseArgs(args) });
  }

  async balance(address) {
    return BigInt(await this.client.getBalance({ address }));
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
