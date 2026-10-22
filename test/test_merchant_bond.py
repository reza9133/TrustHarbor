"""
Offline tests for contracts/MerchantBond.py, run against a stand-in escrow so
that every deal brief and exposure report the bond can receive is under the
test's control.

After every test the solvency invariant is checked: the contract's balance
equals total_bonded, which equals the sum of every seller's bonded amount.

Run with:  python3 -m unittest discover -s test -v
"""
import random
import unittest

import genlayer_stub as stub
from support import (BOND_ADDR, BUYER, DAY, ESCROW_ADDR, GEN, HOUR, OTHER_BUYER, OTHER_SELLER, OUTSIDER, OWNER,
    SELLER, as_, deploy_fake_escrow, j, load_bond, make_deal_brief)


class BondTestCase(unittest.TestCase):
    PENALTY_BPS = 2000

    def setUp(self):
        stub.reset()
        self.mod = load_bond()
        self.fake = deploy_fake_escrow()
        self.bond = stub.deploy(self.mod.MerchantBond, BOND_ADDR, ESCROW_ADDR, self.PENALTY_BPS, DAY,
            sender=OWNER)

    def tearDown(self):
        b = self.bond
        total = sum(int(x.bonded) for x in b.bonds.values())
        self.assertEqual(int(b.total_bonded), total)
        self.assertEqual(stub.balance_of(BOND_ADDR), total, "balance must equal the sum of all bonds")
        for x in b.bonds.values():
            self.assertLessEqual(int(x.pending_unbond), int(x.bonded) + int(x.pending_unbond))
        self.assertEqual(int(b.total_bonded) + int(b.total_slashed) + int(b.total_withdrawn),
            self.deposited_total(), "everything ever deposited is bonded, slashed or withdrawn")

    # -- helpers -- #

    _deposited = 0

    def deposited_total(self):
        return self._deposited

    def post(self, seller=SELLER, amount=2 * GEN):
        as_(seller, amount)
        out = j(self.bond.post_bond())
        if out["ok"]:
            self._deposited += amount
        return out

    def install_deal(self, deal_id=1, buyer=BUYER, seller=SELLER, price=5 * GEN, **over):
        fields = dict(status="SETTLED", resolution="RULING", buyer_pct=80, resolved_epoch=stub.CTX.now)
        fields.update(over)
        self.fake._deals[deal_id] = make_deal_brief(deal_id, buyer, seller, price, **fields)
        return deal_id

    def set_exposure(self, seller=SELLER, live_deals=0, open_disputes=0, last_adverse_epoch=0):
        self.fake._exposure[seller.lower()] = {"live_deals": live_deals, "open_disputes": open_disputes,
            "last_adverse_epoch": last_adverse_epoch}

    def claim(self, who=BUYER, deal_id=1):
        as_(who)
        return j(self.bond.claim_compensation(deal_id))

    def reverts(self, who, method, *args, contains=""):
        as_(who)
        with self.assertRaises(stub.UserError) as ctx:
            getattr(self.bond, method)(*args)
        self.assertIn(contains, ctx.exception.message)

    def call(self, who, method, *args):
        as_(who)
        return j(getattr(self.bond, method)(*args))

    def info(self, who=SELLER):
        return j(self.bond.get_bond(who))

    # ------------------------------------------------------------------ #
    # constructor
    # ------------------------------------------------------------------ #

    def test_constructor_rejects_bad_arguments(self):
        for bad in ("", "nope", "0x1234", "0x" + "00" * 20, "0x" + "zz" * 20):
            with self.assertRaises(stub.UserError, msg=bad):
                stub.deploy(self.mod.MerchantBond, "0x" + "d9" * 20, bad, sender=OWNER)
        for bps in (0, -1, 5001):
            with self.assertRaises(stub.UserError, msg=str(bps)):
                stub.deploy(self.mod.MerchantBond, "0x" + "d9" * 20, ESCROW_ADDR, bps, DAY, sender=OWNER)
        for delay in (0, 59, 30 * DAY + 1):
            with self.assertRaises(stub.UserError, msg=str(delay)):
                stub.deploy(self.mod.MerchantBond, "0x" + "d9" * 20, ESCROW_ADDR, 100, delay, sender=OWNER)

    def test_constructor_edges_and_defaults(self):
        stub.deploy(self.mod.MerchantBond, "0xd1" + "00" * 19, ESCROW_ADDR, 1, 60, sender=OWNER)
        stub.deploy(self.mod.MerchantBond, "0xd2" + "00" * 19, ESCROW_ADDR, 5000, 30 * DAY, sender=OWNER)
        other = stub.deploy(self.mod.MerchantBond, "0xd3" + "00" * 19, ESCROW_ADDR, sender=OWNER)
        cfg = j(other.get_config())
        self.assertEqual((cfg["penalty_bps"], cfg["unbond_delay"]), (2000, 7 * DAY))
        self.assertEqual((cfg["owner"], cfg["escrow"]), (OWNER, ESCROW_ADDR))

    # ------------------------------------------------------------------ #
    # post_bond
    # ------------------------------------------------------------------ #

    def test_post_bond_accumulates_and_records_the_first_deposit_time(self):
        first = stub.CTX.now
        out = self.post(amount=GEN)
        self.assertEqual((out["ok"], out["bonded"], out["deposited"]), (True, str(GEN), str(GEN)))
        stub.advance(500)
        out = self.post(amount=2 * GEN)
        self.assertEqual(out["bonded"], str(3 * GEN))
        info = self.info()
        self.assertEqual((info["bonded"], info["first_bonded_epoch"]), (str(3 * GEN), first))
        stats = j(self.bond.get_stats())
        self.assertEqual((stats["total_bonded"], stats["deposits"]), (str(3 * GEN), 2))

    def test_each_seller_has_their_own_bond(self):
        self.post(SELLER, GEN)
        self.post(OTHER_SELLER, 2 * GEN)
        self.assertEqual(self.info(SELLER)["bonded"], str(GEN))
        self.assertEqual(self.info(OTHER_SELLER)["bonded"], str(2 * GEN))

    def test_post_bond_refunds_instead_of_raising(self):
        for amount, fragment in ((self.mod.MIN_BOND_DEPOSIT - 1, "below minimum"),
                (self.mod.MAX_BOND_DEPOSIT + 1, "above maximum"), (0, "below minimum")):
            before = stub.paid_to(SELLER)
            out = self.post(amount=amount)
            self.assertFalse(out["ok"])
            self.assertIn(fragment, out["reason"])
            self.assertEqual(out["refunded"], str(amount))
            self.assertEqual(stub.paid_to(SELLER) - before, amount)
        self.assertEqual(self.info()["bonded"], "0")
        self.assertEqual(stub.balance_of(BOND_ADDR), 0)

    def test_post_bond_accepts_the_exact_edges(self):
        self.assertTrue(self.post(amount=self.mod.MIN_BOND_DEPOSIT)["ok"])
        self.assertTrue(self.post(OTHER_SELLER, self.mod.MAX_BOND_DEPOSIT)["ok"])

    def test_paused_bond_refunds_new_deposits_only(self):
        self.post(amount=GEN)
        as_(OWNER)
        self.bond.set_paused(True)
        out = self.post(amount=GEN)
        self.assertFalse(out["ok"])
        self.assertIn("paused", out["reason"])
        self.assertEqual(self.info()["bonded"], str(GEN))
        # everything else keeps working while paused
        self.assertTrue(self.call(SELLER, "request_unbond", GEN // 2)["ok"])
        self.assertTrue(self.call(SELLER, "cancel_unbond")["ok"])
        as_(OWNER)
        self.bond.set_paused(False)
        self.assertTrue(self.post(amount=GEN)["ok"])

    def test_post_bond_fuzz_never_raises_and_never_loses_money(self):
        rng = random.Random(77)
        refunded = 0
        for _ in range(300):
            amount = rng.choice([0, 1, 10 ** 16 - 1, 10 ** 16, 10 ** 18, 1000 * 10 ** 18, 1000 * 10 ** 18 + 1])
            out = self.post(rng.choice([SELLER, OTHER_SELLER, OUTSIDER]), amount)
            if not out["ok"]:
                self.assertEqual(out["refunded"], str(amount))
                refunded += amount
        self.assertEqual(stub.paid_to(SELLER) + stub.paid_to(OTHER_SELLER) + stub.paid_to(OUTSIDER), refunded)

    # ------------------------------------------------------------------ #
    # claim_compensation
    # ------------------------------------------------------------------ #

    def test_claim_slashes_a_share_of_what_the_buyer_was_awarded(self):
        self.post(amount=2 * GEN)
        self.install_deal(price=5 * GEN, buyer_pct=80)
        out = self.claim()
        penalty = 4 * GEN * 2000 // 10000
        self.assertEqual((out["penalty"], out["paid"], out["shortfall"]), (str(penalty), str(penalty), "0"))
        self.assertEqual(stub.paid_to(BUYER), penalty)
        info = self.info()
        self.assertEqual((info["bonded"], info["strikes"], info["total_slashed"]),
            (str(2 * GEN - penalty), 1, str(penalty)))
        stats = j(self.bond.get_stats())
        self.assertEqual((stats["total_slashed"], stats["claims"]), (str(penalty), 1))

    def test_each_deal_can_be_claimed_exactly_once(self):
        self.post(amount=3 * GEN)
        self.install_deal()
        self.claim()
        self.reverts(BUYER, "claim_compensation", 1, contains="already claimed")
        self.assertEqual(self.info()["strikes"], 1)

    def test_a_bond_too_small_for_the_penalty_pays_what_it_has_and_the_claim_is_still_used_up(self):
        self.post(amount=3 * 10 ** 17)
        self.install_deal(price=5 * GEN, buyer_pct=80)
        out = self.claim()
        penalty = 4 * GEN * 2000 // 10000
        self.assertEqual((out["paid"], out["shortfall"]), (str(3 * 10 ** 17), str(penalty - 3 * 10 ** 17)))
        self.assertEqual(self.info()["bonded"], "0")
        self.assertEqual(stub.balance_of(BOND_ADDR), 0)
        self.post(amount=GEN)
        self.reverts(BUYER, "claim_compensation", 1, contains="already claimed")

    def test_claims_come_only_from_rulings_that_found_real_fault(self):
        self.post(amount=3 * GEN)
        cases = [dict(status="DELIVERED", resolution=""), dict(status="RELEASED", resolution="CONFIRMED"),
            dict(status="REFUNDED", resolution="SELLER_REFUND"), dict(status="SETTLED", resolution="DEFAULT_SPLIT"),
            dict(status="SETTLED", resolution="AUTO_RELEASE"), dict(status="DISPUTED", resolution="RULING"),
            dict(status="SETTLED", resolution="ruling")]
        for over in cases:
            self.install_deal(**over)
            self.reverts(BUYER, "claim_compensation", 1, contains="not settled by an arbitration ruling")
        for pct in (0, 5, 15, 19):
            self.install_deal(buyer_pct=pct)
            self.reverts(BUYER, "claim_compensation", 1, contains="less than 20%")
        self.install_deal(buyer_pct=20)
        self.assertTrue(self.claim()["ok"])

    def test_claims_expire_so_a_late_bond_cannot_be_slashed_for_an_old_deal(self):
        expiry = self.mod.CLAIM_EXPIRY_SECONDS
        self.post(amount=3 * GEN)
        self.install_deal(resolved_epoch=stub.CTX.now)
        stub.advance(expiry)
        self.assertTrue(j(self.bond.preview_claim(1, BUYER))["eligible"])
        stub.advance(1)
        self.reverts(BUYER, "claim_compensation", 1, contains="claim period for this deal has ended")
        self.assertFalse(j(self.bond.preview_claim(1, BUYER))["eligible"])
        self.assertEqual(self.info()["strikes"], 0)
        self.assertEqual(j(self.bond.get_config())["claim_expiry_seconds"], expiry)

    def test_a_claim_inside_the_expiry_still_works_after_the_exit_window(self):
        self.post(amount=3 * GEN)
        self.install_deal(resolved_epoch=stub.CTX.now)
        stub.advance(10 * DAY)
        self.assertTrue(self.claim()["ok"])

    def test_only_the_deals_buyer_can_claim(self):
        self.post(amount=3 * GEN)
        self.install_deal()
        for who in (SELLER, OUTSIDER, OTHER_BUYER, OWNER):
            self.reverts(who, "claim_compensation", 1, contains="only the deal's buyer")
        self.assertEqual(self.info()["strikes"], 0)
        self.assertTrue(self.claim()["ok"])

    def test_failed_claims_do_not_use_up_the_claim(self):
        self.install_deal()
        self.reverts(BUYER, "claim_compensation", 1, contains="no bond")
        self.post(amount=GEN)
        self.assertTrue(self.claim()["ok"])

    def test_claims_against_a_seller_with_nothing_bonded(self):
        self.install_deal()
        self.reverts(BUYER, "claim_compensation", 1, contains="no bond to claim")
        self.post(amount=GEN)
        self.call(SELLER, "request_unbond", GEN)
        stub.advance(DAY)
        self.set_exposure()
        self.call(SELLER, "withdraw_unbonded")
        self.reverts(BUYER, "claim_compensation", 1, contains="no bond to claim")

    def test_a_penalty_that_rounds_to_zero_is_refused(self):
        self.post(amount=GEN)
        self.install_deal(price=4, buyer_pct=100)
        self.reverts(BUYER, "claim_compensation", 1, contains="rounds down to zero")

    def test_claim_with_unknown_deal_or_garbage_from_the_escrow(self):
        self.reverts(BUYER, "claim_compensation", 9, contains="unknown deal_id")
        self.fake._raw[3] = "not json"
        self.reverts(BUYER, "claim_compensation", 3, contains="unreadable")
        self.fake._raw[4] = "[]"
        self.reverts(BUYER, "claim_compensation", 4, contains="unreadable")
        self.install_deal(deal_id=5, seller="not-an-address")
        self.reverts(BUYER, "claim_compensation", 5, contains="invalid seller")
        self.install_deal(deal_id=6)
        self.fake._deals[6]["buyer_amount"] = "lots"
        self.post(amount=GEN)
        as_(BUYER)
        with self.assertRaises(Exception):
            self.bond.claim_compensation(6)
        self.assertEqual(self.info()["strikes"], 0)

    def test_claim_is_never_blocked_by_a_pause(self):
        self.post(amount=GEN)
        self.install_deal()
        as_(OWNER)
        self.bond.set_paused(True)
        self.assertTrue(self.claim()["ok"])

    def test_the_claim_key_is_bound_to_the_escrow_address(self):
        key = self.mod._claim_key
        self.assertNotEqual(key(ESCROW_ADDR, 1), key("0x" + "99" * 20, 1))
        self.assertNotEqual(key(ESCROW_ADDR, 1), key(ESCROW_ADDR, 11))
        self.assertEqual(key(ESCROW_ADDR.upper().replace("0X", "0x"), 1), key(ESCROW_ADDR, 1))

    def test_slashing_hits_bond_that_is_waiting_to_unbond(self):
        self.post(amount=2 * GEN)
        self.call(SELLER, "request_unbond", 2 * GEN)
        self.install_deal()
        self.claim()
        info = self.info()
        self.assertEqual(info["bonded"], str(2 * GEN - 800000000000000000))
        self.assertEqual(info["effective_bond"], "0")
        stub.advance(DAY)
        self.set_exposure()
        out = self.call(SELLER, "withdraw_unbonded")
        self.assertEqual(out["withdrawn"], str(2 * GEN - 800000000000000000), "pays the smaller of pending and bonded")
        self.assertEqual(out["bonded"], "0")

    def test_two_claims_on_different_deals_accumulate_strikes(self):
        self.post(amount=5 * GEN)
        for deal_id in (1, 2, 3):
            self.install_deal(deal_id=deal_id, buyer=BUYER if deal_id != 2 else OTHER_BUYER)
        self.claim(BUYER, 1)
        self.claim(OTHER_BUYER, 2)
        self.claim(BUYER, 3)
        info = self.info()
        self.assertEqual(info["strikes"], 3)
        self.assertEqual(info["bonded"], str(5 * GEN - 3 * 800000000000000000))

    # ------------------------------------------------------------------ #
    # trust flag
    # ------------------------------------------------------------------ #

    def test_trust_needs_threshold_effective_bond_and_few_strikes(self):
        self.assertFalse(self.bond.is_trusted(SELLER))
        self.post(amount=GEN - self.mod.MIN_BOND_DEPOSIT)
        self.assertFalse(self.bond.is_trusted(SELLER))
        self.post(amount=self.mod.MIN_BOND_DEPOSIT)
        self.assertTrue(self.bond.is_trusted(SELLER))
        self.call(SELLER, "request_unbond", 1)
        self.assertFalse(self.bond.is_trusted(SELLER), "bond waiting to unbond does not count")
        self.call(SELLER, "cancel_unbond")
        self.assertTrue(self.bond.is_trusted(SELLER))

    def test_three_strikes_end_trust_even_with_a_huge_bond(self):
        self.post(amount=100 * GEN)
        for deal_id in (1, 2, 3):
            self.install_deal(deal_id=deal_id, price=GEN)
        self.claim(BUYER, 1)
        self.claim(BUYER, 2)
        self.assertTrue(self.bond.is_trusted(SELLER))
        self.claim(BUYER, 3)
        self.assertFalse(self.bond.is_trusted(SELLER))

    def test_trust_threshold_is_tunable_and_zero_still_needs_a_bond(self):
        self.post(amount=GEN // 2)
        self.assertFalse(self.bond.is_trusted(SELLER))
        as_(OWNER)
        self.bond.set_trust_threshold(GEN // 4)
        self.assertTrue(self.bond.is_trusted(SELLER))
        as_(OWNER)
        self.bond.set_trust_threshold(0)
        self.assertFalse(self.bond.is_trusted(OUTSIDER))
        self.assertFalse(self.bond.is_trusted("garbage"))
        self.assertFalse(self.bond.is_trusted("0x" + "00" * 20))

    # ------------------------------------------------------------------ #
    # unbonding
    # ------------------------------------------------------------------ #

    def test_unbond_flow_pays_out_after_the_delay(self):
        self.post(amount=3 * GEN)
        out = self.call(SELLER, "request_unbond", GEN)
        self.assertEqual((out["pending_unbond"], out["unlock_epoch"]), (str(GEN), stub.CTX.now + DAY))
        info = self.info()
        self.assertEqual((info["bonded"], info["effective_bond"]), (str(3 * GEN), str(2 * GEN)))
        self.reverts(SELLER, "withdraw_unbonded", contains="not elapsed")
        stub.advance(DAY - 1)
        self.reverts(SELLER, "withdraw_unbonded", contains="not elapsed")
        stub.advance(1)
        out = self.call(SELLER, "withdraw_unbonded")
        self.assertEqual((out["withdrawn"], out["bonded"]), (str(GEN), str(2 * GEN)))
        self.assertEqual(stub.paid_to(SELLER), GEN)
        info = self.info()
        self.assertEqual((info["pending_unbond"], info["unlock_epoch"]), ("0", 0))
        self.reverts(SELLER, "withdraw_unbonded", contains="no unbond request")
        stats = j(self.bond.get_stats())
        self.assertEqual((stats["total_withdrawn"], stats["unbonds"]), (str(GEN), 1))

    def test_request_unbond_guards(self):
        self.reverts(SELLER, "request_unbond", GEN, contains="no bond")
        self.post(amount=GEN)
        for bad in (0, -1, GEN + 1):
            self.reverts(SELLER, "request_unbond", bad, contains="amount must be")
        self.reverts(OUTSIDER, "request_unbond", 1, contains="no bond")
        self.assertTrue(self.call(SELLER, "request_unbond", GEN)["ok"])

    def test_a_new_request_replaces_the_old_one_and_restarts_the_clock(self):
        self.post(amount=3 * GEN)
        self.call(SELLER, "request_unbond", GEN)
        stub.advance(DAY - 10)
        out = self.call(SELLER, "request_unbond", 2 * GEN)
        self.assertEqual(out["pending_unbond"], str(2 * GEN))
        self.assertEqual(out["unlock_epoch"], stub.CTX.now + DAY)
        stub.advance(20)
        self.reverts(SELLER, "withdraw_unbonded", contains="not elapsed")

    def test_cancel_unbond(self):
        self.reverts(SELLER, "cancel_unbond", contains="no unbond request")
        self.post(amount=GEN)
        self.reverts(SELLER, "cancel_unbond", contains="no unbond request")
        self.call(SELLER, "request_unbond", GEN)
        out = self.call(SELLER, "cancel_unbond")
        self.assertEqual(out["bonded"], str(GEN))
        self.reverts(SELLER, "withdraw_unbonded", contains="no unbond request")

    def test_withdraw_unbonded_needs_a_request(self):
        self.reverts(OUTSIDER, "withdraw_unbonded", contains="no unbond request")
        self.post(amount=GEN)
        self.reverts(SELLER, "withdraw_unbonded", contains="no unbond request")

    def test_unresolved_deals_hold_the_bond_in_place(self):
        self.post(amount=GEN)
        self.call(SELLER, "request_unbond", GEN)
        stub.advance(DAY)
        self.set_exposure(live_deals=2)
        self.reverts(SELLER, "withdraw_unbonded", contains="2 unresolved deal(s)")
        self.set_exposure(live_deals=1, open_disputes=1)
        self.reverts(SELLER, "withdraw_unbonded", contains="1 unresolved deal(s)")
        self.set_exposure(live_deals=0)
        self.assertTrue(self.call(SELLER, "withdraw_unbonded")["ok"])

    def test_a_recent_adverse_ruling_holds_the_bond_for_the_claim_window(self):
        self.post(amount=GEN)
        self.call(SELLER, "request_unbond", GEN)
        stub.advance(DAY)
        window = self.mod.CLAIM_WINDOW_SECONDS
        adverse = stub.CTX.now - 100
        self.set_exposure(last_adverse_epoch=adverse)
        self.reverts(SELLER, "withdraw_unbonded", contains="may still claim")
        stub.advance(window - 101)
        self.reverts(SELLER, "withdraw_unbonded", contains="may still claim")
        stub.advance(1)
        self.assertTrue(self.call(SELLER, "withdraw_unbonded")["ok"])

    def test_an_old_adverse_ruling_does_not_hold_anything(self):
        self.post(amount=GEN)
        self.call(SELLER, "request_unbond", GEN)
        stub.advance(10 * DAY)
        self.set_exposure(last_adverse_epoch=stub.CTX.now - 9 * DAY)
        self.assertTrue(self.call(SELLER, "withdraw_unbonded")["ok"])

    def test_the_buyer_can_claim_inside_the_window_the_seller_cannot_dodge(self):
        self.post(amount=2 * GEN)
        self.call(SELLER, "request_unbond", 2 * GEN)
        stub.advance(DAY)
        self.install_deal(resolved_epoch=stub.CTX.now)
        self.set_exposure(last_adverse_epoch=stub.CTX.now)
        self.reverts(SELLER, "withdraw_unbonded", contains="may still claim")
        self.assertTrue(self.claim()["ok"])

    def test_unreadable_exposure_reports_block_the_withdrawal(self):
        self.post(amount=GEN)
        self.call(SELLER, "request_unbond", GEN)
        stub.advance(DAY)
        original = type(self.fake).get_seller_exposure
        self.addCleanup(setattr, type(self.fake), "get_seller_exposure", original)
        for payload in ("not json", "[1]", "{}", '{"live_deals": 0}', '{"last_adverse_epoch": 0}'):
            type(self.fake).get_seller_exposure = self._exposure_returning(payload)
            as_(SELLER)
            with self.assertRaises(stub.UserError, msg=payload):
                self.bond.withdraw_unbonded()
        self.assertEqual(self.info()["bonded"], str(GEN))

    def _exposure_returning(self, payload):
        @stub.gl.public.view
        def get_seller_exposure(self, seller: str) -> str:
            return payload
        return stub._make_public(get_seller_exposure)

    def test_a_failed_withdrawal_changes_nothing(self):
        self.post(amount=GEN)
        self.call(SELLER, "request_unbond", GEN)
        stub.advance(DAY)
        self.set_exposure(live_deals=1)
        before = self.info()
        self.reverts(SELLER, "withdraw_unbonded", contains="unresolved")
        self.assertEqual(self.info(), before)
        self.assertEqual(stub.PAYMENTS, [])

    # ------------------------------------------------------------------ #
    # admin
    # ------------------------------------------------------------------ #

    def test_admin_functions_are_owner_only(self):
        for who in (SELLER, BUYER, OUTSIDER):
            self.reverts(who, "set_paused", True, contains="not the owner")
            self.reverts(who, "set_trust_threshold", 1, contains="not the owner")
            self.reverts(who, "transfer_ownership", who, contains="not the owner")

    def test_admin_bounds_and_ownership_transfer(self):
        self.reverts(OWNER, "set_trust_threshold", -1, contains="threshold must be")
        self.reverts(OWNER, "set_trust_threshold", 10 ** 30 + 1, contains="threshold must be")
        self.reverts(OWNER, "set_trust_threshold", 2 ** 128, contains="threshold must be")
        for bad in ("nope", "", "0x" + "00" * 20):
            self.reverts(OWNER, "transfer_ownership", bad, contains="new_owner")
        as_(OWNER)
        self.bond.transfer_ownership(OUTSIDER)
        self.reverts(OWNER, "set_paused", True, contains="not the owner")
        as_(OUTSIDER)
        self.bond.set_paused(True)
        self.assertTrue(j(self.bond.get_config())["paused"])

    def test_there_is_no_admin_way_to_move_a_bond_or_repoint_the_escrow(self):
        for name in ("set_escrow", "set_escrow_address", "slash", "admin_slash", "withdraw", "withdraw_all",
                "rescue", "sweep", "emergency_withdraw", "withdraw_fees", "set_penalty_bps"):
            self.assertFalse(hasattr(self.mod.MerchantBond, name), name)

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #

    def test_unknown_bond_views(self):
        info = self.info(OUTSIDER)
        self.assertEqual((info["bonded"], info["strikes"], info["trusted"]), ("0", 0, False))
        with self.assertRaises(stub.UserError):
            self.bond.get_bond("nope")

    def test_preview_claim_matches_what_claim_would_do(self):
        self.post(amount=3 * GEN)
        self.install_deal()
        preview = j(self.bond.preview_claim(1, BUYER))
        self.assertEqual((preview["eligible"], preview["penalty"], preview["payable"], preview["seller"]),
            (True, str(800000000000000000), str(800000000000000000), SELLER))
        for stranger in (SELLER, OUTSIDER):
            p = j(self.bond.preview_claim(1, stranger))
            self.assertFalse(p["eligible"])
            self.assertIn("only the deal's buyer", p["reason"])
        self.install_deal(buyer_pct=10)
        self.assertFalse(j(self.bond.preview_claim(1, BUYER))["eligible"])
        self.install_deal(buyer_pct=80)
        self.claim()
        self.assertIn("already claimed", j(self.bond.preview_claim(1, BUYER))["reason"])
        with self.assertRaises(stub.UserError):
            self.bond.preview_claim(1, "nope")
        with self.assertRaises(stub.UserError):
            self.bond.preview_claim(404, BUYER)

    def test_preview_reports_the_shortfall_for_a_thin_bond(self):
        self.post(amount=10 ** 17)
        self.install_deal()
        preview = j(self.bond.preview_claim(1, BUYER))
        self.assertEqual((preview["penalty"], preview["payable"]), (str(800000000000000000), str(10 ** 17)))

    def test_stats_and_config(self):
        self.post(amount=GEN)
        stats = j(self.bond.get_stats())
        self.assertEqual((stats["total_bonded"], stats["on_chain_balance"]), (str(GEN), str(GEN)))
        cfg = j(self.bond.get_config())
        self.assertEqual((cfg["penalty_bps"], cfg["min_fault_pct"], cfg["claim_window_seconds"]),
            (2000, 20, 3 * DAY))
        self.assertEqual(cfg["trust_threshold"], str(GEN))

    # ------------------------------------------------------------------ #
    # randomized lifecycle
    # ------------------------------------------------------------------ #

    def test_random_operations_keep_the_bond_solvent(self):
        for seed in range(6):
            self.setUp()
            self._deposited = 0
            rng = random.Random(seed)
            sellers = [SELLER, OTHER_SELLER]
            buyers = [BUYER, OTHER_BUYER]
            deal_count = 0
            for step in range(250):
                action = rng.choice(["post", "post", "deal", "claim", "claim", "request", "cancel", "withdraw",
                    "withdraw", "expose", "wait"])
                try:
                    if action == "post":
                        self.post(rng.choice(sellers), rng.choice([10 ** 16, GEN // 3, GEN, 4 * GEN]))
                    elif action == "deal":
                        deal_count += 1
                        self.install_deal(deal_id=deal_count, buyer=rng.choice(buyers), seller=rng.choice(sellers),
                            price=rng.choice([10 ** 15, 10 ** 18, 7 * 10 ** 18 + 1]),
                            buyer_pct=rng.choice([0, 15, 20, 55, 100]),
                            resolution=rng.choice(["RULING", "RULING", "DEFAULT_SPLIT", "CONFIRMED"]))
                    elif action == "claim" and deal_count:
                        deal_id = rng.randint(1, deal_count)
                        self.claim(self.fake._deals[deal_id]["buyer"], deal_id)
                    elif action == "request":
                        who = rng.choice(sellers)
                        self.call(who, "request_unbond", rng.choice([1, GEN // 2, GEN, 3 * GEN]))
                    elif action == "cancel":
                        self.call(rng.choice(sellers), "cancel_unbond")
                    elif action == "withdraw":
                        self.call(rng.choice(sellers), "withdraw_unbonded")
                    elif action == "expose":
                        who = rng.choice(sellers)
                        self.set_exposure(who, live_deals=rng.choice([0, 0, 1]),
                            last_adverse_epoch=rng.choice([0, stub.CTX.now - 5 * DAY, stub.CTX.now]))
                    elif action == "wait":
                        stub.advance(rng.choice([60, HOUR, DAY, 4 * DAY]))
                except stub.UserError:
                    pass
                total = sum(int(x.bonded) for x in self.bond.bonds.values())
                self.assertEqual(stub.balance_of(BOND_ADDR), total)
                self.assertEqual(int(self.bond.total_bonded), total)
            self.tearDown()


if __name__ == "__main__":
    unittest.main()
