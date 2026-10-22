# TrustHarbor

A three-contract trust layer for peer-to-peer deals, written as GenLayer
Intelligent Contracts for **Studionet**. A buyer escrows GEN against plain
language terms, a seller delivers, and if the two disagree a panel of
validators reads both sides' evidence and rules on *how much* of the price
each side deserves. Sellers can back themselves with a bond that is slashed
when an AI ruling finds them at fault.

```
 ArbitrationBench      DealEscrow              MerchantBond
 (no GEN, stands alone) (holds buyers' GEN)     (holds sellers' bonds)
        ^                    ^   |                    |
        |  get_case_brief()  |   |  get_deal_brief()  |
        +--------------------+   +--- get_seller_     |
        constructor arg:             exposure() ------+
        bench address                constructor arg: escrow address
```

Every contract is deployed once, in order, and is constructed with the
address of the contract before it. The link only ever points backwards and is
only ever read through `view()` calls: the bench knows nothing about money,
the escrow reads the bench, the bond reads the escrow. There is no admin
function anywhere that repoints a contract at a different predecessor.

## Why three contracts

| Contract | Job | Holds GEN? | Calls another contract? |
|---|---|---|---|
| `ArbitrationBench` | Opens cases, collects evidence URLs, runs the AI ruling, handles one appeal | No | No |
| `DealEscrow` | Escrows a price, enforces the buyer / seller timeline, applies a final ruling as an exact percentage split | Yes (prices, claimable balances, fees) | Reads the bench |
| `MerchantBond` | Keeps seller collateral, slashes it for adverse rulings, gates withdrawing it | Yes (bonds only) | Reads the escrow |

Keeping them separate means the part that talks to the web and an LLM never
shares a contract with the part that holds money, and each layer can be read,
tested and audited on its own.

## ArbitrationBench

A permissionless small-claims bench.

* **Open.** A claimant opens a case against a respondent with a subject, a
  public URL holding their evidence, the plain-language *standard* both sides
  dealt under, and a response window (60 s to 7 days). At most 25 cases per
  claimant can be open at once.
* **Respond.** The respondent files one evidence URL inside the window.
* **Rule.** Once the respondent has answered, or the window has closed,
  *anyone* may call `rule_case`. The validators fetch both pages and ask an
  LLM a numeric question: what share, 0 to 100, of the disputed amount is the
  claimant entitled to?
* **Appeal.** A ruling is provisional for the appeal window (default 24 h).
  A party who ended up at least 20 points worse off than a clean win may
  appeal once. The appeal is a fresh, independent judgement that replaces the
  first one and is immediately final. A ruling nobody appeals becomes final by
  the clock alone, with no extra transaction; phases are derived from time.

### Consensus is numeric, not a label match

Each validator re-runs the whole judgement itself and agrees when its own
share lands within **15 points** of the leader's (shares are snapped to
multiples of 5 first). The leader's number is the one stored. Reasoning text
is never compared. Failures are compared by class: a validator that also
could not fetch a page, or also got a transient LLM error, agrees with a
leader that hit the same class of trouble, and a malformed LLM answer is
treated as a disagreement so the leader rotates instead of a bad answer being
frozen into consensus.

### Guard rails in the ruling itself

* A confidence below 30 is a *soft* failure: the call returns `ok: false`,
  changes nothing, and the case can be ruled again.
* An empty claimant page yields confidence 0 without spending an LLM call.
* A respondent page that cannot be fetched is passed to the LLM as
  `UNREACHABLE` instead of stalling the case; one that was never filed is
  `NOT_FILED`.
* Evidence is capped (4,000 characters per side), wrapped in marker lines
  and defanged so a page cannot forge those markers, and the prompt tells the
  model to treat it purely as evidence.
* A claimant cannot freeze their own case with an empty or dead evidence
  page: until 3 days after the response deadline an unusable claimant page
  just means "try again later"; after that the claimant forfeits (share 0).
* Validators must also agree on whether the ruling is confident, so a leader
  cannot turn thin evidence into a ruling, or censor a clear one, by
  misreporting its confidence.
* A case's appeal window is fixed when the case is opened, so an admin change
  can never shorten or stretch an in-flight dispute.
* Pausing only stops *new* cases. Responses, rulings and appeals on existing
  cases always work, so a pause can never trap a dispute.

## DealEscrow

