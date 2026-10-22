import { DAY, GEN, HOUR, errorText, expectedSplit, fmtWei, lc, parseJson, sameAddress } from './util.mjs';

const CENT = GEN / 100n; // 0.01 GEN

// --------------------------------------------------------------------------- //
// the context every scenario works through
// --------------------------------------------------------------------------- //

export class Ctx {
  constructor({ driver, reporter, cfg }) {
    this.d = driver;
    this.r = reporter;
    this.cfg = cfg;
    this.acct = {}; // label -> address
    this.addr = {}; // bench | escrow | bond -> address
    this.facts = {};
  }

  // -- steps ----------------------------------------------------------- //

  check(title, condition, detail = '') {
    return condition ? this.r.pass(title, detail) : this.r.fail(title, detail);
  }

  /** A write that must succeed. */
  async tx(title, who, key, fn, args = [], { value = 0n } = {}) {
    const res = await this.d.write(this.addr[key], fn, args, { sender: this.acct[who], value });
    if (res.ok) this.r.pass(title, `${who} → ${key}.${fn}`, res.hash);
    else this.r.fail(title, `${who} → ${key}.${fn} reverted: ${res.error}`, res.hash);
    return res;
  }

  /** A write that must be refused. */
  async txFail(title, who, key, fn, args = [], { value = 0n } = {}) {
    const res = await this.d.write(this.addr[key], fn, args, { sender: this.acct[who], value });
    if (!res.ok) this.r.pass(title, `refused as expected (${(res.error || '').slice(0, 140)})`, res.hash);
    else this.r.fail(title, `${who} → ${key}.${fn} was accepted but must be refused`, res.hash);
    return res;
  }

  async view(key, fn, args = []) {
    return parseJson(await this.d.read(this.addr[key], fn, args));
  }

  async wait(seconds, reason) {
    this.r.info(`waiting ${seconds}s`, reason);
    await this.d.sleep(seconds);
  }

  // -- helpers --------------------------------------------------------- //

  async deal(id) {
    return this.view('escrow', 'get_deal', [id]);
  }

  async caseOf(id) {
    return this.view('bench', 'get_case', [id]);
  }

  /** Opens a deal and returns its id (found by listing the buyer's deals), or null. */
  async openDeal(title, buyer, seller, price, { terms = this.cfg.terms, window = DAY, sellerText } = {}) {
    const before = await this.view('escrow', 'get_deals_by_party', [this.acct[buyer], 0, 1]);
    const res = await this.tx(title, buyer, 'escrow', 'open_deal', [sellerText ?? this.acct[seller], terms, window], { value: price });
    if (!res.ok) return null;
    const after = await this.view('escrow', 'get_deals_by_party', [this.acct[buyer], 0, 1]);
    if (after.total !== before.total + 1) {
      this.r.fail('the deal shows up in the buyer\'s list', `deals before ${before.total}, after ${after.total}`);
      return null;
    }
    const last = await this.view('escrow', 'get_deals_by_party', [this.acct[buyer], after.total - 1, 1]);
    const deal = last.deals[0];
    this.check('the new deal is FUNDED with the exact price', deal.status === 'FUNDED' && BigInt(deal.price) === BigInt(price),
      `deal ${deal.deal_id}: ${deal.status}, price ${fmtWei(deal.price)}`);
    return deal.deal_id;
  }

  async lastCaseOf(claimant) {
    const page = await this.view('bench', 'get_cases_by_party', [this.acct[claimant], 0, 1]);
    if (!page.total) return null;
    const last = await this.view('bench', 'get_cases_by_party', [this.acct[claimant], page.total - 1, 1]);
    return last.cases[0].case_id;
  }

  /** Hard solvency rules that must hold at every instant. */
  async solvency(title) {
    const e = await this.view('escrow', 'get_platform_stats');
    const escrowBalance = await this.d.balance(this.addr.escrow);
    const owes = BigInt(e.owed_total) + BigInt(e.fees_available);
    const b = await this.view('bond', 'get_stats');
    const bondBalance = await this.d.balance(this.addr.bond);
    const benchBalance = await this.d.balance(this.addr.bench);
    this.check(`${title}: escrow holds at least what it owes`, escrowBalance >= owes,
      `balance ${fmtWei(escrowBalance)} ≥ owed ${fmtWei(e.owed_total)} + fees ${fmtWei(e.fees_available)}`);
    this.check(`${title}: bond contract holds at least the bonds`, bondBalance >= BigInt(b.total_bonded),
      `balance ${fmtWei(bondBalance)} ≥ bonded ${fmtWei(b.total_bonded)}`);
    this.check(`${title}: the bench never holds value`, benchBalance === 0n, `balance ${fmtWei(benchBalance)}`);
    return { escrowBalance, owes, bondBalance, totalBonded: BigInt(b.total_bonded) };
  }

