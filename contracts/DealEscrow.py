# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json

# ============================================================================
# DealEscrow -- link 2 of 3: escrowed deals that obey an ArbitrationBench
# ============================================================================
#
# This contract is deployed AFTER ArbitrationBench, with the bench's on-chain
# address passed straight into its constructor:
#
#     genlayer deploy --contract contracts/ArbitrationBench.py
#         -> note the printed address, call it BENCH_ADDR
#     genlayer deploy --contract contracts/DealEscrow.py --args BENCH_ADDR
#
# A buyer opens a deal by sending GEN with open_deal(): the price sits in
# escrow, the seller is named, and the buyer's plain-language terms are fixed
# for good. Delivery is the seller's acceptance of those terms:
#
#     FUNDED --mark_delivered--> DELIVERED --confirm_receipt--------> RELEASED
#       |  \                        |   \---release_after_review----> RELEASED
#       |   \--refund_buyer-------> |   \---refund_buyer-----------> REFUNDED
#       |                           \--attach_case--> DISPUTED --settle_deal--> SETTLED
#       \--reclaim_undelivered (after the delivery deadline)------------> REFUNDED
#
# A dispute is never decided here. The unhappy party opens a case on
# ArbitrationBench, using the exact subject and standard this contract hands
# out through get_dispute_template(), and then calls attach_case(). From that
# moment the deal waits for the bench's FINAL ruling and splits the price by
# the awarded share: DealEscrow only ever reads the bench through one narrow
# view() call, get_case_brief(). Everything the bench cannot be trusted with
# is checked here before a case is accepted: the two parties must be exactly
# this deal's buyer and seller, the subject must name this very deal on this
# very contract, the standard must equal the deal's own terms (so a claimant
# cannot smuggle in a one-sided rulebook), and the case must have been opened
# AFTER the deal existed (so nobody can pre-win a ruling for a deal that has
# not been created yet), and it must still be OPEN on the bench (so nobody can
# open several cases, let them all be ruled, and attach only the one whose
# outcome they like: the ruling is never visible before the case is bound),
# and the respondent must still have at least 12 hours left to answer when it
# is bound (so nobody can be shut out of their own dispute by a short or
# already-running response window; open a case with a window of a day or more
# and attach it right away).
#
# A deal's fee and review window are copied into the deal when it is funded,
# so no later admin change can alter the terms a buyer or seller agreed to.
#
# Money model (escrow with a percentage split, not a pool):
#   - the price is split exactly: buyer_amount + seller_gross == price, and
#     the platform fee is taken only from the seller's side, so there is no
#     rounding dust anywhere;
#   - nothing is pushed to anybody at resolution. Each side's share is
#     credited to a claimable balance and pulled with withdraw().
#
# Money-safety rule (load-bearing, not optional): open_deal() is the only
# `@gl.public.write.payable` method in this contract, so it is the only one
# that can ever receive GEN it did not ask for. It must never raise -- a
# rejection refunds the sender in place and returns {"ok": false, ...}. The
# strongest way to keep that promise is for it to never depend on anything
# outside this file: open_deal() and every helper it reaches make no
# cross-contract call at all. test/test_payable_safety.py enforces both
# properties by walking the AST, transitively through every self-helper.
#
# Nobody gets trapped: a deal nobody finishes can always be finished by a
# permissionless call (confirm, auto-release after the review window,
# reclaim after the delivery deadline), and a disputed deal whose case never
# gets ruled falls back to an even split after DISPUTE_GRACE_SECONDS. A
# pause only stops NEW deals; it never freezes an existing one.
# ============================================================================

ST_FUNDED = "FUNDED"
ST_DELIVERED = "DELIVERED"
ST_DISPUTED = "DISPUTED"
ST_RELEASED = "RELEASED"
ST_REFUNDED = "REFUNDED"
ST_SETTLED = "SETTLED"

RES_CONFIRMED = "CONFIRMED"
RES_AUTO_RELEASE = "AUTO_RELEASE"
RES_SELLER_REFUND = "SELLER_REFUND"
RES_UNDELIVERED = "UNDELIVERED_REFUND"
RES_RULING = "RULING"
RES_DEFAULT_SPLIT = "DEFAULT_SPLIT"

BPS_DENOM = 10000
DEFAULT_FEE_BPS = 250
MAX_FEE_BPS = 500

