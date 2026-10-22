#!/usr/bin/env node
/**
 * Live end-to-end run of TrustHarbor on GenLayer Studionet.
 *
 *   node run.mjs                 live run on Studionet (needs network access)
 *   node run.mjs --serve         ... and serve the live report on port 8080
 *   node run.mjs --local         the same scenarios on the real GenLayer Python SDK, offline
 *   node run.mjs --only S1,S6    run only some scenarios (S0 setup always runs)
 *
 * Environment: CLAIM_URL / RESPONSE_URL (public evidence pages for the dispute),
 * E2E_NETWORK (studionet | localnet), E2E_ENDPOINT, E2E_FINAL_TIMEOUT (seconds),
 * E2E_PYTHON (python used by --local).
 */
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { Reporter } from './lib/report.mjs';
import { Ctx, SCENARIOS, runScenario } from './lib/scenarios.mjs';
import { errorText } from './lib/util.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTRACTS = path.resolve(HERE, '..', 'contracts');

function parseArgs(argv) {
  const out = { local: false, serve: false, port: 8080, only: null, out: path.join(HERE, 'report') };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--local') out.local = true;
    else if (a === '--serve') {
      out.serve = true;
      if (argv[i + 1] && /^\d+$/.test(argv[i + 1])) out.port = Number(argv[++i]);
    } else if (a === '--only') out.only = new Set(String(argv[++i] || '').toUpperCase().split(',').filter(Boolean));
    else if (a === '--out') out.out = path.resolve(argv[++i]);
    else if (a === '--help' || a === '-h') {
      console.log(fs.readFileSync(fileURLToPath(import.meta.url), 'utf8').split('*/')[0]);
      process.exit(0);
    } else {
      console.error('unknown argument: ' + a);
      process.exit(2);
    }
  }
  return out;
}

function git(command) {
  try {
    return execSync(command, { cwd: HERE, stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
  } catch {
    return '';
  }
}

/** Public URLs of the two evidence pages the validators will fetch. */
function evidenceUrls() {
  if (process.env.CLAIM_URL && process.env.RESPONSE_URL) {
    return { claimUrl: process.env.CLAIM_URL, responseUrl: process.env.RESPONSE_URL, note: 'from CLAIM_URL / RESPONSE_URL' };
  }
  const repo = process.env.GITHUB_REPOSITORY;
  if (repo) {
    const branch = git('git rev-parse --abbrev-ref HEAD') || 'main';
    const prefix = git('git rev-parse --show-prefix') || 'e2e/';
    const base = `https://raw.githubusercontent.com/${repo}/${branch}/${prefix}evidence`;
    return { claimUrl: `${base}/buyer.txt`, responseUrl: `${base}/seller.txt`, note: `raw GitHub files of ${repo}@${branch} (the repository must be public and the files pushed)` };
  }
  return {
    claimUrl: 'https://example.com/',
    responseUrl: 'https://example.org/',
    note: 'no CLAIM_URL / RESPONSE_URL / GITHUB_REPOSITORY set: placeholder pages, the AI ruling will probably be inconclusive',
  };
}

function serve(dir, port) {
  const types = { '.html': 'text/html; charset=utf-8', '.md': 'text/markdown; charset=utf-8', '.json': 'application/json', '.log': 'text/plain; charset=utf-8' };
  const server = http.createServer((req, res) => {
    const name = req.url === '/' || !req.url ? 'index.html' : decodeURIComponent(req.url.split('?')[0]).replace(/^\/+/, '');
    const file = path.join(dir, path.basename(name));
    fs.readFile(file, (error, data) => {
      if (error) {
        res.writeHead(404).end('not found');
        return;
      }
      res.writeHead(200, { 'content-type': types[path.extname(file)] || 'text/plain', 'cache-control': 'no-store' });
      res.end(data);
    });
  });
  server.listen(port, '0.0.0.0');
  return server;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const mode = args.local ? 'local' : 'live';
  const evidence = evidenceUrls();
  const cfg = {
    feeBps: 250,
    penaltyBps: 2000,
    // live transactions take tens of seconds each; the window must outlast the S5 steps that run inside it
    appealWindow: Number(process.env.E2E_APPEAL_WINDOW || (args.local ? 60 : 150)),
    reviewWindow: 900,
    unbondDelay: 60,
    terms: 'Deliver a complete 4k logo pack as PNG and SVG files.',
    claimUrl: args.local ? 'https://local.test/evidence/buyer.txt' : evidence.claimUrl,
    responseUrl: args.local ? 'https://local.test/evidence/seller.txt' : evidence.responseUrl,
    ruleAttempts: 3,
    finalTimeoutS: Number(process.env.E2E_FINAL_TIMEOUT || (args.local ? 5 : 240)),
    pollS: args.local ? 1 : 5,
  };

  const reporter = new Reporter({
    dir: args.out,
    meta: { mode, network: process.env.E2E_NETWORK || (args.local ? 'local simulation' : 'studionet'), endpoint: process.env.E2E_ENDPOINT || '' },
    explorerTxUrl: process.env.EXPLORER_TX_URL || '',
  });
  const server = args.serve ? serve(args.out, args.port) : null;
  if (server) reporter.print(`live report: http://localhost:${args.port}/  (in Codespaces open the forwarded port ${args.port})`);
  reporter.print(`mode ${mode} · reports in ${args.out}`);
  reporter.print(`evidence: ${evidence.note}`);

  let driver;
  if (args.local) {
    const { LocalDriver } = await import('./lib/local-driver.mjs');
    driver = new LocalDriver({
      pages: {
        'local.test/evidence/buyer': fs.readFileSync(path.join(HERE, 'evidence', 'buyer.txt'), 'utf8'),
        'local.test/evidence/seller': fs.readFileSync(path.join(HERE, 'evidence', 'seller.txt'), 'utf8'),
      },
      share: Number(process.env.E2E_LOCAL_SHARE || 80),
    });
  } else {
    const { LiveDriver } = await import('./lib/live-driver.mjs');
    driver = new LiveDriver({
      network: process.env.E2E_NETWORK || 'studionet',
      endpoint: process.env.E2E_ENDPOINT || '',
      contractsDir: CONTRACTS,
    });
  }

  let aborted = false;
  try {
    const info = await driver.init();
    reporter.setMeta({ network: info.network, endpoint: info.endpoint });
    reporter.print(`connected to ${info.network} (${info.endpoint})`);
    const ctx = new Ctx({ driver, reporter, cfg });
    for (const scenario of SCENARIOS) {
      if (args.only && !scenario.required && !args.only.has(scenario.id)) continue;
      await runScenario(ctx, scenario);
    }
  } catch (error) {
    aborted = true;
    reporter.scenario('abort', 'The run could not continue');
    reporter.fail('fatal error', errorText(error));
    if (/fetch failed|ENOTFOUND|ECONN|EAI_AGAIN|403|network/i.test(String(error?.message || error))) {
      reporter.warn('network', 'the RPC endpoint could not be reached from this machine; use --local for an offline run');
    }
  } finally {
    reporter.finish();
    await driver.close?.();
  }

  if (server) {
    reporter.print(`report server still running on port ${args.port}; press Ctrl+C to stop`);
    return;
  }
  process.exit(reporter.failed || aborted ? 1 : 0);
}

main();