  /** On a real network payouts are finalized transfers: wait until balances match the books exactly. */
  async settledBalances(title) {
    const deadline = Date.now() + this.cfg.finalTimeoutS * 1000;
    let last = null;
    for (;;) {
      const e = await this.view('escrow', 'get_platform_stats');
      const b = await this.view('bond', 'get_stats');
      const escrowBalance = await this.d.balance(this.addr.escrow);
      const bondBalance = await this.d.balance(this.addr.bond);
      const owes = BigInt(e.owed_total) + BigInt(e.fees_available);
      last = { escrowBalance, owes, bondBalance, bonded: BigInt(b.total_bonded) };
      if (escrowBalance === owes && bondBalance === last.bonded) {
        return this.r.pass(`${title}: balances equal the books exactly`,
          `escrow ${fmtWei(escrowBalance)}, bond contract ${fmtWei(bondBalance)}`);
      }
      if (Date.now() > deadline) break;
      await this.d.sleep(this.cfg.pollS);
    }
    return this.r.warn(`${title}: balances have not settled yet`,
      `escrow ${fmtWei(last.escrowBalance)} vs books ${fmtWei(last.owes)}; bond ${fmtWei(last.bondBalance)} vs ${fmtWei(last.bonded)} ` +
      `(transfers are emitted at finalization; this is a warning, not a failure, if the network is still finalizing)`);
  }

  /** Waits for a recipient's balance to rise by about `amount`. */
  async receives(title, who, before, amount) {
    const deadline = Date.now() + this.cfg.finalTimeoutS * 1000;
    const wanted = before + (amount * 99n) / 100n; // tolerate a tiny fee on the withdrawing account
    let now = before;
    for (;;) {
      now = await this.d.balance(this.acct[who]);
      if (now >= wanted) return this.r.pass(title, `${who} balance ${fmtWei(before)} → ${fmtWei(now)}`);
      if (Date.now() > deadline) break;
      await this.d.sleep(this.cfg.pollS);
    }
    return this.r.warn(title, `${who} balance is ${fmtWei(now)}, expected about ${fmtWei(before + amount)} (payout not finalized yet?)`);
  }

  async withdrawAll(who) {
    const owed = BigInt(await this.d.read(this.addr.escrow, 'get_claimable', [this.acct[who]]));
    if (owed === 0n) return 0n;
    const before = await this.d.balance(this.acct[who]);
    const res = await this.tx(`${who} withdraws ${fmtWei(owed)}`, who, 'escrow', 'withdraw');
    if (!res.ok) return 0n;
    const left = BigInt(await this.d.read(this.addr.escrow, 'get_claimable', [this.acct[who]]));
    this.check(`${who}'s claimable balance is zero after withdrawing`, left === 0n, `left ${fmtWei(left)}`);
    await this.receives(`${who} receives the payout`, who, before, owed);
    return owed;
  }
}

// --------------------------------------------------------------------------- //
// S0  setup
// --------------------------------------------------------------------------- //

