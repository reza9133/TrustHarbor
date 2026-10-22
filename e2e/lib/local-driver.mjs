import { spawn } from 'node:child_process';
import readline from 'node:readline';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { lc, normaliseArgs } from './util.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));

const toWire = (value) =>
  JSON.stringify(value, (_key, v) => (typeof v === 'bigint' ? { $big: v.toString() } : v));

const fromWire = (text) =>
  JSON.parse(text, (_key, v) => (v && typeof v === 'object' && '$big' in v ? BigInt(v.$big) : v));

/**
 * Runs the very same scenarios against the real GenLayer Python SDK on this
 * machine (no network, no gas, instant clock): a dry run of the live script.
 * Time-based waits advance a simulated clock instead of sleeping.
 */
export class LocalDriver {
  constructor({ python = process.env.E2E_PYTHON || 'python3', pages = {}, share = 80, confidence = 90 }) {
    this.name = 'local';
    this.python = python;
    this.pages = pages;
    this.share = share;
    this.confidence = confidence;
    this.nextId = 1;
    this.pending = new Map();
  }

  async init() {
    this.child = spawn(this.python, [path.join(HERE, 'local_bridge.py')], { stdio: ['pipe', 'pipe', 'inherit'] });
    this.child.on('exit', (code) => {
      for (const { reject } of this.pending.values()) reject(new Error('local bridge exited with code ' + code));
      this.pending.clear();
    });
    const lines = readline.createInterface({ input: this.child.stdout });
    lines.on('line', (line) => {
      if (!line.startsWith('@@')) return;
      const message = fromWire(line.slice(2));
      const waiter = this.pending.get(message.id);
      if (!waiter) return;
      this.pending.delete(message.id);
      if (message.ok) waiter.resolve(message.result);
      else waiter.reject(new Error(message.error));
    });
    const info = await this.rpc('init', { pages: this.pages, share: this.share, confidence: this.confidence });
    return { network: 'local simulation', endpoint: info.engine };
  }

  rpc(op, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.child.stdin.write(toWire({ id, op, ...params }) + '\n');
    });
  }

  async newAccount(label) {
    const { address } = await this.rpc('account', { label });
    return { address, label };
  }

  async fund(address, wei) {
    await this.rpc('fund', { address, amount: BigInt(wei) });
  }

  async deploy(name, args, sender) {
    const out = await this.rpc('deploy', { name, args: normaliseArgs(args), sender });
    return { address: out.address, hash: out.hash };
  }

  async write(address, fn, args, { sender, value = 0n }) {
    const out = await this.rpc('write', {
      address,
      method: fn,
      args: normaliseArgs(args),
      sender,
      value: BigInt(value),
    });
    return { ok: out.ok, hash: out.hash, error: out.error || '', detail: out.error || 'ok' };
  }

  async read(address, fn, args = []) {
    const out = await this.rpc('read', { address, method: fn, args: normaliseArgs(args) });
    return out.value;
  }

  async balance(address) {
    const out = await this.rpc('balance', { address });
    return BigInt(out.value);
  }

  async sleep(seconds) {
    await this.rpc('advance', { seconds });
  }

  get payoutsAreAsync() {
    return false;
  }

  /** The glsim engine does not reject value sent to non-payable methods. */
  get enforcesNonPayable() {
    return false;
  }

  async close() {
    try {
      this.child.stdin.end();
    } catch {
      /* already gone */
    }
  }
}

export { lc };