```
FUNDED --mark_delivered--> DELIVERED --confirm_receipt--------> RELEASED
  |  \                        |   \---release_after_review----> RELEASED
  |   \--refund_buyer-------> |   \---refund_buyer-----------> REFUNDED
  |                           \--attach_case--> DISPUTED --settle_deal--> SETTLED
  \--reclaim_undelivered (after the delivery deadline)------------> REFUNDED
```

* `open_deal(seller, terms, delivery_window)` is **payable**. The sent GEN is
  the price (0.001 to 100 GEN) and the buyer's terms (up to 300 characters)
  are fixed for good.
* The seller declares delivery with `mark_delivered`, which starts the
  buyer's review window (default 3 days).
* The buyer can `confirm_receipt`; a silent buyer is taken to be satisfied
  once the window ends and anyone calls `release_after_review`. A seller
  that never delivers is refunded in full by anyone calling
  `reclaim_undelivered` after the delivery deadline.
* **Nothing is pushed.** Resolution credits each side's claimable balance and
  they pull it with `withdraw()`.

### How a dispute is bound to a deal

The escrow never decides a dispute. The unhappy party opens a case on the
bench using the exact subject and standard returned by
`get_dispute_template(deal_id)`, then calls `attach_case`. The escrow accepts
the case only if all of the following hold:

1. the caller is the deal's buyer or seller, and the deal is `DELIVERED` and
   still inside its review window;
2. the case's two parties are exactly this deal's buyer and seller;
3. the case's subject names *this deal on this escrow contract*;
4. the case's standard equals the deal's own terms, so nobody can smuggle in a
   one-sided rulebook;
5. the case was opened after the deal was created, so a ruling cannot be won
   in advance for a deal that does not exist yet;
6. the case is still `OPEN` on the bench, so nobody can open several cases,
   let them all be ruled, and attach only the outcome they like;
7. the respondent still has at least 12 hours left to answer when the case is
   bound, so nobody can be shut out of their own dispute by a short or
   already-running response window (open a case with a window of a day or
   more, and attach it right away).

After that the deal waits for the bench's `FINAL` ruling and `settle_deal`
(callable by anyone) splits the price by the awarded share. Money safety:

* the split is exact (`buyer + seller + fee == price`, no dust);
* the platform fee (default 2.5%, at most 5%) is taken only from the seller's
  side, when a deal is resolved;
* a deal's fee and review window are copied into the deal when it is funded
  (`get_deal` shows them), so a later admin change can never alter the terms
  of a deal that already exists;
* a withdrawn case returns the deal to `DELIVERED`. If the seller was the
  claimant who withdrew it, the buyer gets a fresh review window and the
  seller can no longer attach a case to that deal, so binding a case and
  withdrawing it cannot be used to run out the buyer's clock;
* a case that is never ruled falls back to an even split after 30 days, so
  funds can never be stuck behind a stalled bench;
* a seller cannot refund their way out of a pending ruling: `refund_buyer` is
  unavailable once a deal is `DISPUTED`.

## MerchantBond

Sellers `post_bond()` collateral. When a deal against them is `SETTLED` by a
ruling that awarded the buyer **at least 20%**, the deal's buyer may call
`claim_compensation(deal_id)`: a penalty of 20% of the amount the buyer was
awarded is slashed from the seller's bond and paid to the buyer, on top of the
refund the escrow already credited.

* Only the deal's own buyer can ever trigger a slash, each deal can be
  claimed once, and only within 30 days of its ruling. A bond too small for the penalty pays what it has.
* **Leaving is slow and checked.** `request_unbond(amount)` starts a delay
  (default 7 days). `withdraw_unbonded()` additionally requires that the
  escrow reports **no unresolved deal** for the seller (a bond stands behind
  every deal that is funded, delivered or disputed) and no adverse ruling in
  the last 3 days, which is the head start a wronged buyer gets to file a
  claim. Bond waiting to unbond stays fully slashable and does not count
  towards the trusted flag. A seller who wants to leave sooner can end any
  open deal at once with `refund_buyer`.
* `is_trusted(address)` is a live flag, not a stored badge: at least 1 GEN of
  bond that is not waiting to unbond, and at most 2 strikes.
* There is no admin slash, no admin withdrawal and no way to repoint the
  contract at another escrow. A pause only stops new deposits.

## Trust model and safety properties