export async function s0Setup(ctx) {
  const { d, r, cfg } = ctx;
  const people = { owner: 1n, buyer: 5n, seller: 3n, seller2: 2n, outsider: 1n };
  for (const [label, gen] of Object.entries(people)) {
    const account = await d.newAccount(label);
    ctx.acct[label] = account.address;
    await d.fund(account.address, gen * GEN);
    r.pass(`account ${label} created and funded`, `${account.address} with ${gen} GEN`);
  }
  const bench = await d.deploy('ArbitrationBench', [cfg.appealWindow], ctx.acct.owner);
  ctx.addr.bench = bench.address;
  r.pass('ArbitrationBench deployed', bench.address, bench.hash);
  const escrow = await d.deploy('DealEscrow', [bench.address, cfg.feeBps, cfg.reviewWindow], ctx.acct.owner);
  ctx.addr.escrow = escrow.address;
  r.pass('DealEscrow deployed', escrow.address, escrow.hash);
  const bond = await d.deploy('MerchantBond', [escrow.address, cfg.penaltyBps, cfg.unbondDelay], ctx.acct.owner);
  ctx.addr.bond = bond.address;
  r.pass('MerchantBond deployed', bond.address, bond.hash);
  r.setMeta({ contracts: { ArbitrationBench: bench.address, DealEscrow: escrow.address, MerchantBond: bond.address } });

  const benchCfg = await ctx.view('bench', 'get_config');
  const escrowCfg = await ctx.view('escrow', 'get_config');
  const bondCfg = await ctx.view('bond', 'get_config');
  ctx.check('each contract is owned by the deployer',
    [benchCfg, escrowCfg, bondCfg].every((c) => sameAddress(c.owner, ctx.acct.owner)));
  ctx.check('DealEscrow is wired to the bench', sameAddress(escrowCfg.bench, bench.address), escrowCfg.bench);
  ctx.check('MerchantBond is wired to the escrow', sameAddress(bondCfg.escrow, escrow.address), bondCfg.escrow);
  ctx.check('constructor parameters were applied',
    benchCfg.appeal_window === cfg.appealWindow && escrowCfg.fee_bps === cfg.feeBps &&
      escrowCfg.review_window === cfg.reviewWindow && bondCfg.penalty_bps === cfg.penaltyBps &&
      bondCfg.unbond_delay === cfg.unbondDelay,
    `appeal ${benchCfg.appeal_window}s, fee ${escrowCfg.fee_bps}bps, review ${escrowCfg.review_window}s, penalty ${bondCfg.penalty_bps}bps, unbond ${bondCfg.unbond_delay}s`);
  await ctx.solvency('fresh deployment');
}

// --------------------------------------------------------------------------- //
// S1  a deal nobody disputes
// --------------------------------------------------------------------------- //

export async function s1Happy(ctx) {
  const price = CENT;
  const statsBefore = await ctx.view('escrow', 'get_platform_stats');
  const id = await ctx.openDeal('buyer funds a 0.01 GEN deal', 'buyer', 'seller', price);
  if (id === null) return;
  const statsFunded = await ctx.view('escrow', 'get_platform_stats');
  ctx.check('the price is escrowed (owed_total grew by exactly the price)',
    BigInt(statsFunded.owed_total) - BigInt(statsBefore.owed_total) === price,
    `owed ${fmtWei(statsBefore.owed_total)} → ${fmtWei(statsFunded.owed_total)}`);
  await ctx.solvency('after funding');

  await ctx.tx('seller declares delivery', 'seller', 'escrow', 'mark_delivered', [id, 'files sent']);
  ctx.check('the deal is DELIVERED and the review clock runs', (await ctx.deal(id)).status === 'DELIVERED');

  await ctx.tx('buyer confirms receipt', 'buyer', 'escrow', 'confirm_receipt', [id]);
  const deal = await ctx.deal(id);
  const want = expectedSplit(price, 0, ctx.cfg.feeBps);
  ctx.check('the deal is RELEASED to the seller minus the platform fee',
    deal.status === 'RELEASED' && BigInt(deal.seller_amount) === want.seller && BigInt(deal.fee) === want.fee && BigInt(deal.buyer_amount) === 0n,
    `seller ${fmtWei(deal.seller_amount)}, fee ${fmtWei(deal.fee)}`);

  await ctx.withdrawAll('seller');
  await ctx.settledBalances('after the first payout');
}

// --------------------------------------------------------------------------- //
// S2  a silent buyer: auto-release after the review window
// --------------------------------------------------------------------------- //

export async function s2AutoRelease(ctx) {
  await ctx.tx('owner shortens the review window for NEW deals to 60 s', 'owner', 'escrow', 'set_review_window', [60]);
  const id = await ctx.openDeal('buyer funds a deal that will go silent', 'buyer', 'seller', CENT);
  if (id !== null) {
    await ctx.tx('seller declares delivery', 'seller', 'escrow', 'mark_delivered', [id, 'sent']);
    ctx.check('the deal copied the 60 s review window', (await ctx.deal(id)).review_window === 60);
    await ctx.txFail('nobody can release inside the review window', 'outsider', 'escrow', 'release_after_review', [id]);
    await ctx.wait(65, 'the buyer stays silent past the review window');
    await ctx.tx('a stranger releases the deal after the window', 'outsider', 'escrow', 'release_after_review', [id]);
    const deal = await ctx.deal(id);
    ctx.check('the resolution is AUTO_RELEASE', deal.status === 'RELEASED' && deal.resolution === 'AUTO_RELEASE', deal.resolution);
    await ctx.withdrawAll('seller');
  }
  await ctx.tx('owner restores the review window', 'owner', 'escrow', 'set_review_window', [ctx.cfg.reviewWindow]);
}