MIN_PRICE = 10 ** 15
MAX_PRICE = 100 * 10 ** 18

MAX_TERMS_LEN = 300
MAX_NOTE_LEN = 200

MIN_DELIVERY_WINDOW = 60
MAX_DELIVERY_WINDOW = 60 * 86400

DEFAULT_REVIEW_WINDOW = 3 * 86400
MIN_REVIEW_WINDOW = 60
MAX_REVIEW_WINDOW = 14 * 86400

DISPUTE_GRACE_SECONDS = 30 * 86400
MIN_RESPONSE_TIME_LEFT = 12 * 3600
DEFAULT_SPLIT_BUYER_PCT = 50

MAX_SCAN = 200
MAX_PAGE = 40


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _clamp(value: int, lo: int, hi: int) -> int:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


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


def _as_id(value):
    """A caller-supplied id as a u32, or None when it cannot be one."""
    try:
        n = int(value)
    except Exception:
        return None
    if n < 0 or n > 4294967295:
        return None
    return u32(n)


def _same_addr(a, b) -> bool:
    return str(a).lower() == str(b).lower()


@gl.contract_interface
class _ArbitrationBench:
    class View:
        def get_case_brief(self, case_id: int) -> str: ...

    class Write:
        pass


@allow_storage
@dataclass
class Deal:
    deal_id: u32
    buyer: Address
    seller: Address
    terms: str
    price: u128
    status: str
    created_epoch: u64
    delivery_deadline: u64
    delivered_epoch: u64
    review_deadline: u64
    delivery_note: str
    case_id: u32
    case_attached_epoch: u64
    resolution: str
    buyer_pct: u8
    buyer_amount: u128
    seller_amount: u128
    fee: u128
    fee_bps: u32
    review_window: u64
    blocked_attacher: Address
    resolved_epoch: u64


