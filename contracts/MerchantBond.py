# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json

# ============================================================================
# MerchantBond -- link 3 of 3: seller collateral slashed by final rulings
# ============================================================================
#
# This is the third and last link. It is deployed AFTER DealEscrow, with
# DealEscrow's on-chain address passed straight into its constructor -- the
# same deploy-then-wire-the-address pattern used to build DealEscrow on top
# of ArbitrationBench:
#
#     genlayer deploy --contract contracts/ArbitrationBench.py
#         -> BENCH_ADDR
#     genlayer deploy --contract contracts/DealEscrow.py --args BENCH_ADDR
#         -> ESCROW_ADDR
#     genlayer deploy --contract contracts/MerchantBond.py --args ESCROW_ADDR
#
# MerchantBond never calls ArbitrationBench and knows nothing about cases,
# evidence or LLMs. It reads exactly two things from DealEscrow through plain
# view() calls: a settled deal's brief, and a seller's current dispute
# exposure.
#
# A seller posts a bond to signal good faith. When a deal against that seller
# is SETTLED by an AI ruling that awarded the buyer at least MIN_FAULT_PCT
# percent, the buyer may call claim_compensation(): a penalty of
# `penalty_bps` of the amount the buyer was awarded is slashed from the
# seller's bond and paid straight to the buyer, on top of the refund the
# escrow already credited. Each deal can be claimed exactly once and only
# within CLAIM_EXPIRY_SECONDS of its ruling (so a bond posted months later
# cannot be slashed for an old deal), a bond too small to cover the penalty
# pays what it has, and nobody but the deal's own
# buyer can ever trigger a slash -- there is no admin slash, no admin
# withdrawal, and no way to repoint this contract at another escrow.
#
# Walking away is deliberately slow and deliberately checked. A seller asks
# to unbond some amount (request_unbond) and can withdraw it only after
# unbond_delay seconds AND only while DealEscrow reports (a) no unresolved
# deal of theirs -- a bond stands behind every deal that is funded, delivered
# or disputed, so it cannot be pulled out from under a buyer who is still
# inside the review window -- and (b) no adverse ruling in the last
# CLAIM_WINDOW_SECONDS, which is the head start a wronged buyer is given to
# file a claim. (A seller who wants to leave sooner can end any open deal at
# once with DealEscrow.refund_buyer.) Bond that is waiting to unbond stays fully slashable, and it
# no longer counts toward the "trusted" flag.
#
# Money-safety rule (load-bearing, not optional): post_bond() is the only
# `@gl.public.write.payable` method in this contract. It must never raise --
# a rejection refunds the sender and returns {"ok": false, ...} -- and it
# makes no cross-contract call at all, so a misbehaving DealEscrow can never
# be the reason a deposit reverts. test/test_payable_safety.py enforces that
# by walking the AST, transitively through every self-helper.
#
# Solvency invariant, asserted in the tests after every scenario: this
# contract's on-chain balance always equals total_bonded -- the sum of all
# bonds -- because every other outflow (slash payout, withdrawal) reduces
# both sides of that equation together.
# ============================================================================

BPS_DENOM = 10000
DEFAULT_PENALTY_BPS = 2000
MAX_PENALTY_BPS = 5000
MIN_FAULT_PCT = 20

MIN_BOND_DEPOSIT = 10 ** 16
MAX_BOND_DEPOSIT = 1000 * 10 ** 18

DEFAULT_UNBOND_DELAY = 7 * 86400
MIN_UNBOND_DELAY = 60
MAX_UNBOND_DELAY = 30 * 86400
CLAIM_WINDOW_SECONDS = 3 * 86400
CLAIM_EXPIRY_SECONDS = 30 * 86400

DEFAULT_TRUST_THRESHOLD = 10 ** 18
MAX_TRUST_THRESHOLD = 10 ** 30
MAX_TRUSTED_STRIKES = 2


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _is_hex_address(text: str) -> bool:
    if len(text) != 42 or not text.startswith("0x"):
        return False
    for ch in text[2:]:
        if ch not in "0123456789abcdefABCDEF":
            return False
    return True