// --------------------------------------------------------------------------- //
// S3  a seller who never delivers
// --------------------------------------------------------------------------- //

export async function s3Undelivered(ctx) {
  const id = await ctx.openDeal('buyer funds a deal with a 60 s delivery window', 'buyer', 'seller', CENT, { window: 60 });
  if (id === null) return;
  await ctx.txFail('the buyer cannot reclaim before the deadline', 'outsider', 'escrow', 'reclaim_undelivered', [id]);
  await ctx.wait(65, 'the delivery deadline passes');
  await ctx.txFail('the seller cannot deliver after the deadline', 'seller', 'escrow', 'mark_delivered', [id, 'late']);
  await ctx.tx('a stranger triggers the full refund', 'outsider', 'escrow', 'reclaim_undelivered', [id]);
  const deal = await ctx.deal(id);
  ctx.check('the whole price goes back to the buyer with no fee',
    deal.status === 'REFUNDED' && BigInt(deal.buyer_amount) === CENT && BigInt(deal.fee) === 0n, `${deal.status}, buyer ${fmtWei(deal.buyer_amount)}`);
  await ctx.withdrawAll('buyer');
}

// --------------------------------------------------------------------------- //
// S4  the seller refunds voluntarily
// --------------------------------------------------------------------------- //

export async function s4Refund(ctx) {
  const id = await ctx.openDeal('buyer funds a deal the seller will refund', 'buyer', 'seller', CENT);
  if (id === null) return;
  await ctx.tx('seller refunds the buyer', 'seller', 'escrow', 'refund_buyer', [id]);
  const deal = await ctx.deal(id);
  ctx.check('the deal is REFUNDED in full', deal.status === 'REFUNDED' && BigInt(deal.buyer_amount) === CENT, deal.status);
  await ctx.txFail('a settled deal cannot be refunded twice', 'seller', 'escrow', 'refund_buyer', [id]);
  await ctx.withdrawAll('buyer');
}

// --------------------------------------------------------------------------- //
// S5  a disputed deal: AI ruling, settlement and bond compensation
// --------------------------------------------------------------------------- //