* **Payable methods never raise.** `open_deal` and `post_bond` are the only
  methods that can receive GEN. A bad input refunds the sender in place and
  returns `{"ok": false, ...}`; neither makes any cross-contract call, so a
  misbehaving neighbour can never be the reason a deposit reverts.
  `test/test_payable_safety.py` proves this by walking the syntax tree,
  transitively through every helper.
* **Solvency invariants**, asserted after every test: the escrow's balance is
  exactly what it owes plus the fees not yet swept, and what it owes is the
  sum of all claimable balances plus all unresolved prices; the bond
  contract's balance is exactly the sum of all bonds.
* **What the owners can and cannot do.** Owners can pause new deals, new
  cases and new deposits, tune a few parameters within hard bounds (fee up to
  5%, windows, trust threshold; fee and review window only ever apply to deals
  opened afterwards, so read `get_config` before funding) and sweep *accrued
  fees* from the escrow.
  They cannot touch escrowed prices, claimable balances or bonds, cannot
  rule on a case, and cannot repoint any contract.
* **Residual risks you should know about.** An LLM ruling is an LLM ruling:
  the share tolerance and the one appeal narrow the variance, they do not
  remove it. Evidence pages are mutable web pages, and an appeal re-reads them
  without letting the other side file anything new. A flood of dust deals can
  keep a seller's bond locked until the seller refunds them. The bond only
  stands behind deals that are still unresolved or recently ruled, so buyers
  should check `get_bond` (including any pending unbond) before funding.

## Layout

```
contracts/
  ArbitrationBench.py
  DealEscrow.py
  MerchantBond.py
test/
  genlayer_stub.py          offline stand-in for the GenLayer SDK (stdlib only)
  support.py                shared fixtures, fake neighbours, the 3-contract Chain
  test_arbitration_bench.py
  test_deal_escrow.py
  test_merchant_bond.py
  test_chain_integration.py
  test_adversarial.py       hostile inputs, economic attacks, prompt injection, timing edges
  test_fuzz_chain.py        seeded random walks over all three contracts with global invariants
  test_payable_safety.py
  real_sdk/                 optional tests against the real SDK (including a random walk)
e2e/                        live end-to-end run on Studionet with a live report
.devcontainer/              Codespaces setup (Node 22, Python 3.12, port 8080)
.github/workflows/          manual Studionet run for GitHub Actions
```

## Running the tests

The default suite needs nothing but Python 3.10 or newer:

```
python3 -m unittest discover -s test -v
```

It loads the real contract files against the stand-in SDK, which mimics
storage types, read-only views, reverts that roll back state, value
transfers, the leader / validator split and the rule that non-deterministic
blocks must not touch storage. Alongside the unit tests for each contract it
runs the three-contract chain end to end, randomised lifecycle checks, and the
attack scenarios the design closes (pre-won rulings, one-sided standards,
re-targeted cases, outcome shopping, collateral pulled out from under a
buyer).

Two suites go further than scripted scenarios:

* `test_adversarial.py` hammers every public method of every contract with
  extreme and malicious arguments (it fails on any error that is not a clean
  `UserError`), attaches value to methods that must not take it, calls every
  admin method as a stranger, plays self-dealing, bond-dodging and
  rounding games, tries prompt injection through evidence, subject, standard and
  the model's own answer, and probes every deadline one second before, at and
  after the boundary.
* `test_fuzz_chain.py` runs seeded random walks (valid, role-aware and garbage
  moves, random LLM answers, validator disagreement, clock jumps) and
  recomputes global invariants from storage after every step: solvency of the
  escrow and the bond contract, `owed_total` against claimable balances and
  unresolved prices, per-seller and per-claimant counters, exact deal splits,
  terminal states never changing, and liveness (after the drain every deal can
  be finished and every wei withdrawn).

### Linting

```
pip install genvm-linter
genvm-lint check contracts/ArbitrationBench.py
genvm-lint check contracts/DealEscrow.py
genvm-lint check contracts/MerchantBond.py
```

### Optional: tests against the real SDK

`test/real_sdk` runs the same contracts on the real GenLayer Python SDK
through the simulation engine in the `genlayer-test` package, with a small
harness (`real_chain.py`) that adds balances, rollback and validator replay:

```
pip install genlayer-test numpy
python3 -m unittest discover -s test/real_sdk -v
```