def _parse_address(text):
    """Strict parser: a 0x-prefixed 20-byte hex string, never the zero
    address. Returns an Address, or None when the text is not acceptable."""
    raw = str(text).strip()
    if not _is_hex_address(raw):
        return None
    if int(raw[2:], 16) == 0:
        return None
    return Address(raw)


def _same_addr(a, b) -> bool:
    return str(a).lower() == str(b).lower()


def _claim_key(escrow, deal_id: int) -> str:
    return str(escrow).lower() + ":" + str(int(deal_id))


@gl.contract_interface
class _DealEscrow:
    class View:
        def get_deal_brief(self, deal_id: int) -> str: ...
        def get_seller_exposure(self, seller: str) -> str: ...

    class Write:
        pass


@allow_storage
@dataclass
class Bond:
    bonded: u128
    pending_unbond: u128
    unlock_epoch: u64
    strikes: u32
    total_slashed: u128
    first_bonded_epoch: u64


class MerchantBond(gl.Contract):
    owner: Address
    paused: bool
    escrow: Address
    penalty_bps: u32
    unbond_delay: u64
    trust_threshold: u128

    bonds: TreeMap[Address, Bond]
    claimed: TreeMap[str, bool]

    total_bonded: u128
    total_slashed: u128
    total_withdrawn: u128

    count_deposits: u32
    count_claims: u32
    count_unbonds: u32

    def __init__(self, escrow_address: str, penalty_bps: int = DEFAULT_PENALTY_BPS,
            unbond_delay_seconds: int = DEFAULT_UNBOND_DELAY):
        escrow = _parse_address(escrow_address)
        if escrow is None:
            raise gl.vm.UserError("escrow_address must be a non-zero 0x-prefixed 20-byte hex address")
        penalty = int(penalty_bps)
        if penalty < 1 or penalty > MAX_PENALTY_BPS:
            raise gl.vm.UserError("penalty_bps must be 1.." + str(MAX_PENALTY_BPS))
        delay = int(unbond_delay_seconds)
        if delay < MIN_UNBOND_DELAY or delay > MAX_UNBOND_DELAY:
            raise gl.vm.UserError("unbond delay must be " + str(MIN_UNBOND_DELAY) + ".."
                + str(MAX_UNBOND_DELAY) + " seconds")
        self.owner = gl.message.sender_address
        self.paused = False
        self.escrow = escrow
        self.penalty_bps = u32(penalty)
        self.unbond_delay = u64(delay)
        self.trust_threshold = u128(DEFAULT_TRUST_THRESHOLD)

    # ------------------------------------------------------------------ #
    # internal helpers
    # ------------------------------------------------------------------ #

    def _require_owner(self) -> None:
        if str(gl.message.sender_address) != str(self.owner):
            raise gl.vm.UserError("caller is not the owner")

    def _escrow(self):
        return _DealEscrow(self.escrow)

    def _read_json(self, raw, what: str) -> dict:
        try:
            data = json.loads(raw)
        except Exception:
            raise gl.vm.UserError("DealEscrow returned an unreadable " + what)
        if not isinstance(data, dict):
            raise gl.vm.UserError("DealEscrow returned an unreadable " + what)
        return data

    def _deal_brief(self, deal_id: int) -> dict:
        data = self._read_json(self._escrow().view().get_deal_brief(int(deal_id)), "deal brief")
        if not bool(data.get("found")):
            raise gl.vm.UserError("unknown deal_id on DealEscrow")
        return data

    def _exposure(self, seller) -> dict:
        return self._read_json(self._escrow().view().get_seller_exposure(str(seller)),
            "exposure report")

    def _pay(self, to, amount: int) -> None:
        if amount <= 0:
            return
        gl.get_contract_at(Address(str(to))).emit_transfer(value=u256(int(amount)))

    def _reject(self, sender, value: int, reason: str) -> str:
        if value > 0:
            self._pay(sender, value)
        return json.dumps({"ok": False, "reason": reason, "refunded": str(value)})

    def _deposit_problem(self, value: int) -> str:
        """Validation for the payable post_bond(). Must never raise."""
        try:
            if self.paused:
                return "MerchantBond is paused for new deposits"
            if value < MIN_BOND_DEPOSIT:
                return "deposit below minimum of " + str(MIN_BOND_DEPOSIT) + " wei"
            if value > MAX_BOND_DEPOSIT:
                return "deposit above maximum of " + str(MAX_BOND_DEPOSIT) + " wei per call"
            return ""
        except Exception:
            return "malformed deposit"

    def _effective_bond(self, bond: Bond) -> int:
        bonded = int(bond.bonded)
        pending = int(bond.pending_unbond)
        return bonded - (pending if pending < bonded else bonded)

    def _is_trusted(self, bond) -> bool:
        if bond is None:
            return False
        if int(bond.strikes) > MAX_TRUSTED_STRIKES:
            return False
        effective = self._effective_bond(bond)
        return effective > 0 and effective >= int(self.trust_threshold)

    def _claim_problem(self, deal_id: int, claimer) -> tuple:
        """Shared by claim_compensation() and the preview view. Returns
        (problem, penalty, payable, seller_address); problem is "" when the
        claim would succeed right now."""
        brief = self._deal_brief(deal_id)
        if str(brief.get("status", "")) != "SETTLED" or str(brief.get("resolution", "")) != "RULING":
            return "the deal was not settled by an arbitration ruling", 0, 0, None
        if not _same_addr(brief.get("buyer", ""), claimer):
            return "only the deal's buyer can claim compensation", 0, 0, None
        if int(brief.get("buyer_pct", 0)) < MIN_FAULT_PCT:
            return ("the ruling awarded the buyer less than " + str(MIN_FAULT_PCT)
                + "%, so the seller was not at fault"), 0, 0, None
        if self.claimed.get(_claim_key(self.escrow, deal_id)) is not None:
            return "compensation for this deal was already claimed", 0, 0, None
        resolved = int(brief.get("resolved_epoch", 0))
        if resolved > 0 and _now_epoch() > resolved + CLAIM_EXPIRY_SECONDS:
            return "the claim period for this deal has ended", 0, 0, None
        seller = _parse_address(brief.get("seller", ""))
        if seller is None:
            return "DealEscrow reported an invalid seller address", 0, 0, None
        bond = self.bonds.get(seller)
        bonded = int(bond.bonded) if bond is not None else 0
        if bonded <= 0:
            return "the seller has no bond to claim against", 0, 0, seller
        penalty = int(brief.get("buyer_amount", "0")) * int(self.penalty_bps) // BPS_DENOM
        if penalty <= 0:
            return "the penalty rounds down to zero for this deal", 0, 0, seller
        return "", penalty, (penalty if penalty < bonded else bonded), seller

    def _bond_json(self, who, bond) -> dict:
        if bond is None:
            return {"address": str(who), "bonded": "0", "pending_unbond": "0",
                "effective_bond": "0", "unlock_epoch": 0, "strikes": 0, "total_slashed": "0",
                "first_bonded_epoch": 0, "trusted": False}
        return {"address": str(who), "bonded": str(int(bond.bonded)),
            "pending_unbond": str(int(bond.pending_unbond)),
            "effective_bond": str(self._effective_bond(bond)),
            "unlock_epoch": int(bond.unlock_epoch), "strikes": int(bond.strikes),
            "total_slashed": str(int(bond.total_slashed)),
            "first_bonded_epoch": int(bond.first_bonded_epoch),
            "trusted": self._is_trusted(bond)}

    # ------------------------------------------------------------------ #
    # posting collateral (the only payable method)
    # ------------------------------------------------------------------ #

    @gl.public.write.payable
    def post_bond(self) -> str:
        who = gl.message.sender_address
        value = int(gl.message.value)

        problem = self._deposit_problem(value)
        if problem != "":
            return self._reject(who, value, problem)

        now = _now_epoch()
        bond = self.bonds.get_or_insert_default(who)
        if int(bond.first_bonded_epoch) == 0:
            bond.first_bonded_epoch = u64(now)
        bond.bonded = u128(int(bond.bonded) + value)
        self.total_bonded = u128(int(self.total_bonded) + value)
        self.count_deposits = u32(int(self.count_deposits) + 1)

        return json.dumps({"ok": True, "seller": str(who), "bonded": str(int(bond.bonded)),
            "deposited": str(value)})

    # ------------------------------------------------------------------ #
    # slashing
    # ------------------------------------------------------------------ #

    @gl.public.write
    def claim_compensation(self, deal_id: int) -> str:
        """Only the deal's buyer, only for a deal settled by a ruling that
        found the seller at least MIN_FAULT_PCT percent at fault, only once
        per deal. Carries no value, so it may raise freely; it is
        intentionally NOT blocked by a pause."""
        claimer = gl.message.sender_address
        problem, penalty, payable, seller = self._claim_problem(int(deal_id), claimer)
        if problem != "":
            raise gl.vm.UserError(problem)

        bond = self.bonds.get(seller)
        bond.bonded = u128(int(bond.bonded) - payable)
        bond.strikes = u32(int(bond.strikes) + 1)
        bond.total_slashed = u128(int(bond.total_slashed) + payable)
        self.total_bonded = u128(int(self.total_bonded) - payable)
        self.total_slashed = u128(int(self.total_slashed) + payable)
        self.claimed[_claim_key(self.escrow, deal_id)] = True
        self.count_claims = u32(int(self.count_claims) + 1)
        self._pay(claimer, payable)

        return json.dumps({"ok": True, "deal_id": int(deal_id), "seller": str(seller),
            "penalty": str(penalty), "paid": str(payable), "shortfall": str(penalty - payable)})

    # ------------------------------------------------------------------ #
    # unbonding
    # ------------------------------------------------------------------ #

    @gl.public.write
    def request_unbond(self, amount: int) -> str:
        who = gl.message.sender_address
        bond = self.bonds.get(who)
        if bond is None or int(bond.bonded) <= 0:
            raise gl.vm.UserError("you have no bond")
        amt = int(amount)
        if amt <= 0 or amt > int(bond.bonded):
            raise gl.vm.UserError("amount must be 1.." + str(int(bond.bonded)) + " wei")
        now = _now_epoch()
        bond.pending_unbond = u128(amt)
        bond.unlock_epoch = u64(now + int(self.unbond_delay))
        return json.dumps({"ok": True, "pending_unbond": str(amt),
            "unlock_epoch": int(bond.unlock_epoch)})

    @gl.public.write
    def cancel_unbond(self) -> str:
        who = gl.message.sender_address
        bond = self.bonds.get(who)
        if bond is None or int(bond.pending_unbond) <= 0:
            raise gl.vm.UserError("no unbond request is pending")
        bond.pending_unbond = u128(0)
        bond.unlock_epoch = u64(0)
        return json.dumps({"ok": True, "bonded": str(int(bond.bonded))})

    @gl.public.write
    def withdraw_unbonded(self) -> str:
        who = gl.message.sender_address
        bond = self.bonds.get(who)
        if bond is None or int(bond.pending_unbond) <= 0:
            raise gl.vm.UserError("no unbond request is pending")
        now = _now_epoch()
        if now < int(bond.unlock_epoch):
            raise gl.vm.UserError("the unbonding delay has not elapsed yet")

        exposure = self._exposure(who)
        if "live_deals" not in exposure or "last_adverse_epoch" not in exposure:
            raise gl.vm.UserError("DealEscrow returned an incomplete exposure report")
        live_deals = int(exposure["live_deals"])
        if live_deals > 0:
            raise gl.vm.UserError("you still have " + str(live_deals)
                + " unresolved deal(s) on DealEscrow; your bond stands behind them until they end")
        last_adverse = int(exposure["last_adverse_epoch"])
        if last_adverse > 0 and now < last_adverse + CLAIM_WINDOW_SECONDS:
            raise gl.vm.UserError("a buyer may still claim against your bond until "
                + str(last_adverse + CLAIM_WINDOW_SECONDS))

        bonded = int(bond.bonded)
        pending = int(bond.pending_unbond)
        amt = pending if pending < bonded else bonded
        bond.bonded = u128(bonded - amt)
        bond.pending_unbond = u128(0)
        bond.unlock_epoch = u64(0)
        self.total_bonded = u128(int(self.total_bonded) - amt)
        self.total_withdrawn = u128(int(self.total_withdrawn) + amt)
        self.count_unbonds = u32(int(self.count_unbonds) + 1)
        self._pay(who, amt)
        return json.dumps({"ok": True, "withdrawn": str(amt), "bonded": str(int(bond.bonded))})

    # ------------------------------------------------------------------ #
    # admin -- none of these can move, slash or withdraw anyone's bond
    # ------------------------------------------------------------------ #

    @gl.public.write
    def set_paused(self, paused: bool) -> None:
        """Only stops NEW deposits. Claims, unbonding requests and
        withdrawals keep working."""
        self._require_owner()
        self.paused = bool(paused)

    @gl.public.write
    def set_trust_threshold(self, amount: int) -> None:
        self._require_owner()
        amt = int(amount)
        if amt < 0 or amt > MAX_TRUST_THRESHOLD:
            raise gl.vm.UserError("threshold must be 0.." + str(MAX_TRUST_THRESHOLD) + " wei")
        self.trust_threshold = u128(amt)

    @gl.public.write
    def transfer_ownership(self, new_owner: str) -> None:
        self._require_owner()
        target = _parse_address(new_owner)
        if target is None:
            raise gl.vm.UserError("new_owner must be a non-zero 0x-prefixed 20-byte hex address")
        self.owner = target

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #

    @gl.public.view
    def get_bond(self, address: str) -> str:
        who = _parse_address(address)
        if who is None:
            raise gl.vm.UserError("address must be a non-zero 0x-prefixed 20-byte hex address")
        return json.dumps(self._bond_json(who, self.bonds.get(who)))

    @gl.public.view
    def is_trusted(self, address: str) -> bool:
        """A live flag, never a stored badge: the seller has at least
        trust_threshold of bond that is NOT waiting to unbond, and no more
        than MAX_TRUSTED_STRIKES slashes on record."""
        who = _parse_address(address)
        if who is None:
            return False
        return self._is_trusted(self.bonds.get(who))

    @gl.public.view
    def preview_claim(self, deal_id: int, claimer: str) -> str:
        """What claim_compensation() would do for `claimer` right now,
        without doing it."""
        who = _parse_address(claimer)
        if who is None:
            raise gl.vm.UserError("claimer must be a non-zero 0x-prefixed 20-byte hex address")
        problem, penalty, payable, seller = self._claim_problem(int(deal_id), who)
        return json.dumps({"deal_id": int(deal_id), "eligible": problem == "",
            "reason": problem, "penalty": str(penalty), "payable": str(payable),
            "seller": str(seller) if seller is not None else ""})

    @gl.public.view
    def get_stats(self) -> str:
        return json.dumps({"total_bonded": str(int(self.total_bonded)),
            "on_chain_balance": str(int(self.balance)),
            "total_slashed": str(int(self.total_slashed)),
            "total_withdrawn": str(int(self.total_withdrawn)),
            "deposits": int(self.count_deposits), "claims": int(self.count_claims),
            "unbonds": int(self.count_unbonds)})

    @gl.public.view
    def get_config(self) -> str:
        return json.dumps({"owner": str(self.owner), "escrow": str(self.escrow),
            "paused": bool(self.paused), "penalty_bps": int(self.penalty_bps),
            "max_penalty_bps": MAX_PENALTY_BPS, "min_fault_pct": MIN_FAULT_PCT,
            "unbond_delay": int(self.unbond_delay),
            "claim_window_seconds": CLAIM_WINDOW_SECONDS,
            "claim_expiry_seconds": CLAIM_EXPIRY_SECONDS,
            "trust_threshold": str(int(self.trust_threshold)),
            "max_trusted_strikes": MAX_TRUSTED_STRIKES,
            "min_bond_deposit": str(MIN_BOND_DEPOSIT),
            "max_bond_deposit": str(MAX_BOND_DEPOSIT)})