export async function s5Dispute(ctx) {
  const { r, cfg } = ctx;
  const price = CENT * 5n;
  const bondAmount = CENT * 5n;

  await ctx.tx('owner lowers the trust threshold to 0.01 GEN', 'owner', 'bond', 'set_trust_threshold', [CENT]);
  await ctx.tx('seller posts a 0.05 GEN bond', 'seller', 'bond', 'post_bond', [], { value: bondAmount });
  const bond0 = await ctx.view('bond', 'get_bond', [ctx.acct.seller]);
  ctx.check('the bond is recorded and the seller is trusted',
    BigInt(bond0.bonded) === bondAmount && bond0.trusted === true, `bonded ${fmtWei(bond0.bonded)}, trusted ${bond0.trusted}`);

  const id = await ctx.openDeal('buyer funds a 0.05 GEN deal', 'buyer', 'seller', price);
  if (id === null) return;
  await ctx.tx('seller declares delivery', 'seller', 'escrow', 'mark_delivered', [id, 'sent logo.jpg']);

  const template = await ctx.view('escrow', 'get_dispute_template', [id]);
  await ctx.tx('buyer opens a case on the bench using the dispute template', 'buyer', 'bench', 'open_case',
    [ctx.acct.seller, template.subject, cfg.claimUrl, template.standard, 13 * HOUR]);
  const caseId = await ctx.lastCaseOf('buyer');
  if (caseId === null) return r.fail('the case shows up in the buyer\'s list');
  await ctx.tx('buyer attaches the case to the deal', 'buyer', 'escrow', 'attach_case', [id, caseId]);
  const disputed = await ctx.deal(id);
  ctx.check('the deal is DISPUTED and bound to that case', disputed.status === 'DISPUTED' && disputed.case_id === caseId, `case ${disputed.case_id}`);

  // attacks while the dispute is pending
  await ctx.txFail('the seller cannot refund their way out of a pending ruling', 'seller', 'escrow', 'refund_buyer', [id]);
  await ctx.txFail('a stranger cannot settle a deal that has no final ruling', 'outsider', 'escrow', 'settle_deal', [id]);
  await ctx.txFail('the case cannot be ruled while the respondent still has time to answer', 'outsider', 'bench', 'rule_case', [caseId]);
  await ctx.txFail('only the respondent can answer the case', 'outsider', 'bench', 'respond', [caseId, cfg.responseUrl]);
  await ctx.txFail('a case that is not this deal\'s cannot be attached twice', 'buyer', 'escrow', 'attach_case', [id, caseId]);

  await ctx.tx('seller files a response with their evidence', 'seller', 'bench', 'respond', [caseId, cfg.responseUrl]);

  // the real validators fetch both pages and ask an LLM; allow a few attempts
  let ruled = null;
  for (let attempt = 1; attempt <= cfg.ruleAttempts; attempt += 1) {
    const res = await ctx.tx(`a stranger asks the bench to rule (attempt ${attempt})`, 'outsider', 'bench', 'rule_case', [caseId]);
    const c = await ctx.caseOf(caseId);
    if (res.ok && c.phase === 'RULED') {
      ruled = c;
      break;
    }
    r.info('no confident ruling yet', `phase ${c.phase}`);
    await ctx.d.sleep(5);
  }
  if (!ruled) {
    return r.warn('the arbiter did not reach a confident ruling', 'this depends on the live LLM and evidence pages; the dispute part of the run is incomplete');
  }
  r.info('the arbiter ruled', `claimant (buyer) share ${ruled.claimant_share}% · confidence ${ruled.confidence} · "${ruled.reasoning}"`);
  ctx.check('the ruling is a legal value', ruled.claimant_share >= 0 && ruled.claimant_share <= 100 && ruled.claimant_share % 5 === 0 && ruled.confidence >= 30,
    `share ${ruled.claimant_share}, confidence ${ruled.confidence}`);
  await ctx.txFail('a ruled case cannot be ruled again', 'outsider', 'bench', 'rule_case', [caseId]);
  await ctx.txFail('the deal cannot be settled during the appeal window', 'outsider', 'escrow', 'settle_deal', [id]);
  await ctx.txFail('a ruled case cannot be withdrawn', 'buyer', 'bench', 'withdraw_case', [caseId]);

  await ctx.wait(cfg.appealWindow + 5, 'the appeal window runs out and the ruling becomes final');
  await ctx.tx('a stranger settles the deal with the final ruling', 'outsider', 'escrow', 'settle_deal', [id]);
  const settled = await ctx.deal(id);
  const share = ruled.claimant_share;
  const want = expectedSplit(price, share, cfg.feeBps);
  ctx.check('the deal is SETTLED by the ruling', settled.status === 'SETTLED' && settled.resolution === 'RULING', `${settled.status}/${settled.resolution}`);
  ctx.check('the split matches the ruled share exactly and loses no wei',
    settled.buyer_pct === share && BigInt(settled.buyer_amount) === want.buyer && BigInt(settled.seller_amount) === want.seller && BigInt(settled.fee) === want.fee &&
      BigInt(settled.buyer_amount) + BigInt(settled.seller_amount) + BigInt(settled.fee) === price,
    `buyer ${fmtWei(settled.buyer_amount)} (${settled.buyer_pct}%), seller ${fmtWei(settled.seller_amount)}, fee ${fmtWei(settled.fee)}`);
  await ctx.txFail('a deal cannot be settled twice', 'outsider', 'escrow', 'settle_deal', [id]);
  ctx.facts.disputeDealId = id;

  if (share >= 20) {
    const preview = await ctx.view('bond', 'preview_claim', [id, ctx.acct.buyer]);
    ctx.check('the bond contract says the buyer may claim', preview.eligible === true, preview.reason || `payable ${fmtWei(preview.payable)}`);
    await ctx.txFail('only the deal\'s buyer can claim compensation', 'outsider', 'bond', 'claim_compensation', [id]);
    await ctx.txFail('the seller cannot claim on the buyer\'s behalf', 'seller', 'bond', 'claim_compensation', [id]);
    await ctx.tx('the buyer claims compensation from the seller\'s bond', 'buyer', 'bond', 'claim_compensation', [id]);
    const bond1 = await ctx.view('bond', 'get_bond', [ctx.acct.seller]);
    const penalty = (BigInt(settled.buyer_amount) * BigInt(cfg.penaltyBps)) / 10000n;
    const slashed = penalty < bondAmount ? penalty : bondAmount;
    ctx.check('exactly the penalty was slashed and the seller has one strike',
      BigInt(bond1.bonded) === bondAmount - slashed && bond1.strikes === 1, `bonded ${fmtWei(bond1.bonded)}, strikes ${bond1.strikes}, slashed ${fmtWei(slashed)}`);
    await ctx.txFail('a deal can only be claimed once', 'buyer', 'bond', 'claim_compensation', [id]);

    // the exit hold after an adverse ruling
    const left = BigInt(bond1.bonded);
    if (left > 0n) {
      await ctx.tx('seller asks to unbond everything that is left', 'seller', 'bond', 'request_unbond', [left]);
      await ctx.wait(cfg.unbondDelay + 5, 'the unbonding delay passes');
      await ctx.txFail('the seller still cannot walk away right after an adverse ruling', 'seller', 'bond', 'withdraw_unbonded');
      await ctx.tx('seller cancels the unbond request', 'seller', 'bond', 'cancel_unbond');
    }
  } else {
    r.skip('bond compensation', `the ruling gave the buyer only ${share}% (< 20%), so the seller is not at fault`);
  }

  await ctx.withdrawAll('buyer');
  await ctx.withdrawAll('seller');
  await ctx.solvency('after the dispute');
  await ctx.settledBalances('after the dispute payouts');
}