The tests skip themselves when `genlayer-test` is not installed. The first
run may download the GenVM runner artifacts that the official linter also
uses.

## Live end-to-end run on Studionet

`e2e/` deploys the three contracts to Studionet with throw-away accounts and plays
real deals against them: an undisputed deal, a silent buyer, a seller who never
delivers, a voluntary refund, a disputed deal decided by the live validators
(web fetch plus LLM) with settlement and bond compensation, a round of hostile
calls, and a seller leaving with a clean record. Every step is printed as it
happens and written to a report that is rewritten after each step
(`e2e/report/index.html`, `report.md`, `report.json`, `live.log`).

In a GitHub Codespace (the `.devcontainer` installs everything), three lines:

```
cd e2e
bash run.sh
# open the forwarded port 8080 for the live report
```

Notes:

* The dispute scenario needs two public evidence pages. In a Codespace of a
  public repository they default to `e2e/evidence/*.txt` served from
  `raw.githubusercontent.com` (push them first); otherwise set `CLAIM_URL` and
  `RESPONSE_URL`. The scenario checks that the money follows the ruling exactly,
  not what the LLM decides, so any legal ruling passes.
* Payouts are emitted as finalized transfers, so on a live network the run waits
  for balances to settle (`E2E_FINAL_TIMEOUT`, default 240 s). A balance that has
  not settled yet is a warning, never a failure. The hard checks (the contracts
  always hold at least what they owe) are asserted at every point.
* The run takes several minutes because the contracts have minimum time windows
  (60 s) that the scenarios wait out. `node run.mjs --only S1,S6` runs a subset.
* `node run.mjs --local` runs the same scenarios against the real GenLayer
  Python SDK on your machine (needs `pip install genlayer-test numpy`; set
  `E2E_PYTHON` to that interpreter). Time-based waits advance a simulated clock,
  so it finishes in seconds and is a dry run of the live script.
* The `e2e-studionet` GitHub Actions workflow runs the same live script on demand
  and publishes the report on the run summary.

## Deploying to Studionet

Deploy in order, copying each printed address into the next command. Check
`genlayer deploy --help` for the argument syntax of your CLI version.

```
genlayer network set studionet

# 1. the bench (argument: appeal window in seconds)
genlayer deploy --contract contracts/ArbitrationBench.py --args 86400
#    -> BENCH_ADDR

# 2. the escrow (arguments: bench address, fee in basis points, review window in seconds)
genlayer deploy --contract contracts/DealEscrow.py --args BENCH_ADDR 250 259200
#    -> ESCROW_ADDR

# 3. the bond (arguments: escrow address, penalty in basis points, unbond delay in seconds)
genlayer deploy --contract contracts/MerchantBond.py --args ESCROW_ADDR 2000 604800
```

All trailing arguments are optional; the values shown are the defaults.

This repository does not claim any live deployment: no addresses are
published here, and the contracts were verified with the linter, the
offline suite and the real-SDK harness rather than on a live network.

## Parameters at a glance

| Setting | Default | Bounds |
|---|---|---|
| Bench: response window | chosen per case | 60 s to 7 days |
| Bench: appeal window | 24 h | 60 s to 7 days |
| Bench: share tolerance / step | 15 / 5 points | fixed |
| Bench: minimum confidence | 30 | fixed |
| Bench: appeal swing | 20 points | fixed |
| Escrow: price | n/a | 0.001 to 100 GEN |
| Escrow: platform fee | 2.5% | 0 to 5% |
| Escrow: delivery window | chosen per deal | 60 s to 60 days |
| Escrow: review window | 3 days | 60 s to 14 days |
| Escrow: response time a bound case must still leave | 12 h | fixed |
| Escrow: unruled-dispute fallback | 30 days, 50 / 50 | fixed |
| Bond: deposit per call | n/a | 0.01 to 1000 GEN |
| Bond: penalty | 20% of the buyer's award | 0.01% to 50% |
| Bond: unbond delay | 7 days | 60 s to 30 days |
| Bond: exit hold after an adverse ruling | 3 days | fixed |
| Bond: claims expire | 30 days after the ruling | fixed |
| Bench: claimant forfeits an unusable page | 3 days after the response deadline | fixed |
| Bond: trusted threshold / strikes | 1 GEN / at most 2 | threshold tunable, 0 to 10^30 wei |