class DealEscrow(gl.Contract):
    owner: Address
    paused: bool
    bench: Address
    fee_bps: u32
    review_window: u64

    deals: TreeMap[u32, Deal]
    party_deals: TreeMap[Address, DynArray[u32]]
    claimable: TreeMap[Address, u128]
    seller_open_disputes: TreeMap[Address, u32]
    seller_live_deals: TreeMap[Address, u32]
    seller_last_adverse: TreeMap[Address, u64]
    next_deal_id: u32

    owed_total: u128
    fees_accrued: u128
    fees_withdrawn: u128
    total_volume: u128
    total_paid_out: u128

    count_deals: u32
    count_released: u32
    count_refunded: u32
    count_settled: u32
    count_disputed: u32

    def __init__(self, bench_address: str, fee_bps: int = DEFAULT_FEE_BPS,
            review_window_seconds: int = DEFAULT_REVIEW_WINDOW):
        bench = _parse_address(bench_address)
        if bench is None:
            raise gl.vm.UserError("bench_address must be a non-zero 0x-prefixed 20-byte hex address")
        fee = int(fee_bps)
        if fee < 0 or fee > MAX_FEE_BPS:
            raise gl.vm.UserError("fee_bps must be 0.." + str(MAX_FEE_BPS))
        window = int(review_window_seconds)
        if window < MIN_REVIEW_WINDOW or window > MAX_REVIEW_WINDOW:
            raise gl.vm.UserError("review window must be " + str(MIN_REVIEW_WINDOW) + ".."
                + str(MAX_REVIEW_WINDOW) + " seconds")
        self.owner = gl.message.sender_address
        self.paused = False
        self.bench = bench
        self.fee_bps = u32(fee)
        self.review_window = u64(window)
        self.next_deal_id = u32(0)

    # ------------------------------------------------------------------ #
    # internal helpers
    # ------------------------------------------------------------------ #

    def _require_owner(self) -> None:
        if str(gl.message.sender_address) != str(self.owner):
            raise gl.vm.UserError("caller is not the owner")

    def _get_deal(self, deal_id: int) -> Deal:
        key = _as_id(deal_id)
        deal = self.deals.get(key) if key is not None else None
        if deal is None:
            raise gl.vm.UserError("unknown deal_id")
        return deal

    def _deal_ref(self, deal_id: int) -> str:
        return "deal:" + str(gl.message.contract_address) + ":" + str(int(deal_id))

    def _bench_brief(self, case_id: int) -> dict:
        raw = _ArbitrationBench(self.bench).view().get_case_brief(int(case_id))
        try:
            data = json.loads(raw)
        except Exception:
            raise gl.vm.UserError("the arbitration bench returned an unreadable answer")
        if not isinstance(data, dict) or not bool(data.get("found")):
            raise gl.vm.UserError("case not found on the arbitration bench")
        return data

    def _pay(self, to, amount: int) -> None:
        if amount <= 0:
            return
        gl.get_contract_at(Address(str(to))).emit_transfer(value=u256(int(amount)))

    def _credit(self, who, amount: int) -> None:
        if amount <= 0:
            return
        current = self.claimable.get(who)
        self.claimable[who] = u128((int(current) if current is not None else 0) + amount)

    def _count(self, table, who) -> int:
        current = table.get(who)
        return int(current) if current is not None else 0

    def _reject(self, sender, value: int, reason: str) -> str:
        if value > 0:
            self._pay(sender, value)
        return json.dumps({"ok": False, "reason": reason, "refunded": str(value)})

    def _open_problem(self, seller, terms, delivery_window, value: int, buyer):
        """Validation for the payable open_deal(). Must never raise: any
        surprise in the arguments becomes an ordinary rejection string.
        Returns (problem, seller_address, window_seconds, clean_terms)."""
        try:
            if self.paused:
                return "DealEscrow is paused for new deals", None, 0, ""
            if value < MIN_PRICE:
                return "price below minimum of " + str(MIN_PRICE) + " wei", None, 0, ""
            if value > MAX_PRICE:
                return "price above maximum of " + str(MAX_PRICE) + " wei", None, 0, ""
            seller_addr = _parse_address(seller)
            if seller_addr is None:
                return "seller must be a non-zero 0x-prefixed 20-byte hex address", None, 0, ""
            if _same_addr(seller_addr, buyer):
                return "buyer and seller must be different addresses", None, 0, ""
            clean = str(terms).strip()
            if len(clean) == 0 or len(clean) > MAX_TERMS_LEN:
                return "terms must be 1.." + str(MAX_TERMS_LEN) + " characters", None, 0, ""
            window = int(delivery_window)
            if window < MIN_DELIVERY_WINDOW or window > MAX_DELIVERY_WINDOW:
                return ("delivery_window must be " + str(MIN_DELIVERY_WINDOW) + ".."
                    + str(MAX_DELIVERY_WINDOW) + " seconds"), None, 0, ""
            return "", seller_addr, window, clean
        except Exception:
            return "malformed arguments", None, 0, ""

    def _finish(self, deal: Deal, buyer_pct: int, resolution: str, now: int) -> None:
        """The single place a deal's money is split. Credits both sides'
        claimable balances and carves the platform fee out of owed_total."""
        price = int(deal.price)
        buyer_amount = price * buyer_pct // 100
        seller_gross = price - buyer_amount
        fee = seller_gross * int(deal.fee_bps) // BPS_DENOM
        seller_net = seller_gross - fee

        live_now = self._count(self.seller_live_deals, deal.seller)
        self.seller_live_deals[deal.seller] = u32(live_now - 1 if live_now > 0 else 0)
        was_disputed = str(deal.status) == ST_DISPUTED
        if was_disputed:
            open_now = self._count(self.seller_open_disputes, deal.seller)
            self.seller_open_disputes[deal.seller] = u32(open_now - 1 if open_now > 0 else 0)
        if resolution == RES_RULING and buyer_pct > 0:
            self.seller_last_adverse[deal.seller] = u64(now)

        if resolution == RES_CONFIRMED or resolution == RES_AUTO_RELEASE:
            deal.status = ST_RELEASED
            self.count_released = u32(int(self.count_released) + 1)
        elif resolution == RES_SELLER_REFUND or resolution == RES_UNDELIVERED:
            deal.status = ST_REFUNDED
            self.count_refunded = u32(int(self.count_refunded) + 1)
        else:
            deal.status = ST_SETTLED
            self.count_settled = u32(int(self.count_settled) + 1)

        deal.resolution = resolution
        deal.buyer_pct = u8(buyer_pct)
        deal.buyer_amount = u128(buyer_amount)
        deal.seller_amount = u128(seller_net)
        deal.fee = u128(fee)
        deal.resolved_epoch = u64(now)

        self._credit(deal.buyer, buyer_amount)
        self._credit(deal.seller, seller_net)
        self.fees_accrued = u128(int(self.fees_accrued) + fee)
        self.owed_total = u128(int(self.owed_total) - fee)

    def _deal_json(self, deal: Deal) -> dict:
        return {"deal_id": int(deal.deal_id), "buyer": str(deal.buyer), "seller": str(deal.seller),
            "terms": str(deal.terms), "price": str(int(deal.price)), "status": str(deal.status),
            "created_epoch": int(deal.created_epoch),
            "delivery_deadline": int(deal.delivery_deadline),
            "delivered_epoch": int(deal.delivered_epoch),
            "review_deadline": int(deal.review_deadline),
            "delivery_note": str(deal.delivery_note), "case_id": int(deal.case_id),
            "case_attached_epoch": int(deal.case_attached_epoch),
            "resolution": str(deal.resolution), "buyer_pct": int(deal.buyer_pct),
            "buyer_amount": str(int(deal.buyer_amount)),
            "seller_amount": str(int(deal.seller_amount)), "fee": str(int(deal.fee)),
            "fee_bps": int(deal.fee_bps), "review_window": int(deal.review_window),
            "resolved_epoch": int(deal.resolved_epoch)}

    # ------------------------------------------------------------------ #
    # opening a deal (the only payable method)
    # ------------------------------------------------------------------ #

    @gl.public.write.payable
    def open_deal(self, seller: str, terms: str, delivery_window: int) -> str:
        buyer = gl.message.sender_address
        value = int(gl.message.value)

        problem, seller_addr, window, clean_terms = self._open_problem(
            seller, terms, delivery_window, value, buyer)
        if problem != "":
            return self._reject(buyer, value, problem)

        now = _now_epoch()
        did = int(self.next_deal_id) + 1
        self.next_deal_id = u32(did)

        deal = self.deals.get_or_insert_default(u32(did))
        deal.deal_id = u32(did)
        deal.buyer = buyer
        deal.seller = seller_addr
        deal.terms = clean_terms
        deal.price = u128(value)
        deal.status = ST_FUNDED
        deal.created_epoch = u64(now)
        deal.delivery_deadline = u64(now + window)
        deal.delivered_epoch = u64(0)
        deal.review_deadline = u64(0)
        deal.delivery_note = ""
        deal.case_id = u32(0)
        deal.case_attached_epoch = u64(0)
        deal.resolution = ""
        deal.buyer_pct = u8(0)
        deal.buyer_amount = u128(0)
        deal.seller_amount = u128(0)
        deal.fee = u128(0)
        deal.fee_bps = self.fee_bps
        deal.review_window = self.review_window
        deal.resolved_epoch = u64(0)

        self.party_deals.get_or_insert_default(buyer).append(u32(did))
        self.party_deals.get_or_insert_default(seller_addr).append(u32(did))
        self.seller_live_deals[seller_addr] = u32(self._count(self.seller_live_deals, seller_addr) + 1)
        self.owed_total = u128(int(self.owed_total) + value)
        self.total_volume = u128(int(self.total_volume) + value)
        self.count_deals = u32(int(self.count_deals) + 1)

        return json.dumps({"ok": True, "deal_id": did, "buyer": str(buyer), "seller": str(seller_addr),
            "price": str(value), "delivery_deadline": now + window, "status": ST_FUNDED})

    # ------------------------------------------------------------------ #
    # the undisputed paths
    # ------------------------------------------------------------------ #

    @gl.public.write
    def mark_delivered(self, deal_id: int, note: str) -> str:
        sender = str(gl.message.sender_address)
        deal = self._get_deal(deal_id)
        if sender != str(deal.seller):
            raise gl.vm.UserError("only the seller can mark a deal delivered")
        if str(deal.status) != ST_FUNDED:
            raise gl.vm.UserError("deal is " + str(deal.status) + ", not FUNDED")
        now = _now_epoch()
        if now > int(deal.delivery_deadline):
            raise gl.vm.UserError("the delivery deadline has passed")
        text = str(note).strip()
        if len(text) > MAX_NOTE_LEN:
            raise gl.vm.UserError("note must be at most " + str(MAX_NOTE_LEN) + " characters")

        deal.status = ST_DELIVERED
        deal.delivered_epoch = u64(now)
        deal.review_deadline = u64(now + int(deal.review_window))
        deal.delivery_note = text
        return json.dumps({"ok": True, "deal_id": int(deal_id), "status": ST_DELIVERED,
            "review_deadline": int(deal.review_deadline)})

    @gl.public.write
    def confirm_receipt(self, deal_id: int) -> str:
        sender = str(gl.message.sender_address)
        deal = self._get_deal(deal_id)
        if sender != str(deal.buyer):
            raise gl.vm.UserError("only the buyer can confirm receipt")
        if str(deal.status) != ST_FUNDED and str(deal.status) != ST_DELIVERED:
            raise gl.vm.UserError("deal is " + str(deal.status) + ", not FUNDED or DELIVERED")
        self._finish(deal, 0, RES_CONFIRMED, _now_epoch())
        return json.dumps({"ok": True, "deal_id": int(deal_id), "status": str(deal.status),
            "seller_amount": str(int(deal.seller_amount)), "fee": str(int(deal.fee))})

    @gl.public.write
    def release_after_review(self, deal_id: int) -> str:
        """Permissionless. A buyer who stays silent through the whole review
        window after the seller declared delivery is taken to be satisfied."""
        deal = self._get_deal(deal_id)
        if str(deal.status) != ST_DELIVERED:
            raise gl.vm.UserError("deal is " + str(deal.status) + ", not DELIVERED")
        now = _now_epoch()
        if now < int(deal.review_deadline):
            raise gl.vm.UserError("the buyer's review window is still open")
        self._finish(deal, 0, RES_AUTO_RELEASE, now)
        return json.dumps({"ok": True, "deal_id": int(deal_id), "status": str(deal.status),
            "seller_amount": str(int(deal.seller_amount)), "fee": str(int(deal.fee))})

    @gl.public.write
    def reclaim_undelivered(self, deal_id: int) -> str:
        """Permissionless. The refund is credited to the buyer, whoever
        triggers it, once the delivery deadline has passed with the seller
        never having declared delivery."""
        deal = self._get_deal(deal_id)
        if str(deal.status) != ST_FUNDED:
            raise gl.vm.UserError("deal is " + str(deal.status) + ", not FUNDED")
        now = _now_epoch()
        if now <= int(deal.delivery_deadline):
            raise gl.vm.UserError("the delivery deadline has not passed yet")
        self._finish(deal, 100, RES_UNDELIVERED, now)
        return json.dumps({"ok": True, "deal_id": int(deal_id), "status": str(deal.status),
            "buyer_amount": str(int(deal.buyer_amount))})

    @gl.public.write
    def refund_buyer(self, deal_id: int) -> str:
        sender = str(gl.message.sender_address)
        deal = self._get_deal(deal_id)
        if sender != str(deal.seller):
            raise gl.vm.UserError("only the seller can refund the buyer")
        if str(deal.status) != ST_FUNDED and str(deal.status) != ST_DELIVERED:
            raise gl.vm.UserError("deal is " + str(deal.status) + ", not FUNDED or DELIVERED")
        self._finish(deal, 100, RES_SELLER_REFUND, _now_epoch())
        return json.dumps({"ok": True, "deal_id": int(deal_id), "status": str(deal.status),
            "buyer_amount": str(int(deal.buyer_amount))})

    # ------------------------------------------------------------------ #
    # disputes
    # ------------------------------------------------------------------ #

    @gl.public.write
    def attach_case(self, deal_id: int, case_id: int) -> str:
        sender = str(gl.message.sender_address)
        deal = self._get_deal(deal_id)
        if sender != str(deal.buyer) and sender != str(deal.seller):
            raise gl.vm.UserError("only the buyer or the seller can attach a case")
        if _same_addr(sender, deal.blocked_attacher):
            raise gl.vm.UserError("you withdrew a case bound to this deal earlier; "
                "only the other party can attach a case now")
        if str(deal.status) != ST_DELIVERED:
            raise gl.vm.UserError("deal is " + str(deal.status)
                + "; a case can only be attached while it is DELIVERED")
        now = _now_epoch()
        if now >= int(deal.review_deadline):
            raise gl.vm.UserError("the review window has closed; a case can no longer be attached")

        brief = self._bench_brief(case_id)
        claimant = str(brief.get("claimant", ""))
        respondent = str(brief.get("respondent", ""))
        straight = _same_addr(claimant, deal.buyer) and _same_addr(respondent, deal.seller)
        flipped = _same_addr(claimant, deal.seller) and _same_addr(respondent, deal.buyer)
        if not straight and not flipped:
            raise gl.vm.UserError("the case's parties are not this deal's buyer and seller")
        if str(brief.get("subject", "")) != self._deal_ref(deal_id):
            raise gl.vm.UserError("the case's subject does not name this deal")
        if str(brief.get("standard", "")) != str(deal.terms):
            raise gl.vm.UserError("the case's standard must equal the deal's terms")
        case_phase = str(brief.get("phase", ""))
        if case_phase == "WITHDRAWN":
            raise gl.vm.UserError("that case has been withdrawn")
        if case_phase != "OPEN":
            raise gl.vm.UserError("the case is already " + case_phase + " on the bench; a case must be "
                "attached before it is ruled, so that nobody can pick the outcome they like")
        if int(brief.get("opened_epoch", 0)) < int(deal.created_epoch):
            raise gl.vm.UserError("the case was opened before this deal existed")
        time_left = int(brief.get("response_deadline", 0)) - now
        if time_left < MIN_RESPONSE_TIME_LEFT:
            raise gl.vm.UserError("the respondent would have only " + str(time_left) + "s left to answer; "
                "a case bound to a deal must leave at least " + str(MIN_RESPONSE_TIME_LEFT) + "s")

        deal.status = ST_DISPUTED
        deal.case_id = u32(int(case_id))
        deal.case_attached_epoch = u64(now)
        self.seller_open_disputes[deal.seller] = u32(self._count(self.seller_open_disputes, deal.seller) + 1)
        self.count_disputed = u32(int(self.count_disputed) + 1)
        return json.dumps({"ok": True, "deal_id": int(deal_id), "case_id": int(case_id),
            "status": ST_DISPUTED})

    @gl.public.write
    def settle_deal(self, deal_id: int) -> str:
        """Permissionless. Applies the bench's FINAL ruling to a disputed
        deal. Carries no value, so it may raise freely while the ruling is
        not final yet."""
        deal = self._get_deal(deal_id)
        if str(deal.status) != ST_DISPUTED:
            raise gl.vm.UserError("deal is " + str(deal.status) + ", not DISPUTED")

        now = _now_epoch()
        brief = self._bench_brief(int(deal.case_id))
        phase = str(brief.get("phase", ""))

        if phase == "FINAL":
            share = int(brief.get("claimant_share", -1))
            if share < 0 or share > 100:
                raise gl.vm.UserError("the bench reported an invalid share")
            claimant = str(brief.get("claimant", ""))
            if _same_addr(claimant, deal.buyer):
                buyer_pct = share
            elif _same_addr(claimant, deal.seller):
                buyer_pct = 100 - share
            else:
                raise gl.vm.UserError("the bench's claimant is not a party to this deal")
            self._finish(deal, buyer_pct, RES_RULING, now)
            return json.dumps({"ok": True, "deal_id": int(deal_id), "status": str(deal.status),
                "resolution": RES_RULING, "buyer_pct": buyer_pct,
                "buyer_amount": str(int(deal.buyer_amount)),
                "seller_amount": str(int(deal.seller_amount)), "fee": str(int(deal.fee))})

        if phase == "WITHDRAWN":
            open_now = self._count(self.seller_open_disputes, deal.seller)
            self.seller_open_disputes[deal.seller] = u32(open_now - 1 if open_now > 0 else 0)
            if _same_addr(brief.get("claimant", ""), deal.seller):
                # The seller froze the buyer out of their own dispute by binding
                # a case and then withdrew it: the buyer gets a fresh review
                # window and the seller cannot bind another case to this deal.
                fresh = now + int(deal.review_window)
                if fresh > int(deal.review_deadline):
                    deal.review_deadline = u64(fresh)
                deal.blocked_attacher = deal.seller
            deal.status = ST_DELIVERED
            deal.case_id = u32(0)
            deal.case_attached_epoch = u64(0)
            return json.dumps({"ok": True, "deal_id": int(deal_id), "status": ST_DELIVERED,
                "note": "the case was withdrawn; the deal is back to DELIVERED"})

        if phase == "OPEN" and now >= int(deal.case_attached_epoch) + DISPUTE_GRACE_SECONDS:
            self._finish(deal, DEFAULT_SPLIT_BUYER_PCT, RES_DEFAULT_SPLIT, now)
            return json.dumps({"ok": True, "deal_id": int(deal_id), "status": str(deal.status),
                "resolution": RES_DEFAULT_SPLIT, "buyer_pct": DEFAULT_SPLIT_BUYER_PCT,
                "buyer_amount": str(int(deal.buyer_amount)),
                "seller_amount": str(int(deal.seller_amount)), "fee": str(int(deal.fee))})

        raise gl.vm.UserError("the case is " + phase + " on the bench; settle after its FINAL ruling "
            "(or, for a case nobody ever rules, after " + str(DISPUTE_GRACE_SECONDS) + "s)")

    # ------------------------------------------------------------------ #
    # pulling money out
    # ------------------------------------------------------------------ #

    @gl.public.write
    def withdraw(self) -> str:
        who = gl.message.sender_address
        current = self.claimable.get(who)
        amount = int(current) if current is not None else 0
        if amount <= 0:
            raise gl.vm.UserError("nothing to withdraw")
        self.claimable[who] = u128(0)
        self.owed_total = u128(int(self.owed_total) - amount)
        self.total_paid_out = u128(int(self.total_paid_out) + amount)
        self._pay(who, amount)
        return json.dumps({"ok": True, "withdrawn": str(amount)})

    # ------------------------------------------------------------------ #
    # admin
    # ------------------------------------------------------------------ #

    @gl.public.write
    def set_paused(self, paused: bool) -> None:
        self._require_owner()
        self.paused = bool(paused)

    @gl.public.write
    def set_fee_bps(self, fee_bps: int) -> None:
        """Applies to deals opened after the change: a deal's fee is fixed
        when it is funded."""
        self._require_owner()
        fee = int(fee_bps)
        if fee < 0 or fee > MAX_FEE_BPS:
            raise gl.vm.UserError("fee_bps must be 0.." + str(MAX_FEE_BPS))
        self.fee_bps = u32(fee)

    @gl.public.write
    def set_review_window(self, seconds: int) -> None:
        """Applies to deals opened after the change: a deal's review window
        is fixed when it is funded, so a change can never shorten the time a
        buyer was promised."""
        self._require_owner()
        window = int(seconds)
        if window < MIN_REVIEW_WINDOW or window > MAX_REVIEW_WINDOW:
            raise gl.vm.UserError("review window must be " + str(MIN_REVIEW_WINDOW) + ".."
                + str(MAX_REVIEW_WINDOW) + " seconds")
        self.review_window = u64(window)

    @gl.public.write
    def transfer_ownership(self, new_owner: str) -> None:
        self._require_owner()
        target = _parse_address(new_owner)
        if target is None:
            raise gl.vm.UserError("new_owner must be a non-zero 0x-prefixed 20-byte hex address")
        self.owner = target

    @gl.public.write
    def withdraw_fees(self, to: str, amount: int) -> None:
        """The only way GEN ever leaves for the owner. It can never touch
        escrowed prices or anyone's claimable balance: free_balance excludes
        owed_total, and the amount is capped by the fees actually accrued."""
        self._require_owner()
        target = _parse_address(to)
        if target is None:
            raise gl.vm.UserError("to must be a non-zero 0x-prefixed 20-byte hex address")
        available = int(self.fees_accrued) - int(self.fees_withdrawn)
        free_balance = int(self.balance) - int(self.owed_total)
        withdrawable = min(available, free_balance if free_balance > 0 else 0)
        amt = int(amount)
        if amt <= 0 or amt > withdrawable:
            raise gl.vm.UserError("amount must be 1.." + str(withdrawable) + " wei (currently withdrawable)")
        self.fees_withdrawn = u128(int(self.fees_withdrawn) + amt)
        self._pay(target, amt)

    # ------------------------------------------------------------------ #
    # views -- get_deal_brief() / get_seller_exposure() are the surface
    # MerchantBond.py is built against
    # ------------------------------------------------------------------ #

    @gl.public.view
    def get_deal(self, deal_id: int) -> str:
        key = _as_id(deal_id)
        deal = self.deals.get(key) if key is not None else None
        if deal is None:
            return json.dumps({"found": False})
        out = self._deal_json(deal)
        out["found"] = True
        return json.dumps(out)

    @gl.public.view
    def get_deal_brief(self, deal_id: int) -> str:
        key = _as_id(deal_id)
        deal = self.deals.get(key) if key is not None else None
        if deal is None:
            return json.dumps({"found": False})
        return json.dumps({"found": True, "deal_id": int(deal.deal_id), "buyer": str(deal.buyer),
            "seller": str(deal.seller), "price": str(int(deal.price)), "status": str(deal.status),
            "resolution": str(deal.resolution), "buyer_pct": int(deal.buyer_pct),
            "buyer_amount": str(int(deal.buyer_amount)),
            "resolved_epoch": int(deal.resolved_epoch)})

    @gl.public.view
    def get_seller_exposure(self, seller: str) -> str:
        """What a collateral contract needs to know before it lets a seller
        walk away with a bond: how many of their deals are still unresolved
        (funded, delivered or disputed -- in each of those a buyer's money is
        still at stake), how many of those are in dispute, and when the
        latest adverse ruling was made."""
        who = _parse_address(seller)
        if who is None:
            raise gl.vm.UserError("seller must be a non-zero 0x-prefixed 20-byte hex address")
        last = self.seller_last_adverse.get(who)
        return json.dumps({"seller": str(who),
            "live_deals": self._count(self.seller_live_deals, who),
            "open_disputes": self._count(self.seller_open_disputes, who),
            "last_adverse_epoch": int(last) if last is not None else 0})

    @gl.public.view
    def get_dispute_template(self, deal_id: int) -> str:
        """Exactly what a party must put into ArbitrationBench.open_case()
        for attach_case() to accept the case: the subject, the standard, and
        who is on the other side."""
        key = _as_id(deal_id)
        deal = self.deals.get(key) if key is not None else None
        if deal is None:
            raise gl.vm.UserError("unknown deal_id")
        return json.dumps({"deal_id": int(deal_id), "bench": str(self.bench),
            "subject": self._deal_ref(deal_id), "standard": str(deal.terms),
            "buyer": str(deal.buyer), "seller": str(deal.seller)})

    @gl.public.view
    def get_claimable(self, address: str) -> str:
        who = _parse_address(address)
        if who is None:
            raise gl.vm.UserError("address must be a non-zero 0x-prefixed 20-byte hex address")
        return str(self._count(self.claimable, who))

    @gl.public.view
    def get_deals_by_party(self, party: str, offset: int = 0, limit: int = MAX_PAGE) -> str:
        who = _parse_address(party)
        if who is None:
            raise gl.vm.UserError("party must be a non-zero 0x-prefixed 20-byte hex address")
        ids = self.party_deals.get(who)
        if ids is None:
            return json.dumps({"party": str(who), "total": 0, "deals": []})
        off = max(0, int(offset))
        lim = _clamp(int(limit), 1, MAX_PAGE)
        out = []
        scanned = 0
        i = off
        while i < len(ids) and len(out) < lim and scanned < MAX_SCAN:
            deal = self.deals.get(ids[i])
            if deal is not None:
                out.append(self._deal_json(deal))
            i += 1
            scanned += 1
        return json.dumps({"party": str(who), "total": len(ids), "deals": out})

    @gl.public.view
    def get_platform_stats(self) -> str:
        fees_available = int(self.fees_accrued) - int(self.fees_withdrawn)
        return json.dumps({"owed_total": str(int(self.owed_total)),
            "on_chain_balance": str(int(self.balance)),
            "fees_accrued": str(int(self.fees_accrued)),
            "fees_withdrawn": str(int(self.fees_withdrawn)),
            "fees_available": str(fees_available),
            "total_volume": str(int(self.total_volume)),
            "total_paid_out": str(int(self.total_paid_out)),
            "deals": int(self.count_deals), "released": int(self.count_released),
            "refunded": int(self.count_refunded), "settled": int(self.count_settled),
            "disputed": int(self.count_disputed)})

    @gl.public.view
    def get_config(self) -> str:
        return json.dumps({"owner": str(self.owner), "bench": str(self.bench),
            "paused": bool(self.paused), "fee_bps": int(self.fee_bps),
            "max_fee_bps": MAX_FEE_BPS, "review_window": int(self.review_window),
            "min_price": str(MIN_PRICE), "max_price": str(MAX_PRICE),
            "min_delivery_window": MIN_DELIVERY_WINDOW, "max_delivery_window": MAX_DELIVERY_WINDOW,
            "dispute_grace_seconds": DISPUTE_GRACE_SECONDS,
            "min_response_time_left": MIN_RESPONSE_TIME_LEFT})