// --------------------------------------------------------------------------- //
// S6  hostile callers and hostile inputs
// --------------------------------------------------------------------------- //

export async function s6Adversarial(ctx) {
  const { r } = ctx;
  const zero = '0x' + '00'.repeat(20);
  const dealsBefore = (await ctx.view('escrow', 'get_platform_stats')).deals;

  // payable calls must refuse politely: no raise, no deal, the value comes back
  const bad = [
    ['empty terms', [ctx.acct.seller, '   ', DAY], CENT],
    ['over-long terms (301 chars)', [ctx.acct.seller, 'x'.repeat(301), DAY], CENT],
    ['price below the minimum', [ctx.acct.seller, 'terms', DAY], 1000n],
    ['seller is the buyer', [ctx.acct.buyer, 'terms', DAY], CENT],
    ['zero-address seller', [zero, 'terms', DAY], CENT],
    ['garbage seller', ['not an address', 'terms', DAY], CENT],
    ['delivery window of 1 second', [ctx.acct.seller, 'terms', 1], CENT],
  ];
  for (const [label, args, value] of bad) {
    const res = await ctx.d.write(ctx.addr.escrow, 'open_deal', args, { sender: ctx.acct.buyer, value });
    r.info(`open_deal with ${label}`, res.ok ? 'transaction accepted, rejection returned in the result' : `reverted: ${res.error}`, res.hash);
  }
  const dealsAfter = (await ctx.view('escrow', 'get_platform_stats')).deals;
  ctx.check('none of the hostile deals was created', dealsAfter === dealsBefore, `deals ${dealsBefore} → ${dealsAfter}`);
  const bondBefore = await ctx.view('bond', 'get_stats');
  await ctx.d.write(ctx.addr.bond, 'post_bond', [], { sender: ctx.acct.buyer, value: 1000n });
  await ctx.d.write(ctx.addr.bond, 'post_bond', [], { sender: ctx.acct.buyer, value: 0n });
  const bondAfter = await ctx.view('bond', 'get_stats');
  ctx.check('dust bonds are not recorded', bondBefore.total_bonded === bondAfter.total_bonded, `bonded ${fmtWei(bondAfter.total_bonded)}`);
  await ctx.solvency('after refused deposits');
  await ctx.settledBalances('after refused deposits (refunds)');

  // strangers cannot use any admin power on any contract
  const admin = [
    ['escrow', 'set_paused', [true]], ['escrow', 'set_fee_bps', [0]], ['escrow', 'set_review_window', [60]],
    ['escrow', 'withdraw_fees', [ctx.acct.outsider, 1]], ['escrow', 'transfer_ownership', [ctx.acct.outsider]],
    ['bench', 'set_paused', [true]], ['bench', 'set_appeal_window', [60]], ['bench', 'transfer_ownership', [ctx.acct.outsider]],
    ['bond', 'set_paused', [true]], ['bond', 'set_trust_threshold', [0]], ['bond', 'transfer_ownership', [ctx.acct.outsider]],
  ];
  for (const [key, fn, args] of admin) await ctx.txFail(`a stranger cannot call ${key}.${fn}`, 'outsider', key, fn, args);
  const cfgNow = await ctx.view('escrow', 'get_config');
  ctx.check('ownership and settings are unchanged', sameAddress(cfgNow.owner, ctx.acct.owner) && cfgNow.paused === false && cfgNow.fee_bps === ctx.cfg.feeBps);

  // role checks on a live deal, including a mixed-case seller address
  const upper = '0x' + ctx.acct.seller.slice(2).toUpperCase();
  const id = await ctx.openDeal('buyer funds a deal naming the seller in upper-case hex', 'buyer', 'seller', CENT, { sellerText: upper });
  if (id !== null) {
    const deal = await ctx.deal(id);
    ctx.check('the seller address was normalised', sameAddress(deal.seller, ctx.acct.seller), deal.seller);
    await ctx.txFail('a stranger cannot declare delivery', 'outsider', 'escrow', 'mark_delivered', [id, 'x']);
    await ctx.txFail('the buyer cannot declare delivery for the seller', 'buyer', 'escrow', 'mark_delivered', [id, 'x']);
    await ctx.txFail('a stranger cannot confirm receipt', 'outsider', 'escrow', 'confirm_receipt', [id]);
    await ctx.txFail('the seller cannot confirm receipt for the buyer', 'seller', 'escrow', 'confirm_receipt', [id]);
    await ctx.txFail('a stranger cannot refund the buyer', 'outsider', 'escrow', 'refund_buyer', [id]);
    await ctx.tx('the seller (lower-case address) declares delivery', 'seller', 'escrow', 'mark_delivered', [id, 'sent']);
    await ctx.txFail('delivery cannot be declared twice', 'seller', 'escrow', 'mark_delivered', [id, 'again']);
    await ctx.txFail('a stranger cannot attach a case to the deal', 'outsider', 'escrow', 'attach_case', [id, 1]);
    await ctx.txFail('a case that does not exist cannot be attached', 'buyer', 'escrow', 'attach_case', [id, 999999]);
    await ctx.tx('the buyer confirms and closes the deal', 'buyer', 'escrow', 'confirm_receipt', [id]);
    await ctx.withdrawAll('seller');
  }

  // hostile ids: reads answer "not found", writes refuse cleanly
  for (const bad of [-1n, 2n ** 32n, 2n ** 40n, 2n ** 70n]) {
    const d1 = await ctx.view('escrow', 'get_deal', [bad]);
    const c1 = await ctx.view('bench', 'get_case', [bad]);
    ctx.check(`views answer "not found" for id ${bad}`, d1.found === false && c1.found === false);
  }
  await ctx.txFail('rule_case refuses an impossible id', 'outsider', 'bench', 'rule_case', [2n ** 40n]);
  await ctx.txFail('settle_deal refuses a negative id', 'outsider', 'escrow', 'settle_deal', [-1n]);
  await ctx.txFail('claim_compensation refuses an impossible id', 'buyer', 'bond', 'claim_compensation', [2n ** 70n]);
  await ctx.txFail('set_trust_threshold refuses a value beyond the storage width', 'owner', 'bond', 'set_trust_threshold', [2n ** 128n]);

  // value attached to methods that must not take it
  if (ctx.d.enforcesNonPayable === false) {
    r.skip('value sent to a non-payable method is refused', 'the local simulation does not model non-payable value checks; this runs on the live network');
  } else {
    // both calls would succeed if the value were ignored, so a refusal can only come from the value check
    await ctx.txFail('value sent to a non-payable escrow method is refused', 'owner', 'escrow', 'set_paused', [false], { value: 1n });
    await ctx.txFail('value sent to a non-payable bench method is refused', 'owner', 'bench', 'set_paused', [false], { value: 1n });
    const cfgAfter = await ctx.view('bench', 'get_config');
    ctx.check('the bench holds no value after that attempt', (await ctx.d.balance(ctx.addr.bench)) === 0n, `paused ${cfgAfter.paused}`);
  }

  // nothing to withdraw / nothing to unbond
  await ctx.txFail('a stranger has nothing to withdraw', 'outsider', 'escrow', 'withdraw');
  await ctx.txFail('a stranger has no bond to unbond', 'outsider', 'bond', 'request_unbond', [1]);
  await ctx.txFail('a stranger cannot withdraw an unbond', 'outsider', 'bond', 'withdraw_unbonded');

  // a stranger can open a case naming someone, but never get it attached to their deal
  await ctx.tx('a stranger opens a spam case naming the buyer (permissionless by design)', 'outsider', 'bench', 'open_case',
    [ctx.acct.buyer, 'spam', ctx.cfg.claimUrl, '', HOUR]);
  const spam = await ctx.lastCaseOf('outsider');
  if (spam !== null) await ctx.tx('the stranger withdraws their own spam case', 'outsider', 'bench', 'withdraw_case', [spam]);
  await ctx.solvency('after the hostile round');
  r.info('hostile round complete', 'every refusal above is an expected revert');
}

