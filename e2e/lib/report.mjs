import fs from 'node:fs';
import path from 'node:path';

const COLOURS = { PASS: '\x1b[32m', FAIL: '\x1b[31m', WARN: '\x1b[33m', SKIP: '\x1b[90m', INFO: '\x1b[36m' };
const RESET = '\x1b[0m';
const ICONS = { PASS: 'PASS', FAIL: 'FAIL', WARN: 'WARN', SKIP: 'SKIP', INFO: 'INFO' };

const escapeHtml = (text) =>
  String(text ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

const escapeMd = (text) => String(text ?? '').replace(/\|/g, '\\|').replace(/\n/g, ' ');

/**
 * A live report. Every recorded step is printed at once and the report files
 * are rewritten straight away, so the Markdown / HTML / JSON on disk always
 * reflect the run so far (the HTML page refreshes itself while the run is
 * still going).
 */
export class Reporter {
  constructor({ dir, meta = {}, explorerTxUrl = '' }) {
    this.dir = dir;
    this.meta = meta;
    this.explorerTxUrl = explorerTxUrl;
    this.scenarios = [];
    this.current = null;
    this.startedAt = new Date();
    this.finishedAt = null;
    this.running = true;
    fs.mkdirSync(dir, { recursive: true });
    this.logPath = path.join(dir, 'live.log');
    fs.writeFileSync(this.logPath, '');
    this.flush();
  }

  // -- recording ------------------------------------------------------- //

  scenario(id, title) {
    this.current = { id, title, steps: [], startedAt: Date.now(), ms: 0 };
    this.scenarios.push(this.current);
    this.print(`\n=== ${id}  ${title}`);
    this.flush();
  }

  step(status, title, detail = '', tx = '') {
    if (!this.current) this.scenario('misc', 'Miscellaneous');
    const entry = { status, title, detail: String(detail ?? ''), tx: tx || '', at: new Date().toISOString() };
    this.current.steps.push(entry);
    this.current.ms = Date.now() - this.current.startedAt;
    const colour = COLOURS[status] || '';
    const suffix = detail ? `  — ${String(detail).slice(0, 220)}` : '';
    this.print(`${colour}[${ICONS[status]}]${RESET} ${title}${suffix}`);
    this.flush();
    return entry;
  }

  pass(title, detail, tx) { return this.step('PASS', title, detail, tx); }
  fail(title, detail, tx) { return this.step('FAIL', title, detail, tx); }
  warn(title, detail, tx) { return this.step('WARN', title, detail, tx); }
  skip(title, detail, tx) { return this.step('SKIP', title, detail, tx); }
  info(title, detail, tx) { return this.step('INFO', title, detail, tx); }

  setMeta(patch) {
    this.meta = { ...this.meta, ...patch };
    this.flush();
  }

  // -- summary --------------------------------------------------------- //

  counts() {
    const counts = { PASS: 0, FAIL: 0, WARN: 0, SKIP: 0, INFO: 0 };
    for (const s of this.scenarios) for (const step of s.steps) counts[step.status] += 1;
    return counts;
  }

  get failed() {
    return this.counts().FAIL > 0;
  }

  finish() {
    this.running = false;
    this.finishedAt = new Date();
    const c = this.counts();
    this.print(`\nRESULT: ${c.PASS} passed, ${c.FAIL} failed, ${c.WARN} warnings, ${c.SKIP} skipped`);
    this.flush();
    const summary = process.env.GITHUB_STEP_SUMMARY;
    if (summary) {
      try {
        fs.appendFileSync(summary, this.markdown() + '\n');
      } catch {
        /* best effort */
      }
    }
  }

  print(line) {
    console.log(line);
    try {
      fs.appendFileSync(this.logPath, line.replace(/\x1b\[[0-9;]*m/g, '') + '\n');
    } catch {
      /* best effort */
    }
  }

  // -- rendering ------------------------------------------------------- //

  txLink(hash) {
    if (!hash) return '';
    if (!this.explorerTxUrl) return hash;
    return this.explorerTxUrl.replace('{hash}', hash);
  }

  markdown() {
    const c = this.counts();
    const lines = [];
    lines.push('# TrustHarbor — end-to-end report');
    lines.push('');
    lines.push(`- **Status:** ${this.running ? 'running…' : c.FAIL ? 'FAILED' : 'PASSED'}`);
    lines.push(`- **Mode:** ${this.meta.mode || ''}  |  **Network:** ${this.meta.network || ''}  |  **RPC:** ${this.meta.endpoint || ''}`);
    lines.push(`- **Started:** ${this.startedAt.toISOString()}${this.finishedAt ? `  |  **Finished:** ${this.finishedAt.toISOString()}` : ''}`);
    lines.push(`- **Totals:** ${c.PASS} passed · ${c.FAIL} failed · ${c.WARN} warnings · ${c.SKIP} skipped`);
    for (const [name, addr] of Object.entries(this.meta.contracts || {})) lines.push(`- **${name}:** \`${addr}\``);
    lines.push('');
    for (const s of this.scenarios) {
      lines.push(`## ${s.id} — ${s.title}`);
      lines.push('');
      lines.push('| | Step | Detail | Tx |');
      lines.push('|---|---|---|---|');
      for (const step of s.steps) {
        lines.push(`| ${ICONS[step.status]} | ${escapeMd(step.title)} | ${escapeMd(step.detail).slice(0, 400)} | ${step.tx ? '`' + step.tx + '`' : ''} |`);
      }
      lines.push('');
    }
    return lines.join('\n');
  }

  html() {
    const c = this.counts();
    const refresh = this.running ? '<meta http-equiv="refresh" content="4">' : '';
    const verdict = this.running ? 'running…' : c.FAIL ? 'FAILED' : 'PASSED';
    const rows = this.scenarios
      .map((s) => {
        const body = s.steps
          .map((step) => {
            const tx = step.tx ? `<code>${escapeHtml(step.tx)}</code>` : '';
            return `<tr class="${step.status}"><td class="st">${step.status}</td><td>${escapeHtml(step.title)}</td><td>${escapeHtml(step.detail)}</td><td>${tx}</td></tr>`;
          })
          .join('\n');
        return `<h2>${escapeHtml(s.id)} — ${escapeHtml(s.title)}</h2><table><thead><tr><th>Result</th><th>Step</th><th>Detail</th><th>Tx</th></tr></thead><tbody>${body}</tbody></table>`;
      })
      .join('\n');
    const contracts = Object.entries(this.meta.contracts || {})
      .map(([name, addr]) => `<li><b>${escapeHtml(name)}</b> <code>${escapeHtml(addr)}</code></li>`)
      .join('');
    return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">${refresh}
<title>TrustHarbor E2E Report</title>
<style>
:root{--bg:#fff;--fg:#1d2330;--mut:#5b6577;--line:#e3e7ee;--pass:#117a3d;--fail:#c4221b;--warn:#a15c00;--skip:#6b7280;--info:#0b6aa6;--card:#f6f8fb}
@media (prefers-color-scheme:dark){:root{--bg:#10141c;--fg:#e6eaf2;--mut:#9aa4b6;--line:#262d3b;--pass:#4cc38a;--fail:#ff7b72;--warn:#e3b341;--skip:#8b95a7;--info:#58a6ff;--card:#171c27}}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto}
h1{margin:0 0 4px}h2{margin:28px 0 8px;font-size:18px}
.meta{color:var(--mut);margin-bottom:12px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;background:var(--card);border:1px solid var(--line)}
.totals span{margin-right:14px;font-weight:600}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:6px 10px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line);word-break:break-word}
th{color:var(--mut);font-weight:600;font-size:13px}
td.st{font-weight:700;width:60px}
tr.PASS td.st{color:var(--pass)}tr.FAIL td.st{color:var(--fail)}tr.WARN td.st{color:var(--warn)}tr.SKIP td.st{color:var(--skip)}tr.INFO td.st{color:var(--info)}
tr.FAIL{background:color-mix(in srgb,var(--fail) 10%,transparent)}
code{font-size:12px}
ul{margin:6px 0 0;padding-left:20px}
</style></head><body><main>
<h1>TrustHarbor — end-to-end report</h1>
<div class="meta">Mode <b>${escapeHtml(this.meta.mode || '')}</b> · Network <b>${escapeHtml(this.meta.network || '')}</b> · ${escapeHtml(this.meta.endpoint || '')}<br>
Started ${escapeHtml(this.startedAt.toISOString())}${this.finishedAt ? ' · Finished ' + escapeHtml(this.finishedAt.toISOString()) : ''}</div>
<p><span class="badge">${verdict}</span></p>
<p class="totals"><span style="color:var(--pass)">${c.PASS} passed</span><span style="color:var(--fail)">${c.FAIL} failed</span><span style="color:var(--warn)">${c.WARN} warnings</span><span style="color:var(--skip)">${c.SKIP} skipped</span></p>
<ul>${contracts}</ul>
${rows}
</main></body></html>`;
  }

  flush() {
    try {
      fs.writeFileSync(path.join(this.dir, 'report.md'), this.markdown());
      fs.writeFileSync(path.join(this.dir, 'index.html'), this.html());
      fs.writeFileSync(
        path.join(this.dir, 'report.json'),
        JSON.stringify(
          {
            meta: this.meta,
            running: this.running,
            startedAt: this.startedAt.toISOString(),
            finishedAt: this.finishedAt ? this.finishedAt.toISOString() : null,
            counts: this.counts(),
            scenarios: this.scenarios,
          },
          null,
          2,
        ),
      );
    } catch {
      /* the console output is the source of truth if the disk is not writable */
    }
  }
}
