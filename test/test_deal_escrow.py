"""
Offline tests for contracts/DealEscrow.py, run against a stand-in bench so
that every answer the escrow can receive from the bench is under the test's
control.

After every test the solvency invariants are checked:
  * the contract's balance equals what it owes plus the fees not yet swept;
  * what it owes equals every claimable balance plus every unresolved price;
  * every resolved deal split its price exactly.

Run with:  python3 -m unittest discover -s test -v
"""
import random
import unittest

import genlayer_stub as stub
from support import (BENCH_ADDR, BUYER, DAY, ESCROW_ADDR, GEN, HOUR, MINUTE, OTHER_BUYER, OTHER_SELLER,
    OUTSIDER, OWNER, SELLER, TERMS, as_, deploy_fake_bench, j, load_escrow, make_brief)

LIVE = ("FUNDED", "DELIVERED", "DISPUTED")


class EscrowTestCase(unittest.TestCase):
    FEE_BPS = 250

    def setUp(self):
        stub.reset()
        self.mod = load_escrow()
        self.fake = deploy_fake_bench()
        self.escrow = stub.deploy(self.mod.DealEscrow, ESCROW_ADDR, BENCH_ADDR, self.FEE_BPS, DAY,
            sender=OWNER)
        self._next_case = 0

    def tearDown(self):
        self.assert_invariants()

    # -- helpers -- #

    def assert_invariants(self):
        e = self.escrow
        fees_left = int(e.fees_accrued) - int(e.fees_withdrawn)
        self.assertEqual(stub.balance_of(ESCROW_ADDR), int(e.owed_total) + fees_left,
            "balance must equal owed_total plus unswept fees")
        claimable = sum(int(v) for v in e.claimable.values())
        live_prices = sum(int(d.price) for d in e.deals.values() if str(d.status) in LIVE)
        self.assertEqual(int(e.owed_total), claimable + live_prices,
            "owed_total must equal all claimable balances plus all unresolved prices")
        self.assertGreaterEqual(fees_left, 0)
        disputed = {}
        live = {}
        for d in e.deals.values():
            self.assertEqual(int(d.price) > 0, True)
            if str(d.status) in LIVE:
                self.assertEqual((int(d.buyer_amount), int(d.seller_amount), int(d.fee)), (0, 0, 0))
            else:
                self.assertEqual(int(d.buyer_amount) + int(d.seller_amount) + int(d.fee), int(d.price),
                    "a resolved deal must split its price exactly")
            if str(d.status) in LIVE:
                live[str(d.seller)] = live.get(str(d.seller), 0) + 1
            if str(d.status) == "DISPUTED":
                disputed[str(d.seller)] = disputed.get(str(d.seller), 0) + 1
        for seller in {str(d.seller) for d in e.deals.values()}:
            exposure = j(e.get_seller_exposure(seller))
            self.assertEqual(exposure["open_disputes"], disputed.get(seller, 0))
            self.assertEqual(exposure["live_deals"], live.get(seller, 0))
        self.assertEqual(sum(int(v) for v in e.seller_live_deals.values()), sum(live.values()))
        total_open = sum(int(v) for v in e.seller_open_disputes.values())
        self.assertEqual(total_open, sum(disputed.values()))

    def open_deal(self, buyer=BUYER, seller=SELLER, price=5 * GEN, terms=TERMS, window=DAY):
        as_(buyer, price)
        return j(self.escrow.open_deal(seller, terms, window))

    def new_deal(self, **kwargs):
        out = self.open_deal(**kwargs)
        self.assertTrue(out["ok"], out)
        return out["deal_id"]

    def delivered_deal(self, **kwargs):
        deal_id = self.new_deal(**kwargs)
        as_(kwargs.get("seller", SELLER))
        self.escrow.mark_delivered(deal_id, "done")
        return deal_id

    def deal(self, deal_id):
        return j(self.escrow.get_deal(deal_id))

    def call(self, who, method, *args):
        as_(who)
        return j(getattr(self.escrow, method)(*args))

    def reverts(self, who, method, *args, contains=""):
        as_(who)
        with self.assertRaises(stub.UserError) as ctx:
            getattr(self.escrow, method)(*args)
        self.assertIn(contains, ctx.exception.message)

    def install_case(self, deal_id, claimant=BUYER, respondent=SELLER, phase="OPEN", share=-1, **over):
        """Installs a bench brief for the deal that attach_case() accepts,
        unless `over` breaks one of its fields."""
        template = j(self.escrow.get_dispute_template(deal_id))
        self._next_case += 1
        fields = dict(claimant=claimant, respondent=respondent, subject=template["subject"],
            standard=template["standard"], phase=phase, opened_epoch=stub.CTX.now, claimant_share=share)
        fields.update(over)
        self.fake._briefs[self._next_case] = make_brief(self._next_case, **fields)
        return self._next_case

    def disputed_deal(self, claimant=None, respondent=None, **kwargs):
        buyer = kwargs.get("buyer", BUYER)
        seller = kwargs.get("seller", SELLER)
        claimant = buyer if claimant is None else claimant
        respondent = seller if respondent is None else respondent
        deal_id = self.delivered_deal(**kwargs)
        case_id = self.install_case(deal_id, claimant, respondent)
        self.call(claimant, "attach_case", deal_id, case_id)
        return deal_id, case_id

    def set_case(self, case_id, **changes):
        self.fake._briefs[case_id].update(changes)
        if "phase" in changes:
            self.fake._briefs[case_id]["final"] = changes["phase"] == "FINAL"

    def split(self, price, pct, fee_bps=FEE_BPS):
        buyer_amount = price * pct // 100
        gross = price - buyer_amount
        fee = gross * fee_bps // 10000
        return buyer_amount, gross - fee, fee

    # ------------------------------------------------------------------ #
    # constructor
    # ------------------------------------------------------------------ #

    def test_constructor_rejects_bad_bench_addresses(self):
        for bad in ("", "nope", "0x1234", "0x" + "00" * 20, "0x" + "zz" * 20, "0x" + "11" * 21):
            with self.assertRaises(stub.UserError, msg=bad):
                stub.deploy(self.mod.DealEscrow, "0x" + "c9" * 20, bad, sender=OWNER)

    def test_constructor_rejects_bad_fee_and_review_window(self):
        for fee in (-1, 501, 10 ** 6):
            with self.assertRaises(stub.UserError, msg=str(fee)):
                stub.deploy(self.mod.DealEscrow, "0x" + "c9" * 20, BENCH_ADDR, fee, DAY, sender=OWNER)
        for window in (0, 59, 14 * DAY + 1, -1):
            with self.assertRaises(stub.UserError, msg=str(window)):
                stub.deploy(self.mod.DealEscrow, "0x" + "c9" * 20, BENCH_ADDR, 100, window, sender=OWNER)

    def test_constructor_accepts_the_edges_and_defaults(self):
        stub.deploy(self.mod.DealEscrow, "0xa1" + "00" * 19, BENCH_ADDR, 0, 60, sender=OWNER)
        stub.deploy(self.mod.DealEscrow, "0xa2" + "00" * 19, BENCH_ADDR, 500, 14 * DAY, sender=OWNER)
        other = stub.deploy(self.mod.DealEscrow, "0xa3" + "00" * 19, BENCH_ADDR, sender=OWNER)
        cfg = j(other.get_config())
        self.assertEqual((cfg["fee_bps"], cfg["review_window"]), (250, 3 * DAY))
        self.assertEqual(cfg["owner"], OWNER)
        self.assertEqual(cfg["bench"], BENCH_ADDR)

    # ------------------------------------------------------------------ #
    # open_deal
    # ------------------------------------------------------------------ #

    def test_open_deal_escrows_the_price_and_records_everything(self):
        out = self.open_deal(terms="  Deliver the pack.  ")
        self.assertEqual((out["ok"], out["deal_id"], out["status"]), (True, 1, "FUNDED"))
        self.assertEqual(out["delivery_deadline"], stub.CTX.now + DAY)
        deal = self.deal(1)
        self.assertEqual((deal["buyer"], deal["seller"], deal["terms"], deal["price"]),
            (BUYER, SELLER, "Deliver the pack.", str(5 * GEN)))
        self.assertEqual(stub.balance_of(ESCROW_ADDR), 5 * GEN)
        stats = j(self.escrow.get_platform_stats())
        self.assertEqual((stats["owed_total"], stats["total_volume"], stats["deals"]),
            (str(5 * GEN), str(5 * GEN), 1))
        for party in (BUYER, SELLER):
            self.assertEqual([d["deal_id"] for d in j(self.escrow.get_deals_by_party(party))["deals"]], [1])

    def test_deal_ids_increment_and_money_accumulates(self):
        self.new_deal(price=GEN)
        self.new_deal(price=2 * GEN, buyer=OTHER_BUYER, seller=OTHER_SELLER)
        self.assertEqual(self.deal(2)["deal_id"], 2)
        self.assertEqual(stub.balance_of(ESCROW_ADDR), 3 * GEN)

    def rejected(self, expect_fragment, **kwargs):
        price = kwargs.get("price", 5 * GEN)
        before_paid = stub.paid_to(kwargs.get("buyer", BUYER))
        before_next = int(self.escrow.next_deal_id)
        out = self.open_deal(**kwargs)
        self.assertFalse(out["ok"], out)
        self.assertIn(expect_fragment, out["reason"])
        self.assertEqual(out["refunded"], str(price))
        self.assertEqual(stub.paid_to(kwargs.get("buyer", BUYER)) - before_paid, price)
        self.assertEqual(int(self.escrow.next_deal_id), before_next)
        self.assertEqual(stub.balance_of(ESCROW_ADDR), 0)

    def test_open_deal_refunds_instead_of_raising_for_every_bad_input(self):
        self.rejected("below minimum", price=self.mod.MIN_PRICE - 1)
        self.rejected("above maximum", price=self.mod.MAX_PRICE + 1)
        for bad_seller in ("", "nope", "0x1234", "0x" + "00" * 20, "0x" + "zz" * 20, "0x" + "11" * 21,
                None, 12345, "0x" + "33" * 19):
            self.rejected("seller must be", seller=bad_seller)
        self.rejected("different addresses", seller=BUYER)
        self.rejected("different addresses", seller=BUYER.upper().replace("0X", "0x"))
        for bad_terms in ("", "   ", "t" * (self.mod.MAX_TERMS_LEN + 1)):
            self.rejected("terms must be", terms=bad_terms)
        for bad_window in (0, 59, 60 * DAY + 1, -3):
            self.rejected("delivery_window", window=bad_window)
        for weird_window in ("abc", None, [1], {"a": 1}):
            self.rejected("malformed", window=weird_window)

    def test_open_deal_accepts_exact_boundaries(self):
        self.assertTrue(self.open_deal(price=self.mod.MIN_PRICE)["ok"])
        self.assertTrue(self.open_deal(price=self.mod.MAX_PRICE, buyer=OTHER_BUYER)["ok"])
        self.assertTrue(self.open_deal(terms="t" * self.mod.MAX_TERMS_LEN, buyer=OUTSIDER)["ok"])
        self.assertTrue(self.open_deal(window=60, buyer=OUTSIDER)["ok"])
        self.assertTrue(self.open_deal(window=60 * DAY, buyer=OUTSIDER)["ok"])

    def test_open_deal_with_no_value_is_a_clean_rejection_with_no_transfer(self):
        as_(BUYER, 0)
        out = j(self.escrow.open_deal(SELLER, TERMS, DAY))
        self.assertFalse(out["ok"])
        self.assertEqual(stub.PAYMENTS, [])

    def test_paused_escrow_refunds_new_deals_but_does_not_raise(self):
        as_(OWNER)
        self.escrow.set_paused(True)
        self.rejected("paused")
        as_(OWNER)
        self.escrow.set_paused(False)
        self.assertTrue(self.open_deal()["ok"])

    def test_open_deal_fuzz_never_raises_and_never_loses_money(self):
        rng = random.Random(20260101)
        sellers = [SELLER, BUYER, "", "0x" + "00" * 20, None, 5, "0x" + "5a" * 20, "garbage"]
        terms = [TERMS, "", " ", "x" * 301, "y", None, 7]
        windows = [60, 59, DAY, 10 ** 12, -1, "5", None, 3.5 if False else 3600]
        deposited = refunded = 0
        for _ in range(400):
            value = rng.choice([0, 1, 10 ** 15 - 1, 10 ** 15, 10 ** 18, 100 * 10 ** 18, 100 * 10 ** 18 + 1])
            as_(BUYER, value)
            out = j(self.escrow.open_deal(rng.choice(sellers), rng.choice(terms), rng.choice(windows)))
            self.assertIsInstance(out["ok"], bool)
            if out["ok"]:
                deposited += value
            else:
                self.assertEqual(out["refunded"], str(value))
                refunded += value
        self.assertEqual(stub.balance_of(ESCROW_ADDR), deposited)
        self.assertEqual(stub.paid_to(BUYER), refunded)

    # ------------------------------------------------------------------ #
    # mark_delivered
    # ------------------------------------------------------------------ #

    def test_mark_delivered_sets_the_review_clock(self):
        deal_id = self.new_deal()
        out = self.call(SELLER, "mark_delivered", deal_id, "  here is the link  ")
        self.assertEqual(out["status"], "DELIVERED")
        deal = self.deal(deal_id)
        self.assertEqual(deal["delivery_note"], "here is the link")
        self.assertEqual(deal["delivered_epoch"], stub.CTX.now)
        self.assertEqual(deal["review_deadline"], stub.CTX.now + DAY)

    def test_mark_delivered_guards(self):
        deal_id = self.new_deal(window=2 * MINUTE)
        for who in (BUYER, OUTSIDER, OWNER):
            self.reverts(who, "mark_delivered", deal_id, "x", contains="only the seller")
        self.reverts(SELLER, "mark_delivered", deal_id, "n" * 201, contains="at most")
        self.reverts(SELLER, "mark_delivered", 99, "x", contains="unknown deal_id")
        stub.advance(2 * MINUTE)
        self.assertEqual(self.call(SELLER, "mark_delivered", deal_id, "on the last second")["status"],
            "DELIVERED")
        self.reverts(SELLER, "mark_delivered", deal_id, "again", contains="not FUNDED")

    def test_mark_delivered_after_the_deadline_is_refused(self):
        deal_id = self.new_deal(window=2 * MINUTE)
        stub.advance(2 * MINUTE + 1)
        self.reverts(SELLER, "mark_delivered", deal_id, "late", contains="deadline has passed")

    def test_review_window_is_fixed_when_the_deal_is_funded(self):
        as_(OWNER)
        self.escrow.set_review_window(2 * HOUR)
        early = self.new_deal()
        later = self.new_deal(buyer=OTHER_BUYER)
        as_(OWNER)
        self.escrow.set_review_window(60)
        latest = self.new_deal(buyer=OUTSIDER)
        for deal_id in (early, later, latest):
            self.call(SELLER, "mark_delivered", deal_id, "x")
        self.assertEqual(self.deal(early)["review_deadline"], stub.CTX.now + 2 * HOUR)
        self.assertEqual(self.deal(later)["review_deadline"], stub.CTX.now + 2 * HOUR,
            "a later change must not shorten the time a funded buyer was promised")
        self.assertEqual(self.deal(latest)["review_deadline"], stub.CTX.now + 60)
        self.assertEqual((self.deal(early)["review_window"], self.deal(latest)["review_window"]), (2 * HOUR, 60))

    # ------------------------------------------------------------------ #
    # undisputed endings and exact splits
    # ------------------------------------------------------------------ #

    def test_confirm_receipt_releases_to_the_seller_minus_the_fee(self):
        deal_id = self.delivered_deal()
        out = self.call(BUYER, "confirm_receipt", deal_id)
        buyer_amount, seller_net, fee = self.split(5 * GEN, 0)
        self.assertEqual((out["status"], out["seller_amount"], out["fee"]),
            ("RELEASED", str(seller_net), str(fee)))
        self.assertEqual(self.escrow.get_claimable(SELLER), str(seller_net))
        self.assertEqual(self.escrow.get_claimable(BUYER), "0")
        deal = self.deal(deal_id)
        self.assertEqual((deal["resolution"], deal["buyer_pct"]), ("CONFIRMED", 0))

    def test_confirm_receipt_works_before_delivery_too_and_only_for_the_buyer(self):
        deal_id = self.new_deal()
        for who in (SELLER, OUTSIDER):
            self.reverts(who, "confirm_receipt", deal_id, contains="only the buyer")
        self.assertEqual(self.call(BUYER, "confirm_receipt", deal_id)["status"], "RELEASED")
        self.reverts(BUYER, "confirm_receipt", deal_id, contains="not FUNDED or DELIVERED")

    def test_release_after_review_needs_the_whole_window_and_is_permissionless(self):
        deal_id = self.delivered_deal()
        self.reverts(OUTSIDER, "release_after_review", deal_id, contains="still open")
        stub.advance(DAY - 1)
        self.reverts(OUTSIDER, "release_after_review", deal_id, contains="still open")
        stub.advance(1)
        out = self.call(OUTSIDER, "release_after_review", deal_id)
        self.assertEqual(out["status"], "RELEASED")
        self.assertEqual(self.deal(deal_id)["resolution"], "AUTO_RELEASE")
        self.reverts(OUTSIDER, "release_after_review", deal_id, contains="not DELIVERED")

    def test_release_after_review_needs_delivery(self):
        deal_id = self.new_deal()
        stub.advance(40 * DAY)
        self.reverts(OUTSIDER, "release_after_review", deal_id, contains="not DELIVERED")

    def test_reclaim_undelivered_after_the_deadline_credits_the_buyer_in_full(self):
        deal_id = self.new_deal(window=2 * MINUTE)
        self.reverts(OUTSIDER, "reclaim_undelivered", deal_id, contains="not passed")
        stub.advance(2 * MINUTE)
        self.reverts(OUTSIDER, "reclaim_undelivered", deal_id, contains="not passed")
        stub.advance(1)
        out = self.call(OUTSIDER, "reclaim_undelivered", deal_id)
        self.assertEqual((out["status"], out["buyer_amount"]), ("REFUNDED", str(5 * GEN)))
        self.assertEqual(self.escrow.get_claimable(BUYER), str(5 * GEN))
        self.assertEqual(j(self.escrow.get_platform_stats())["fees_accrued"], "0")
        self.reverts(OUTSIDER, "reclaim_undelivered", deal_id, contains="not FUNDED")

    def test_reclaim_is_refused_once_delivered(self):
        deal_id = self.delivered_deal(window=2 * MINUTE)
        stub.advance(DAY - 1 if False else 5 * MINUTE)
        self.reverts(OUTSIDER, "reclaim_undelivered", deal_id, contains="not FUNDED")

    def test_refund_buyer_is_a_seller_only_full_refund_with_no_fee(self):
        deal_id = self.delivered_deal()
        for who in (BUYER, OUTSIDER):
            self.reverts(who, "refund_buyer", deal_id, contains="only the seller")
        out = self.call(SELLER, "refund_buyer", deal_id)
        self.assertEqual((out["status"], out["buyer_amount"]), ("REFUNDED", str(5 * GEN)))
        self.assertEqual(self.deal(deal_id)["resolution"], "SELLER_REFUND")
        self.assertEqual(self.escrow.get_claimable(SELLER), "0")
        self.reverts(SELLER, "refund_buyer", deal_id, contains="not FUNDED or DELIVERED")

    def test_seller_can_refund_before_delivering_too(self):
        deal_id = self.new_deal()
        self.assertEqual(self.call(SELLER, "refund_buyer", deal_id)["status"], "REFUNDED")

    def test_a_seller_cannot_refund_their_way_out_of_a_pending_dispute(self):
        deal_id, _ = self.disputed_deal()
        self.reverts(SELLER, "refund_buyer", deal_id, contains="not FUNDED or DELIVERED")
        self.reverts(BUYER, "confirm_receipt", deal_id, contains="not FUNDED or DELIVERED")

    def test_zero_fee_deployment_takes_nothing(self):
        stub.reset()
        self.mod = load_escrow()
        self.fake = deploy_fake_bench()
        self.escrow = stub.deploy(self.mod.DealEscrow, ESCROW_ADDR, BENCH_ADDR, 0, DAY, sender=OWNER)
        deal_id = self.delivered_deal()
        out = self.call(BUYER, "confirm_receipt", deal_id)
        self.assertEqual((out["seller_amount"], out["fee"]), (str(5 * GEN), "0"))

    def test_splits_are_always_exact_for_awkward_prices(self):
        for fee_bps in (0, 1, 250, 333, 500):
            for price in (10 ** 15, 10 ** 15 + 1, 123456789012345679, 10 ** 18 + 7, 99 * 10 ** 18 + 13):
                for pct in range(0, 101, 5):
                    self.setUp()
                    as_(OWNER)
                    self.escrow.set_fee_bps(fee_bps)
                    deal_id, case_id = self.disputed_deal(price=price)
                    self.set_case(case_id, phase="FINAL", claimant_share=pct)
                    out = self.call(OUTSIDER, "settle_deal", deal_id)
                    buyer_amount, seller_net, fee = self.split(price, pct, fee_bps)
                    self.assertEqual((int(out["buyer_amount"]), int(out["seller_amount"]), int(out["fee"])),
                        (buyer_amount, seller_net, fee))
                    self.assertEqual(buyer_amount + seller_net + fee, price)
                    self.tearDown()

    # ------------------------------------------------------------------ #
    # attach_case
    # ------------------------------------------------------------------ #

    def test_attach_case_binds_the_case_and_counts_the_dispute(self):
        deal_id = self.delivered_deal()
        case_id = self.install_case(deal_id)
        out = self.call(BUYER, "attach_case", deal_id, case_id)
        self.assertEqual((out["status"], out["case_id"]), ("DISPUTED", case_id))
        deal = self.deal(deal_id)
        self.assertEqual((deal["status"], deal["case_id"], deal["case_attached_epoch"]),
            ("DISPUTED", case_id, stub.CTX.now))
        self.assertEqual(j(self.escrow.get_seller_exposure(SELLER))["open_disputes"], 1)
        self.assertEqual(j(self.escrow.get_platform_stats())["disputed"], 1)

    def test_either_party_may_attach_and_the_seller_may_be_the_claimant(self):
        deal_id = self.delivered_deal()
        case_id = self.install_case(deal_id, claimant=SELLER, respondent=BUYER)
        out = self.call(SELLER, "attach_case", deal_id, case_id)
        self.assertEqual(out["status"], "DISPUTED")
        other = self.delivered_deal(buyer=OTHER_BUYER)
        case2 = self.install_case(other, claimant=OTHER_BUYER, respondent=SELLER)
        self.assertEqual(self.call(SELLER, "attach_case", other, case2)["status"], "DISPUTED")

    def test_only_the_parties_can_attach(self):
        deal_id = self.delivered_deal()
        case_id = self.install_case(deal_id)
        for who in (OUTSIDER, OWNER, OTHER_BUYER):
            self.reverts(who, "attach_case", deal_id, case_id, contains="only the buyer or the seller")

    def test_attach_needs_a_delivered_deal_inside_the_review_window(self):
        funded = self.new_deal()
        self.reverts(BUYER, "attach_case", funded, self.install_case(funded), contains="FUNDED")
        deal_id = self.delivered_deal(buyer=OTHER_BUYER)
        stub.advance(DAY - 1)
        case_id = self.install_case(deal_id, claimant=OTHER_BUYER)
        self.assertEqual(self.call(OTHER_BUYER, "attach_case", deal_id, case_id)["status"], "DISPUTED")
        late = self.delivered_deal(buyer=OUTSIDER)
        stub.advance(DAY)
        late_case = self.install_case(late, claimant=OUTSIDER)
        self.reverts(OUTSIDER, "attach_case", late, late_case, contains="review window has closed")

    def test_attach_rejects_unknown_deal_and_unknown_or_unreadable_case(self):
        self.reverts(BUYER, "attach_case", 9, 1, contains="unknown deal_id")
        deal_id = self.delivered_deal()
        self.reverts(BUYER, "attach_case", deal_id, 77, contains="case not found")
        self.fake._raw[5] = "this is not json"
        self.reverts(BUYER, "attach_case", deal_id, 5, contains="unreadable")
        self.fake._raw[6] = "[1, 2, 3]"
        self.reverts(BUYER, "attach_case", deal_id, 6, contains="case not found")

    def test_attach_rejects_a_case_between_the_wrong_parties(self):
        deal_id = self.delivered_deal()
        wrong = [dict(claimant=OUTSIDER, respondent=SELLER), dict(claimant=BUYER, respondent=OUTSIDER),
            dict(claimant=OUTSIDER, respondent=OTHER_SELLER), dict(claimant=BUYER, respondent=BUYER),
            dict(claimant=SELLER, respondent=SELLER), dict(claimant="", respondent="")]
        for parties in wrong:
            case_id = self.install_case(deal_id, **parties)
            self.reverts(BUYER, "attach_case", deal_id, case_id, contains="parties are not")

    def test_attach_accepts_mixed_case_party_strings(self):
        deal_id = self.delivered_deal()
        case_id = self.install_case(deal_id, claimant=BUYER.replace("22", "2A"), respondent=SELLER.upper()
            .replace("0X", "0x"))
        # 0x2A... and 0x22... are different addresses, so this one must be refused
        self.reverts(BUYER, "attach_case", deal_id, case_id, contains="parties are not")
        ok = self.install_case(deal_id, claimant=BUYER.upper().replace("0X", "0x"), respondent=SELLER)
        self.assertEqual(self.call(BUYER, "attach_case", deal_id, ok)["status"], "DISPUTED")

    def test_attach_rejects_a_case_that_names_another_deal_or_another_escrow(self):
        deal_a = self.delivered_deal()
        deal_b = self.delivered_deal()
        case_for_a = self.install_case(deal_a)
        self.reverts(BUYER, "attach_case", deal_b, case_for_a, contains="subject does not name")
        elsewhere = self.install_case(deal_a, subject="deal:" + "0x" + "99" * 20 + ":" + str(deal_a))
        self.reverts(BUYER, "attach_case", deal_a, elsewhere, contains="subject does not name")
        for subject in ("", "deal:1", "Order 17", j(self.escrow.get_dispute_template(deal_a))["subject"] + " "):
            case_id = self.install_case(deal_a, subject=subject)
            self.reverts(BUYER, "attach_case", deal_a, case_id, contains="subject does not name")

    def test_attach_rejects_a_case_with_a_rewritten_standard(self):
        deal_id = self.delivered_deal()
        for standard in ("", "The buyer always wins.", TERMS + " ", TERMS.lower()):
            case_id = self.install_case(deal_id, standard=standard)
            self.reverts(BUYER, "attach_case", deal_id, case_id, contains="standard must equal")

    def test_attach_needs_enough_response_time_left_for_the_respondent(self):
        deal_id = self.delivered_deal()
        minimum = self.mod.MIN_RESPONSE_TIME_LEFT
        for window in (60, 3600, minimum - 1):
            case_id = self.install_case(deal_id, response_window=window)
            self.reverts(BUYER, "attach_case", deal_id, case_id, contains="would have only")
        case_id = self.install_case(deal_id, response_window=minimum)
        self.assertEqual(self.call(BUYER, "attach_case", deal_id, case_id)["status"], "DISPUTED")
        self.assertEqual(j(self.escrow.get_config())["min_response_time_left"], minimum)

    def test_a_case_opened_early_cannot_be_used_to_ambush_the_respondent(self):
        as_(BUYER, 5 * GEN)
        deal_id = j(self.escrow.open_deal(SELLER, TERMS, 10 * DAY))["deal_id"]
        early = self.install_case(deal_id, response_window=DAY)
        stub.advance(2 * DAY)
        as_(SELLER)
        self.escrow.mark_delivered(deal_id, "late but in time")
        self.reverts(BUYER, "attach_case", deal_id, early, contains="would have only")
        fresh = self.install_case(deal_id, response_window=DAY)
        self.assertEqual(self.call(BUYER, "attach_case", deal_id, fresh)["status"], "DISPUTED")

    def test_attach_rejects_a_case_opened_before_the_deal_existed(self):
        stub.advance(100)
        deal_id = self.delivered_deal()
        stale = self.install_case(deal_id, opened_epoch=stub.CTX.now - 1)
        self.reverts(BUYER, "attach_case", deal_id, stale, contains="before this deal existed")
        fresh = self.install_case(deal_id, opened_epoch=stub.CTX.now)
        self.assertEqual(self.call(BUYER, "attach_case", deal_id, fresh)["status"], "DISPUTED")

    def test_attach_only_accepts_a_case_that_has_not_been_ruled_yet(self):
        deal_id = self.delivered_deal()
        for phase in ("RULED", "FINAL"):
            case_id = self.install_case(deal_id, phase=phase, share=100)
            self.reverts(BUYER, "attach_case", deal_id, case_id, contains="before it is ruled")
        withdrawn = self.install_case(deal_id, phase="WITHDRAWN")
        self.reverts(BUYER, "attach_case", deal_id, withdrawn, contains="withdrawn")
        for phase in ("", "WHATEVER", "UNKNOWN"):
            case_id = self.install_case(deal_id, phase=phase)
            self.reverts(BUYER, "attach_case", deal_id, case_id, contains="before it is ruled")
        open_case = self.install_case(deal_id, phase="OPEN")
        self.assertEqual(self.call(BUYER, "attach_case", deal_id, open_case)["status"], "DISPUTED")

    def test_once_disputed_a_second_case_cannot_replace_the_first(self):
        deal_id, first = self.disputed_deal()
        second = self.install_case(deal_id)
        self.reverts(BUYER, "attach_case", deal_id, second, contains="DISPUTED")
        self.reverts(SELLER, "attach_case", deal_id, second, contains="DISPUTED")
        self.assertEqual(self.deal(deal_id)["case_id"], first)

    def test_one_case_cannot_serve_two_deals(self):
        deal_a, case_a = self.disputed_deal()
        deal_b = self.delivered_deal()
        self.reverts(BUYER, "attach_case", deal_b, case_a, contains="subject does not name")

    # ------------------------------------------------------------------ #
    # settle_deal
    # ------------------------------------------------------------------ #

    def test_settle_applies_the_final_share_when_the_claimant_is_the_buyer(self):
        deal_id, case_id = self.disputed_deal()
        self.set_case(case_id, phase="FINAL", claimant_share=80)
        out = self.call(OUTSIDER, "settle_deal", deal_id)
        buyer_amount, seller_net, fee = self.split(5 * GEN, 80)
        self.assertEqual((out["status"], out["resolution"], out["buyer_pct"]), ("SETTLED", "RULING", 80))
        self.assertEqual((out["buyer_amount"], out["seller_amount"], out["fee"]),
            (str(buyer_amount), str(seller_net), str(fee)))
        self.assertEqual(self.escrow.get_claimable(BUYER), str(buyer_amount))
        self.assertEqual(self.escrow.get_claimable(SELLER), str(seller_net))
        self.assertEqual(j(self.escrow.get_platform_stats())["settled"], 1)
        self.assertEqual(j(self.escrow.get_seller_exposure(SELLER))["open_disputes"], 0)

    def test_settle_flips_the_share_when_the_claimant_is_the_seller(self):
        deal_id, case_id = self.disputed_deal(claimant=SELLER, respondent=BUYER)
        self.set_case(case_id, phase="FINAL", claimant_share=100)
        out = self.call(OUTSIDER, "settle_deal", deal_id)
        self.assertEqual(out["buyer_pct"], 0)
        self.assertEqual(self.escrow.get_claimable(BUYER), "0")
        self.set_case(case_id, claimant_share=0)
        deal2, case2 = self.disputed_deal(claimant=SELLER, respondent=BUYER)
        self.set_case(case2, phase="FINAL", claimant_share=0)
        self.assertEqual(self.call(OUTSIDER, "settle_deal", deal2)["buyer_pct"], 100)

    def test_settle_extremes(self):
        deal_id, case_id = self.disputed_deal()
        self.set_case(case_id, phase="FINAL", claimant_share=100)
        out = self.call(OUTSIDER, "settle_deal", deal_id)
        self.assertEqual((out["buyer_amount"], out["seller_amount"], out["fee"]), (str(5 * GEN), "0", "0"))
        other, other_case = self.disputed_deal(buyer=OTHER_BUYER)
        self.set_case(other_case, phase="FINAL", claimant_share=0)
        out = self.call(OUTSIDER, "settle_deal", other)
        self.assertEqual(out["buyer_amount"], "0")

    def test_settle_waits_for_the_final_ruling(self):
        deal_id, case_id = self.disputed_deal()
        self.reverts(OUTSIDER, "settle_deal", deal_id, contains="OPEN on the bench")
        self.set_case(case_id, phase="RULED", claimant_share=90)
        self.reverts(OUTSIDER, "settle_deal", deal_id, contains="RULED on the bench")
        self.assertEqual(self.deal(deal_id)["status"], "DISPUTED")
        self.set_case(case_id, phase="FINAL")
        self.assertEqual(self.call(OUTSIDER, "settle_deal", deal_id)["status"], "SETTLED")

    def test_settle_twice_and_settle_of_non_disputed_deals_raise(self):
        deal_id, case_id = self.disputed_deal()
        self.set_case(case_id, phase="FINAL", claimant_share=50)
        self.call(OUTSIDER, "settle_deal", deal_id)
        self.reverts(OUTSIDER, "settle_deal", deal_id, contains="not DISPUTED")
        funded = self.new_deal()
        self.reverts(OUTSIDER, "settle_deal", funded, contains="not DISPUTED")
        self.reverts(OUTSIDER, "settle_deal", 404, contains="unknown deal_id")

    def test_settle_refuses_nonsense_from_the_bench(self):
        deal_id, case_id = self.disputed_deal()
        for share in (-1, 101, 5000):
            self.set_case(case_id, phase="FINAL", claimant_share=share)
            self.reverts(OUTSIDER, "settle_deal", deal_id, contains="invalid share")
        self.set_case(case_id, phase="FINAL", claimant_share=50, claimant=OUTSIDER)
        self.reverts(OUTSIDER, "settle_deal", deal_id, contains="not a party")
        self.fake._raw[case_id] = "garbage"
        self.reverts(OUTSIDER, "settle_deal", deal_id, contains="unreadable")
        del self.fake._raw[case_id]
        self.fake._briefs.pop(case_id)
        self.reverts(OUTSIDER, "settle_deal", deal_id, contains="case not found")
        self.assertEqual(self.deal(deal_id)["status"], "DISPUTED")

    def test_a_withdrawn_case_returns_the_deal_to_delivered(self):
        deal_id, case_id = self.disputed_deal()
        self.set_case(case_id, phase="WITHDRAWN")
        out = self.call(OUTSIDER, "settle_deal", deal_id)
        self.assertEqual(out["status"], "DELIVERED")
        deal = self.deal(deal_id)
        self.assertEqual((deal["status"], deal["case_id"], deal["case_attached_epoch"]), ("DELIVERED", 0, 0))
        self.assertEqual(j(self.escrow.get_seller_exposure(SELLER))["open_disputes"], 0)
        # a fresh case can be attached while the review window is still open ...
        fresh = self.install_case(deal_id)
        self.assertEqual(self.call(BUYER, "attach_case", deal_id, fresh)["status"], "DISPUTED")
        self.assertEqual(j(self.escrow.get_seller_exposure(SELLER))["open_disputes"], 1)

    def test_a_seller_who_withdraws_a_bound_case_cannot_run_out_the_buyers_clock(self):
        deal_id, case_id = self.disputed_deal(claimant=SELLER, respondent=BUYER)
        stub.advance(2 * DAY)  # well past the review deadline
        self.set_case(case_id, phase="WITHDRAWN")
        self.call(OUTSIDER, "settle_deal", deal_id)
        deal = self.deal(deal_id)
        self.assertEqual(deal["status"], "DELIVERED")
        self.assertEqual(deal["review_deadline"], stub.CTX.now + DAY, "the buyer gets a fresh review window")
        self.reverts(OUTSIDER, "release_after_review", deal_id, contains="still open")
        fresh = self.install_case(deal_id, claimant=SELLER, respondent=BUYER)
        self.reverts(SELLER, "attach_case", deal_id, fresh, contains="only the other party")
        mine = self.install_case(deal_id)
        self.assertEqual(self.call(BUYER, "attach_case", deal_id, mine)["status"], "DISPUTED")

    def test_a_seller_blocked_after_withdrawing_still_gets_paid_if_the_buyer_stays_silent(self):
        deal_id, case_id = self.disputed_deal(claimant=SELLER, respondent=BUYER)
        self.set_case(case_id, phase="WITHDRAWN")
        self.call(OUTSIDER, "settle_deal", deal_id)
        stub.advance(DAY)
        self.assertEqual(self.call(OUTSIDER, "release_after_review", deal_id)["status"], "RELEASED")

    def test_a_buyer_who_withdraws_cannot_stretch_the_review_window(self):
        deal_id, case_id = self.disputed_deal()
        original_deadline = self.deal(deal_id)["review_deadline"]
        stub.advance(2 * DAY)
        self.set_case(case_id, phase="WITHDRAWN")
        self.call(OUTSIDER, "settle_deal", deal_id)
        self.assertEqual(self.deal(deal_id)["review_deadline"], original_deadline)
        self.assertEqual(self.call(OUTSIDER, "release_after_review", deal_id)["status"], "RELEASED")

    def test_after_a_withdrawn_case_the_normal_clock_applies_again(self):
        deal_id, case_id = self.disputed_deal()
        self.set_case(case_id, phase="WITHDRAWN")
        self.call(OUTSIDER, "settle_deal", deal_id)
        stub.advance(DAY)
        self.assertEqual(self.call(OUTSIDER, "release_after_review", deal_id)["status"], "RELEASED")

    def test_an_unruled_case_falls_back_to_an_even_split_after_the_grace_period(self):
        deal_id, case_id = self.disputed_deal()
        grace = self.mod.DISPUTE_GRACE_SECONDS
        stub.advance(grace - 1)
        self.reverts(OUTSIDER, "settle_deal", deal_id, contains="OPEN on the bench")
        stub.advance(1)
        out = self.call(OUTSIDER, "settle_deal", deal_id)
        buyer_amount, seller_net, fee = self.split(5 * GEN, 50)
        self.assertEqual((out["resolution"], out["buyer_pct"], out["buyer_amount"], out["seller_amount"]),
            ("DEFAULT_SPLIT", 50, str(buyer_amount), str(seller_net)))
        self.assertEqual(j(self.escrow.get_seller_exposure(SELLER))["last_adverse_epoch"], 0)

    def test_grace_period_does_not_override_a_ruled_case(self):
        deal_id, case_id = self.disputed_deal()
        self.set_case(case_id, phase="RULED", claimant_share=100)
        stub.advance(self.mod.DISPUTE_GRACE_SECONDS + 5)
        self.reverts(OUTSIDER, "settle_deal", deal_id, contains="RULED on the bench")
        self.set_case(case_id, phase="FINAL")
        self.assertEqual(self.call(OUTSIDER, "settle_deal", deal_id)["buyer_pct"], 100)

    def test_unknown_bench_phases_never_settle_anything(self):
        deal_id, case_id = self.disputed_deal()
        for phase in ("", "UNKNOWN", "final", "SETTLED"):
            self.set_case(case_id, phase=phase)
            self.reverts(OUTSIDER, "settle_deal", deal_id)
        self.assertEqual(self.deal(deal_id)["status"], "DISPUTED")

    def test_seller_exposure_tracks_adverse_rulings_only(self):
        e = self.escrow
        self.assertEqual(j(e.get_seller_exposure(SELLER)), {"seller": SELLER, "live_deals": 0,
            "open_disputes": 0, "last_adverse_epoch": 0})
        deal_id, case_id = self.disputed_deal()
        self.set_case(case_id, phase="FINAL", claimant_share=0)
        self.call(OUTSIDER, "settle_deal", deal_id)
        self.assertEqual(j(e.get_seller_exposure(SELLER))["last_adverse_epoch"], 0, "a seller win is not adverse")
        stub.advance(500)
        other, other_case = self.disputed_deal(buyer=OTHER_BUYER)
        self.set_case(other_case, phase="FINAL", claimant_share=5)
        self.call(OUTSIDER, "settle_deal", other)
        self.assertEqual(j(e.get_seller_exposure(SELLER))["last_adverse_epoch"], stub.CTX.now)
        with self.assertRaises(stub.UserError):
            e.get_seller_exposure("nope")

    def test_live_deals_cover_every_unresolved_stage_and_end_with_the_deal(self):
        e = self.escrow

        def live():
            return j(e.get_seller_exposure(SELLER))["live_deals"]

        self.assertEqual(live(), 0)
        funded = self.new_deal()
        self.assertEqual(live(), 1)
        delivered = self.delivered_deal(buyer=OTHER_BUYER)
        disputed, case_id = self.disputed_deal(buyer=OUTSIDER)
        self.assertEqual(live(), 3)
        # a different seller's deals are not counted
        self.new_deal(seller=OTHER_SELLER)
        self.assertEqual(live(), 3)
        self.call(SELLER, "refund_buyer", funded)
        self.assertEqual(live(), 2)
        self.call(OTHER_BUYER, "confirm_receipt", delivered)
        self.assertEqual(live(), 1)
        self.set_case(case_id, phase="WITHDRAWN")
        self.call(OUTSIDER, "settle_deal", disputed)
        self.assertEqual(live(), 1, "a withdrawn case leaves the deal unresolved")
        stub.advance(DAY)
        self.call(OUTSIDER, "release_after_review", disputed)
        self.assertEqual(live(), 0)

    def test_undelivered_deals_end_through_reclaim_and_stop_counting(self):
        deal_id = self.new_deal(window=2 * MINUTE)
        stub.advance(3 * MINUTE)
        self.call(OUTSIDER, "reclaim_undelivered", deal_id)
        self.assertEqual(j(self.escrow.get_seller_exposure(SELLER))["live_deals"], 0)

    def test_a_flood_of_dust_deals_can_be_cleared_by_the_seller(self):
        for i in range(5):
            self.new_deal(price=self.mod.MIN_PRICE, buyer=OUTSIDER)
        self.assertEqual(j(self.escrow.get_seller_exposure(SELLER))["live_deals"], 5)
        for deal_id in range(1, 6):
            self.call(SELLER, "refund_buyer", deal_id)
        self.assertEqual(j(self.escrow.get_seller_exposure(SELLER))["live_deals"], 0)

    def test_two_open_disputes_for_one_seller_are_counted_separately(self):
        d1, c1 = self.disputed_deal()
        d2, c2 = self.disputed_deal(buyer=OTHER_BUYER)
        self.assertEqual(j(self.escrow.get_seller_exposure(SELLER))["open_disputes"], 2)
        self.set_case(c1, phase="FINAL", claimant_share=10)
        self.call(OUTSIDER, "settle_deal", d1)
        self.assertEqual(j(self.escrow.get_seller_exposure(SELLER))["open_disputes"], 1)
        self.set_case(c2, phase="WITHDRAWN")
        self.call(OUTSIDER, "settle_deal", d2)
        self.assertEqual(j(self.escrow.get_seller_exposure(SELLER))["open_disputes"], 0)

    # ------------------------------------------------------------------ #
    # withdraw / fees
    # ------------------------------------------------------------------ #

    def test_withdraw_pays_the_claimable_balance_once(self):
        deal_id, case_id = self.disputed_deal()
        self.set_case(case_id, phase="FINAL", claimant_share=80)
        self.call(OUTSIDER, "settle_deal", deal_id)
        buyer_amount, seller_net, fee = self.split(5 * GEN, 80)
        self.assertEqual(self.call(BUYER, "withdraw")["withdrawn"], str(buyer_amount))
        self.assertEqual(stub.paid_to(BUYER), buyer_amount)
        self.reverts(BUYER, "withdraw", contains="nothing to withdraw")
        self.assertEqual(self.escrow.get_claimable(BUYER), "0")
        self.assertEqual(self.call(SELLER, "withdraw")["withdrawn"], str(seller_net))
        stats = j(self.escrow.get_platform_stats())
        self.assertEqual(stats["total_paid_out"], str(buyer_amount + seller_net))
        self.assertEqual(stub.balance_of(ESCROW_ADDR), fee)

    def test_withdraw_with_nothing_owed_raises_and_changes_nothing(self):
        self.new_deal()
        for who in (OUTSIDER, SELLER, BUYER):
            self.reverts(who, "withdraw", contains="nothing to withdraw")
        self.assertEqual(stub.PAYMENTS, [])

    def test_withdraw_cannot_touch_an_unresolved_deals_price(self):
        self.new_deal()
        self.reverts(BUYER, "withdraw", contains="nothing to withdraw")
        self.assertEqual(stub.balance_of(ESCROW_ADDR), 5 * GEN)

    def test_fees_are_withdrawable_by_the_owner_only_and_only_up_to_what_accrued(self):
        deal_id = self.delivered_deal()
        self.call(BUYER, "confirm_receipt", deal_id)
        fee = self.split(5 * GEN, 0)[2]
        self.assertEqual(j(self.escrow.get_platform_stats())["fees_available"], str(fee))
        for who in (BUYER, SELLER, OUTSIDER):
            self.reverts(who, "withdraw_fees", OUTSIDER, 1, contains="not the owner")
        for bad in (0, -1, fee + 1):
            self.reverts(OWNER, "withdraw_fees", OWNER, bad, contains="amount must be")
        self.reverts(OWNER, "withdraw_fees", "nope", 1, contains="to must be")
        self.reverts(OWNER, "withdraw_fees", "0x" + "00" * 20, 1, contains="to must be")
        as_(OWNER)
        self.escrow.withdraw_fees(OWNER, fee // 2)
        self.assertEqual(stub.paid_to(OWNER), fee // 2)
        self.reverts(OWNER, "withdraw_fees", OWNER, fee - fee // 2 + 1, contains="amount must be")
        as_(OWNER)
        self.escrow.withdraw_fees(OWNER, fee - fee // 2)
        self.assertEqual(stub.paid_to(OWNER), fee)
        self.reverts(OWNER, "withdraw_fees", OWNER, 1, contains="amount must be")

    def test_the_owner_can_never_reach_escrowed_money(self):
        self.new_deal()
        self.new_deal(buyer=OTHER_BUYER)
        self.reverts(OWNER, "withdraw_fees", OWNER, 1, contains="amount must be")
        deal_id = self.delivered_deal(buyer=OUTSIDER)
        self.call(OUTSIDER, "confirm_receipt", deal_id)
        fee = self.split(5 * GEN, 0)[2]
        self.reverts(OWNER, "withdraw_fees", OWNER, fee + 1, contains="amount must be")
        # the seller's unclaimed proceeds sit in the balance too, but are not fees
        self.assertEqual(stub.balance_of(ESCROW_ADDR), 15 * GEN - 0)

    def test_the_fee_is_fixed_when_the_deal_is_funded(self):
        first = self.delivered_deal()
        as_(OWNER)
        self.escrow.set_fee_bps(500)
        second = self.delivered_deal(buyer=OTHER_BUYER)
        as_(OWNER)
        self.escrow.set_fee_bps(0)
        third = self.delivered_deal(buyer=OUTSIDER)
        for deal_id, buyer in ((first, BUYER), (second, OTHER_BUYER), (third, OUTSIDER)):
            self.call(buyer, "confirm_receipt", deal_id)
        self.assertEqual(self.deal(first)["fee"], str(5 * GEN * 250 // 10000))
        self.assertEqual(self.deal(second)["fee"], str(5 * GEN * 500 // 10000))
        self.assertEqual(self.deal(third)["fee"], "0")
        self.assertEqual((self.deal(first)["fee_bps"], self.deal(second)["fee_bps"]), (250, 500))

    # ------------------------------------------------------------------ #
    # admin
    # ------------------------------------------------------------------ #

    def test_admin_functions_are_owner_only(self):
        for who in (BUYER, SELLER, OUTSIDER):
            self.reverts(who, "set_paused", True, contains="not the owner")
            self.reverts(who, "set_fee_bps", 10, contains="not the owner")
            self.reverts(who, "set_review_window", 120, contains="not the owner")
            self.reverts(who, "transfer_ownership", who, contains="not the owner")

    def test_admin_bounds(self):
        for bad in (-1, 501):
            self.reverts(OWNER, "set_fee_bps", bad, contains="fee_bps")
        for bad in (0, 59, 14 * DAY + 1):
            self.reverts(OWNER, "set_review_window", bad, contains="review window")
        for bad in ("nope", "", "0x" + "00" * 20):
            self.reverts(OWNER, "transfer_ownership", bad, contains="new_owner")

    def test_transfer_ownership_moves_every_admin_power(self):
        as_(OWNER)
        self.escrow.transfer_ownership(OUTSIDER)
        self.reverts(OWNER, "set_paused", True, contains="not the owner")
        as_(OUTSIDER)
        self.escrow.set_paused(True)
        self.assertTrue(j(self.escrow.get_config())["paused"])

    def test_the_bench_pointer_cannot_be_changed_by_anyone(self):
        self.assertFalse(hasattr(self.mod.DealEscrow, "set_bench"))
        self.assertFalse(hasattr(self.mod.DealEscrow, "set_bench_address"))

    def test_pause_never_freezes_existing_deals(self):
        deal_id, case_id = self.disputed_deal()
        other = self.delivered_deal(buyer=OTHER_BUYER)
        as_(OWNER)
        self.escrow.set_paused(True)
        self.set_case(case_id, phase="FINAL", claimant_share=60)
        self.assertEqual(self.call(OUTSIDER, "settle_deal", deal_id)["status"], "SETTLED")
        self.assertEqual(self.call(OTHER_BUYER, "confirm_receipt", other)["status"], "RELEASED")
        self.assertTrue(self.call(BUYER, "withdraw")["ok"])

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #

    def test_unknown_deal_views(self):
        self.assertEqual(j(self.escrow.get_deal(5)), {"found": False})
        self.assertEqual(j(self.escrow.get_deal_brief(5)), {"found": False})
        with self.assertRaises(stub.UserError):
            self.escrow.get_dispute_template(5)

    def test_deal_brief_is_the_narrow_downstream_surface(self):
        deal_id, case_id = self.disputed_deal()
        self.set_case(case_id, phase="FINAL", claimant_share=80)
        self.call(OUTSIDER, "settle_deal", deal_id)
        brief = j(self.escrow.get_deal_brief(deal_id))
        self.assertEqual(sorted(brief), sorted(["found", "deal_id", "buyer", "seller", "price", "status",
            "resolution", "buyer_pct", "buyer_amount", "resolved_epoch"]))
        self.assertEqual((brief["status"], brief["resolution"], brief["buyer_pct"]), ("SETTLED", "RULING", 80))
        self.assertEqual(brief["buyer_amount"], str(self.split(5 * GEN, 80)[0]))

    def test_dispute_template_is_exactly_what_attach_expects(self):
        deal_id = self.delivered_deal()
        template = j(self.escrow.get_dispute_template(deal_id))
        self.assertEqual(template["subject"], "deal:" + ESCROW_ADDR + ":" + str(deal_id))
        self.assertEqual((template["standard"], template["bench"], template["buyer"], template["seller"]),
            (TERMS, BENCH_ADDR, BUYER, SELLER))

    def test_get_deals_by_party_pages_and_validates(self):
        for i in range(5):
            self.new_deal(price=GEN + i)
        page = j(self.escrow.get_deals_by_party(BUYER, 1, 2))
        self.assertEqual((page["total"], [d["deal_id"] for d in page["deals"]]), (5, [2, 3]))
        self.assertEqual([d["deal_id"] for d in j(self.escrow.get_deals_by_party(SELLER, 3, 500))["deals"]],
            [4, 5])
        self.assertEqual(j(self.escrow.get_deals_by_party(OUTSIDER)), {"party": OUTSIDER, "total": 0, "deals": []})
        self.assertEqual(len(j(self.escrow.get_deals_by_party(BUYER, -9, 0))["deals"]), 1)
        with self.assertRaises(stub.UserError):
            self.escrow.get_deals_by_party("nope")

    def test_get_claimable_validates_the_address(self):
        self.assertEqual(self.escrow.get_claimable(OUTSIDER), "0")
        with self.assertRaises(stub.UserError):
            self.escrow.get_claimable("nope")

    def test_platform_stats_agree_with_the_ledger(self):
        deal_id = self.delivered_deal()
        self.call(BUYER, "confirm_receipt", deal_id)
        self.call(SELLER, "withdraw")
        stats = j(self.escrow.get_platform_stats())
        buyer_amount, seller_net, fee = self.split(5 * GEN, 0)
        self.assertEqual((stats["released"], stats["total_volume"], stats["total_paid_out"], stats["fees_accrued"]),
            (1, str(5 * GEN), str(seller_net), str(fee)))
        self.assertEqual(stats["on_chain_balance"], str(fee))

    def test_config_reports_the_tunables(self):
        cfg = j(self.escrow.get_config())
        self.assertEqual((cfg["fee_bps"], cfg["max_fee_bps"], cfg["review_window"], cfg["bench"]),
            (250, 500, DAY, BENCH_ADDR))
        self.assertEqual(cfg["dispute_grace_seconds"], 30 * DAY)

    # ------------------------------------------------------------------ #
    # randomized lifecycle: the invariants must hold after every step
    # ------------------------------------------------------------------ #

    def test_random_operations_keep_the_ledger_solvent(self):
        for seed in range(6):
            self.setUp()
            rng = random.Random(seed)
            buyers = [BUYER, OTHER_BUYER, OUTSIDER]
            sellers = [SELLER, OTHER_SELLER]
            cases = {}
            for step in range(250):
                action = rng.choice(["open", "open", "deliver", "confirm", "release", "reclaim", "refund",
                    "attach", "attach", "ruling", "settle", "settle", "withdraw", "withdraw", "fees",
                    "wait", "config"])
                ids = list(range(1, int(self.escrow.next_deal_id) + 1))
                deal_id = rng.choice(ids) if ids else 1
                deal = self.deal(deal_id) if ids else None
                try:
                    if action == "open":
                        as_(rng.choice(buyers), rng.choice([10 ** 15, 10 ** 18 + 3, 7 * 10 ** 17, 25 * GEN]))
                        self.escrow.open_deal(rng.choice(sellers), TERMS, rng.choice([120, HOUR, DAY]))
                    elif deal is None:
                        continue
                    elif action == "deliver":
                        as_(deal["seller"])
                        self.escrow.mark_delivered(deal_id, "x")
                    elif action == "confirm":
                        as_(deal["buyer"])
                        self.escrow.confirm_receipt(deal_id)
                    elif action == "release":
                        as_(OUTSIDER)
                        self.escrow.release_after_review(deal_id)
                    elif action == "reclaim":
                        as_(OUTSIDER)
                        self.escrow.reclaim_undelivered(deal_id)
                    elif action == "refund":
                        as_(deal["seller"])
                        self.escrow.refund_buyer(deal_id)
                    elif action == "attach":
                        claimant = rng.choice([deal["buyer"], deal["seller"]])
                        respondent = deal["seller"] if claimant == deal["buyer"] else deal["buyer"]
                        case_id = self.install_case(deal_id, claimant, respondent)
                        cases[case_id] = deal_id
                        as_(rng.choice([deal["buyer"], deal["seller"]]))
                        self.escrow.attach_case(deal_id, case_id)
                    elif action == "ruling" and cases:
                        case_id = rng.choice(list(cases))
                        phase = rng.choice(["OPEN", "RULED", "FINAL", "FINAL", "WITHDRAWN"])
                        self.set_case(case_id, phase=phase, claimant_share=rng.choice(range(0, 101, 5)))
                    elif action == "settle":
                        as_(OUTSIDER)
                        self.escrow.settle_deal(deal_id)
                    elif action == "withdraw":
                        as_(rng.choice(buyers + sellers))
                        self.escrow.withdraw()
                    elif action == "fees":
                        fees_left = int(self.escrow.fees_accrued) - int(self.escrow.fees_withdrawn)
                        if fees_left > 0:
                            as_(OWNER)
                            self.escrow.withdraw_fees(OWNER, rng.randint(1, fees_left))
                    elif action == "wait":
                        stub.advance(rng.choice([30, HOUR, DAY, 5 * DAY, 31 * DAY]))
                    elif action == "config":
                        as_(OWNER)
                        self.escrow.set_fee_bps(rng.choice([0, 100, 250, 500]))
                except stub.UserError:
                    pass
                self.assert_invariants()
            self.tearDown()


if __name__ == "__main__":
    unittest.main()