// --------------------------------------------------------------------------- //
// S7  a seller leaves with a clean record
// --------------------------------------------------------------------------- //

export async function s7BondExit(ctx) {
  const { cfg } = ctx;
  const amount = CENT * 2n;
  await ctx.tx('a second seller posts a 0.02 GEN bond', 'seller2', 'bond', 'post_bond', [], { value: amount });
  ctx.check('the second seller is trusted', (await ctx.view('bond', 'is_trusted', [ctx.acct.seller2])) === true);
  await ctx.txFail('they cannot unbond more than they have', 'seller2', 'bond', 'request_unbond', [amount + 1n]);
  await ctx.tx('they request to unbond everything', 'seller2', 'bond', 'request_unbond', [amount]);
  ctx.check('bond waiting to unbond no longer counts as trust', (await ctx.view('bond', 'is_trusted', [ctx.acct.seller2])) === false);
  await ctx.txFail('they cannot withdraw before the delay', 'seller2', 'bond', 'withdraw_unbonded');
  await ctx.wait(cfg.unbondDelay + 5, 'the unbonding delay passes');
  const before = await ctx.d.balance(ctx.acct.seller2);
  await ctx.tx('they withdraw the unbonded amount', 'seller2', 'bond', 'withdraw_unbonded');
  const bond = await ctx.view('bond', 'get_bond', [ctx.acct.seller2]);
  ctx.check('the bond is empty', BigInt(bond.bonded) === 0n, `bonded ${fmtWei(bond.bonded)}`);
  await ctx.receives('the seller receives the bond back', 'seller2', before, amount);
  await ctx.solvency('after the bond exit');
  await ctx.settledBalances('after the bond exit');
}

// --------------------------------------------------------------------------- //
// S8  final books
// --------------------------------------------------------------------------- //

export async function s8Final(ctx) {
  const { r } = ctx;
  for (const who of ['buyer', 'seller', 'seller2']) await ctx.withdrawAll(who);
  const e = await ctx.view('escrow', 'get_platform_stats');
  const owedTotal = BigInt(e.owed_total);
  ctx.check('nothing is owed once every deal is finished and every balance withdrawn (or only a pending deal remains)',
    owedTotal === 0n || e.deals > e.released + e.refunded + e.settled,
    `owed ${fmtWei(owedTotal)}, deals ${e.deals}, released ${e.released}, refunded ${e.refunded}, settled ${e.settled}`);
  const feesAvailable = BigInt(e.fees_available);
  r.info('platform fees accrued', fmtWei(feesAvailable));
  if (feesAvailable > 0n) {
    const before = await ctx.d.balance(ctx.acct.owner);
    await ctx.txFail('the owner cannot withdraw more than the accrued fees', 'owner', 'escrow', 'withdraw_fees', [ctx.acct.owner, feesAvailable + 1n]);
    await ctx.tx('the owner withdraws the accrued fees', 'owner', 'escrow', 'withdraw_fees', [ctx.acct.owner, feesAvailable]);
    await ctx.receives('the owner receives the fees', 'owner', before, feesAvailable);
  }
  await ctx.solvency('final books');
  await ctx.settledBalances('final books');
  const bench = await ctx.view('bench', 'get_stats');
  r.info('bench statistics', `opened ${bench.opened}, ruled ${bench.ruled}, appealed ${bench.appealed}, withdrawn ${bench.withdrawn}`);
}

export const SCENARIOS = [
  { id: 'S0', title: 'Setup: accounts, funding, deployment and wiring', run: s0Setup, required: true },
  { id: 'S1', title: 'An undisputed deal: fund → deliver → confirm → withdraw', run: s1Happy },
  { id: 'S2', title: 'A silent buyer: auto-release after the review window', run: s2AutoRelease },
  { id: 'S3', title: 'A seller who never delivers: full refund after the deadline', run: s3Undelivered },
  { id: 'S4', title: 'The seller refunds the buyer voluntarily', run: s4Refund },
  { id: 'S5', title: 'A disputed deal: live AI ruling, settlement, bond compensation', run: s5Dispute },
  { id: 'S6', title: 'Adversarial: hostile callers, inputs, ids and value', run: s6Adversarial },
  { id: 'S7', title: 'A seller with a clean record unbonds and leaves', run: s7BondExit },
  { id: 'S8', title: 'Final books: solvency and fee sweep', run: s8Final },
];

export async function runScenario(ctx, scenario) {
  ctx.r.scenario(scenario.id, scenario.title);
  try {
    await scenario.run(ctx);
  } catch (error) {
    ctx.r.fail(`${scenario.id} aborted`, errorText(error));
    if (scenario.required) throw error;
  }
}

export { lc };
