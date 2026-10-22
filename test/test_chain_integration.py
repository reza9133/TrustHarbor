"""
End-to-end tests of the real three-contract chain on the offline stub:
ArbitrationBench <- DealEscrow <- MerchantBond, wired by address exactly the
way three consecutive `genlayer deploy` calls wire them.

Besides the happy paths, this suite plays out the attacks the design is meant
to close: pre-won rulings, one-sided standards, replayed or re-targeted
cases, outcome shopping, and a seller pulling collateral out from under a
buyer.

Run with:  python3 -m unittest discover -s test -v
"""
import unittest

import genlayer_stub as stub
from support import (BENCH_ADDR, BOND_ADDR, BUYER, CLAIM_URL, DAY, ESCROW_ADDR, GEN, HOUR, MINUTE, OTHER_BUYER,
    OTHER_SELLER, OUTSIDER, OWNER, RESPONSE_URL, SELLER, TERMS, Chain, as_, j, load_escrow, script_ruling)

ALLOWED_CALLS = {(ESCROW_ADDR, BENCH_ADDR, "get_case_brief"), (BOND_ADDR, ESCROW_ADDR, "get_deal_brief"),
    (BOND_ADDR, ESCROW_ADDR, "get_seller_exposure")}


class ChainTestCase(unittest.TestCase):
    def setUp(self):
        self.chain = Chain()
        self.extra_allowed = set()

    def tearDown(self):
        self.chain.assert_solvent(self)
        unexpected = set(stub.CONTRACT_CALLS) - ALLOWED_CALLS - self.extra_allowed
        self.assertEqual(unexpected, set(), "contracts may only talk to their direct predecessor, by view")

    # -- helpers -- #

    def bond(self, seller=SELLER, amount=2 * GEN):
        out = self.chain.post_bond(seller, amount)
        self.assertTrue(out["ok"], out)

    def disputed_and_final(self, share=80, price=5 * GEN, buyer=BUYER, seller=SELLER):
        c = self.chain
        deal_id = c.open_deal(buyer=buyer, seller=seller, price=price)
        c.deliver(deal_id, seller)
        case_id = c.dispute(deal_id, claimant=buyer, respondent=seller)
        out = c.rule(case_id, share)
        self.assertTrue(out["ok"], out)
        c.finalize()
        return deal_id, case_id

    def reverts(self, call, contains=""):
        with self.assertRaises(stub.UserError) as ctx:
            call()
        self.assertIn(contains, ctx.exception.message)

    def split(self, price, pct, fee_bps=250):
        buyer_amount = price * pct // 100
        gross = price - buyer_amount
        fee = gross * fee_bps // 10000
        return buyer_amount, gross - fee, fee

    # ------------------------------------------------------------------ #
    # flows
    # ------------------------------------------------------------------ #

    def test_full_dispute_flow_from_funding_to_compensation(self):
        c = self.chain
        self.bond(amount=2 * GEN)
        deal_id, case_id = self.disputed_and_final(share=80)

        out = c.settle(deal_id)
        buyer_amount, seller_net, fee = self.split(5 * GEN, 80)
        self.assertEqual((out["resolution"], out["buyer_pct"]), ("RULING", 80))
        self.assertEqual((int(out["buyer_amount"]), int(out["seller_amount"]), int(out["fee"])),
            (buyer_amount, seller_net, fee))

        self.assertEqual(c.withdraw(BUYER)["withdrawn"], str(buyer_amount))
        self.assertEqual(c.withdraw(SELLER)["withdrawn"], str(seller_net))
        self.assertEqual(stub.paid_to(BUYER), buyer_amount)
        self.assertEqual(stub.paid_to(SELLER), seller_net)
        self.assertEqual(stub.balance_of(ESCROW_ADDR), fee)

        as_(BUYER)
        comp = j(c.bond.claim_compensation(deal_id))
        penalty = buyer_amount * 2000 // 10000
        self.assertEqual((comp["penalty"], comp["paid"]), (str(penalty), str(penalty)))
        self.assertEqual(stub.paid_to(BUYER), buyer_amount + penalty)
        self.assertEqual(j(c.bond.get_bond(SELLER))["bonded"], str(2 * GEN - penalty))

        as_(OWNER)
        c.escrow.withdraw_fees(OWNER, fee)
        self.assertEqual(stub.balance_of(ESCROW_ADDR), 0)
        self.assertEqual(stub.paid_to(OWNER), fee)

    def test_undisputed_deal_releases_after_the_review_window_and_the_owner_collects_the_fee(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        stub.advance(DAY)
        as_(OUTSIDER)
        j(c.escrow.release_after_review(deal_id))
        buyer_amount, seller_net, fee = self.split(5 * GEN, 0)
        self.assertEqual(c.withdraw(SELLER)["withdrawn"], str(seller_net))
        as_(OWNER)
        c.escrow.withdraw_fees(OWNER, fee)
        self.assertEqual((stub.paid_to(SELLER), stub.paid_to(OWNER)), (seller_net, fee))
        self.assertEqual(stub.balance_of(ESCROW_ADDR), 0)

    def test_buyer_confirms_early(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        as_(BUYER)
        j(c.escrow.confirm_receipt(deal_id))
        self.assertEqual(j(c.escrow.get_deal(deal_id))["status"], "RELEASED")

    def test_seller_that_never_delivers_is_refunded_in_full(self):
        c = self.chain
        deal_id = c.open_deal(window=HOUR)
        stub.advance(HOUR + 1)
        as_(OUTSIDER)
        j(c.escrow.reclaim_undelivered(deal_id))
        self.assertEqual(c.withdraw(BUYER)["withdrawn"], str(5 * GEN))
        self.assertEqual(stub.balance_of(ESCROW_ADDR), 0)

    def test_the_seller_can_be_the_claimant_and_the_share_flips(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        case_id = c.dispute(deal_id, claimant=SELLER, respondent=BUYER)
        out = c.rule(case_id, 100)
        self.assertTrue(out["ok"])
        c.finalize()
        settled = c.settle(deal_id)
        self.assertEqual((settled["buyer_pct"], settled["buyer_amount"]), (0, "0"))
        self.assertEqual(j(c.escrow.get_seller_exposure(SELLER))["last_adverse_epoch"], 0)

    def test_an_appeal_can_flip_the_outcome_and_the_deal_follows_the_appeal(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        case_id = c.dispute(deal_id)
        c.rule(case_id, 80)
        script_ruling(20)
        as_(SELLER)
        out = j(c.bench.appeal_case(case_id))
        self.assertEqual((out["first_share"], out["claimant_share"], out["phase"]), (80, 20, "FINAL"))
        settled = c.settle(deal_id)
        self.assertEqual(settled["buyer_pct"], 20)
        self.assertEqual(int(settled["buyer_amount"]), self.split(5 * GEN, 20)[0])

    def test_an_unappealed_ruling_cannot_be_settled_early(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        case_id = c.dispute(deal_id)
        c.rule(case_id, 80)
        self.reverts(lambda: c.settle(deal_id), "RULED on the bench")
        stub.advance(HOUR)
        self.assertEqual(c.settle(deal_id)["status"], "SETTLED")

    def test_a_case_nobody_rules_falls_back_to_an_even_split(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        c.dispute(deal_id, respond=False)
        stub.advance(30 * DAY)
        out = c.settle(deal_id)
        self.assertEqual((out["resolution"], out["buyer_pct"]), ("DEFAULT_SPLIT", 50))

    def test_a_ruling_is_still_possible_right_up_to_the_grace_period(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        case_id = c.dispute(deal_id, respond=False)
        stub.advance(30 * DAY - 30 * MINUTE)
        c.rule(case_id, 100)
        stub.advance(31 * MINUTE)
        # the grace period is over, but a ruling is on the table: it must run its appeal window
        self.reverts(lambda: c.settle(deal_id), "RULED on the bench")
        stub.advance(30 * MINUTE)
        self.assertEqual(c.settle(deal_id)["buyer_pct"], 100)

    def test_a_withdrawn_case_lets_the_buyer_try_again_inside_the_review_window(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        first = c.dispute(deal_id)
        as_(BUYER)
        j(c.bench.withdraw_case(first))
        out = c.settle(deal_id)
        self.assertEqual(out["status"], "DELIVERED")
        second = c.dispute(deal_id)
        self.assertNotEqual(first, second)
        c.rule(second, 60)
        c.finalize()
        self.assertEqual(c.settle(deal_id)["buyer_pct"], 60)

    def test_a_low_confidence_ruling_leaves_the_deal_waiting_and_a_retry_works(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        case_id = c.dispute(deal_id)
        out = c.rule(case_id, 90, confidence=10)
        self.assertFalse(out["ok"])
        self.assertEqual(j(c.escrow.get_deal(deal_id))["status"], "DISPUTED")
        out = c.rule(case_id, 90, confidence=85)
        self.assertTrue(out["ok"])

    def test_a_failed_consensus_round_reverts_cleanly_and_the_ruling_can_be_retried(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        case_id = c.dispute(deal_id)
        script_ruling(100, validator_share=0)
        as_(OUTSIDER)
        with self.assertRaises(stub.ConsensusDisagreement):
            c.bench.rule_case(case_id)
        self.assertEqual(j(c.bench.get_case(case_id))["phase"], "OPEN")
        self.assertTrue(c.rule(case_id, 100)["ok"])

    def test_pausing_the_bench_never_strands_an_in_flight_deal(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        case_id = c.dispute(deal_id)
        as_(OWNER)
        c.bench.set_paused(True)
        as_(OWNER)
        c.escrow.set_paused(True)
        self.assertTrue(c.rule(case_id, 70)["ok"])
        c.finalize()
        self.assertEqual(c.settle(deal_id)["status"], "SETTLED")
        self.assertTrue(c.withdraw(BUYER)["ok"])

    def test_many_deals_in_parallel_stay_solvent(self):
        c = self.chain
        self.bond(amount=10 * GEN)
        ids = []
        for i in range(6):
            deal_id = c.open_deal(buyer=[BUYER, OTHER_BUYER][i % 2], price=(i + 1) * GEN // 3 + 7)
            c.deliver(deal_id)
            ids.append(deal_id)
        c.assert_solvent(self)
        case_a = c.dispute(ids[0], claimant=BUYER)
        case_b = c.dispute(ids[1], claimant=OTHER_BUYER)
        c.rule(case_a, 100)
        c.rule(case_b, 35)
        c.finalize()
        as_(BUYER)
        j(c.escrow.confirm_receipt(ids[2]))
        for deal_id in ids[:2]:
            c.settle(deal_id)
        stub.advance(DAY)
        for deal_id in ids[3:]:
            as_(OUTSIDER)
            j(c.escrow.release_after_review(deal_id))
        for who in (BUYER, OTHER_BUYER, SELLER):
            as_(who)
            try:
                c.escrow.withdraw()
            except stub.UserError:
                pass
        c.assert_solvent(self)

    # ------------------------------------------------------------------ #
    # collateral: the bond follows the escrow
    # ------------------------------------------------------------------ #

    def test_collateral_cannot_leave_while_a_deal_is_unresolved(self):
        c = self.chain
        self.bond(amount=2 * GEN)
        as_(SELLER)
        j(c.bond.request_unbond(2 * GEN))
        deal_id = c.open_deal()
        stub.advance(DAY)
        as_(SELLER)
        self.reverts(lambda: c.bond.withdraw_unbonded(), "1 unresolved deal(s)")
        c.deliver(deal_id)
        as_(SELLER)
        self.reverts(lambda: c.bond.withdraw_unbonded(), "1 unresolved deal(s)")
        case_id = c.dispute(deal_id)
        as_(SELLER)
        self.reverts(lambda: c.bond.withdraw_unbonded(), "1 unresolved deal(s)")
        c.rule(case_id, 100)
        c.finalize()
        as_(SELLER)
        self.reverts(lambda: c.bond.withdraw_unbonded(), "1 unresolved deal(s)")
        c.settle(deal_id)
        # resolved, but the buyer still has the claim window
        as_(SELLER)
        self.reverts(lambda: c.bond.withdraw_unbonded(), "may still claim")
        as_(BUYER)
        claim = j(c.bond.claim_compensation(deal_id))
        self.assertEqual(claim["paid"], str(5 * GEN * 2000 // 10000))
        stub.advance(3 * DAY)
        as_(SELLER)
        out = j(c.bond.withdraw_unbonded())
        self.assertEqual(out["withdrawn"], str(2 * GEN - 5 * GEN * 2000 // 10000))
        self.assertEqual(stub.balance_of(BOND_ADDR), 0)

    def test_a_seller_cannot_deliver_and_unbond_before_the_buyer_reacts(self):
        c = self.chain
        self.bond(amount=2 * GEN)
        deal_id = c.open_deal()
        as_(SELLER)
        j(c.bond.request_unbond(2 * GEN))
        stub.advance(DAY)
        c.deliver(deal_id)
        as_(SELLER)
        self.reverts(lambda: c.bond.withdraw_unbonded(), "unresolved deal(s)")

    def test_a_seller_who_settles_everything_honestly_can_leave_after_the_delay(self):
        c = self.chain
        self.bond(amount=GEN)
        deal_id = c.open_deal()
        c.deliver(deal_id)
        as_(BUYER)
        j(c.escrow.confirm_receipt(deal_id))
        as_(SELLER)
        j(c.bond.request_unbond(GEN))
        stub.advance(DAY)
        as_(SELLER)
        self.assertEqual(j(c.bond.withdraw_unbonded())["withdrawn"], str(GEN))

    def test_a_dust_deal_flood_is_cleared_by_the_seller_refunding_it(self):
        c = self.chain
        self.bond(amount=GEN)
        as_(SELLER)
        j(c.bond.request_unbond(GEN))
        ids = [c.open_deal(buyer=OUTSIDER, price=10 ** 15) for _ in range(3)]
        stub.advance(DAY)
        as_(SELLER)
        self.reverts(lambda: c.bond.withdraw_unbonded(), "3 unresolved deal(s)")
        for deal_id in ids:
            as_(SELLER)
            j(c.escrow.refund_buyer(deal_id))
        as_(SELLER)
        self.assertTrue(j(c.bond.withdraw_unbonded())["ok"])

    def test_compensation_is_only_for_ruled_deals_and_only_for_the_buyer(self):
        c = self.chain
        self.bond(amount=3 * GEN)
        deal_id, _ = self.disputed_and_final(share=100)
        c.settle(deal_id)
        for who in (SELLER, OUTSIDER, OTHER_BUYER):
            as_(who)
            self.reverts(lambda: c.bond.claim_compensation(deal_id), "only the deal's buyer")
        as_(BUYER)
        j(c.bond.claim_compensation(deal_id))
        as_(BUYER)
        self.reverts(lambda: c.bond.claim_compensation(deal_id), "already claimed")

        undisputed = c.open_deal(price=GEN)
        c.deliver(undisputed)
        as_(BUYER)
        j(c.escrow.confirm_receipt(undisputed))
        as_(BUYER)
        self.reverts(lambda: c.bond.claim_compensation(undisputed), "not settled by an arbitration ruling")

        split_deal = c.open_deal(price=GEN)
        c.deliver(split_deal)
        c.dispute(split_deal, respond=False)
        stub.advance(30 * DAY)
        c.settle(split_deal)
        as_(BUYER)
        self.reverts(lambda: c.bond.claim_compensation(split_deal), "not settled by an arbitration ruling")

    def test_a_seller_win_earns_the_buyer_no_compensation(self):
        c = self.chain
        self.bond(amount=GEN)
        deal_id, _ = self.disputed_and_final(share=10)
        c.settle(deal_id)
        as_(BUYER)
        self.reverts(lambda: c.bond.claim_compensation(deal_id), "less than 20%")

    def test_the_trusted_flag_follows_the_seller_s_record(self):
        c = self.chain
        self.bond(amount=GEN)
        self.assertTrue(c.bond.is_trusted(SELLER))
        for i in range(3):
            deal_id, _ = self.disputed_and_final(share=100, price=GEN // 10)
            c.settle(deal_id)
            as_(BUYER)
            j(c.bond.claim_compensation(deal_id))
        self.assertFalse(c.bond.is_trusted(SELLER))

    # ------------------------------------------------------------------ #
    # attacks
    # ------------------------------------------------------------------ #

    def test_a_ruling_cannot_be_won_in_advance_for_a_deal_that_does_not_exist_yet(self):
        c = self.chain
        predicted_subject = "deal:" + ESCROW_ADDR + ":1"
        as_(BUYER)
        out = j(c.bench.open_case(SELLER, predicted_subject, CLAIM_URL, TERMS, DAY))
        early_case = out["case_id"]
        as_(SELLER)
        j(c.bench.respond(early_case, RESPONSE_URL))
        stub.advance(5)
        deal_id = c.open_deal()
        c.deliver(deal_id)
        as_(BUYER)
        self.reverts(lambda: c.escrow.attach_case(deal_id, early_case), "before this deal existed")

    def test_a_case_with_a_one_sided_rulebook_is_refused(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        template = j(c.escrow.get_dispute_template(deal_id))
        as_(BUYER)
        out = j(c.bench.open_case(SELLER, template["subject"], CLAIM_URL, "The buyer always wins.", DAY))
        as_(BUYER)
        self.reverts(lambda: c.escrow.attach_case(deal_id, out["case_id"]), "standard must equal")

    def test_a_case_against_the_wrong_counterparty_is_refused(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        template = j(c.escrow.get_dispute_template(deal_id))
        as_(BUYER)
        out = j(c.bench.open_case(OUTSIDER, template["subject"], CLAIM_URL, TERMS, DAY))
        as_(BUYER)
        self.reverts(lambda: c.escrow.attach_case(deal_id, out["case_id"]), "parties are not")

    def test_a_third_party_cannot_hijack_a_deal_with_their_own_case(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        template = j(c.escrow.get_dispute_template(deal_id))
        as_(OUTSIDER)
        out = j(c.bench.open_case(SELLER, template["subject"], CLAIM_URL, TERMS, DAY))
        for who in (OUTSIDER, BUYER, SELLER):
            as_(who)
            self.reverts(lambda: c.escrow.attach_case(deal_id, out["case_id"]), "")

    def test_outcome_shopping_is_closed_a_ruled_case_cannot_be_attached(self):
        c = self.chain = Chain(review_window=3 * DAY)
        deal_id = c.open_deal()
        c.deliver(deal_id)
        template = j(c.escrow.get_dispute_template(deal_id))
        ids = []
        for _ in range(3):
            as_(BUYER)
            ids.append(j(c.bench.open_case(SELLER, template["subject"], CLAIM_URL, TERMS, DAY))["case_id"])
        stub.advance(DAY)
        script_ruling(100)
        for case_id in ids:
            as_(OUTSIDER)
            j(c.bench.rule_case(case_id))
        for case_id in ids:
            as_(BUYER)
            self.reverts(lambda: c.escrow.attach_case(deal_id, case_id), "before it is ruled")
        c.finalize()
        for case_id in ids:
            as_(BUYER)
            self.reverts(lambda: c.escrow.attach_case(deal_id, case_id), "before it is ruled")

    def test_the_first_bound_case_decides_and_the_others_are_irrelevant(self):
        c = self.chain
        deal_id = c.open_deal()
        c.deliver(deal_id)
        first = c.open_case_for(deal_id, BUYER, SELLER)
        second = c.open_case_for(deal_id, BUYER, SELLER)
        as_(SELLER)
        j(c.escrow.attach_case(deal_id, second))
        as_(BUYER)
        self.reverts(lambda: c.escrow.attach_case(deal_id, first), "DISPUTED")
        self.assertEqual(j(c.escrow.get_deal(deal_id))["case_id"], second)

    def test_a_case_cannot_be_reused_for_another_deal(self):
        c = self.chain
        first = c.open_deal()
        second = c.open_deal()
        c.deliver(first)
        c.deliver(second)
        case_id = c.dispute(first)
        as_(BUYER)
        self.reverts(lambda: c.escrow.attach_case(second, case_id), "subject does not name")

    def test_a_case_cannot_follow_a_deal_onto_another_escrow_instance(self):
        c = self.chain
        other_addr = "0x" + "b9" * 20
        other_mod = load_escrow("other_escrow")
        other = stub.deploy(other_mod.DealEscrow, other_addr, BENCH_ADDR, 250, DAY, sender=OWNER)
        self.extra_allowed.add((other_addr, BENCH_ADDR, "get_case_brief"))
        as_(BUYER, GEN)
        mine = j(c.escrow.open_deal(SELLER, TERMS, DAY))["deal_id"]
        as_(BUYER, GEN)
        theirs = j(other.open_deal(SELLER, TERMS, DAY))["deal_id"]
        for escrow, deal_id in ((c.escrow, mine), (other, theirs)):
            as_(SELLER)
            escrow.mark_delivered(deal_id, "x")
        template = j(c.escrow.get_dispute_template(mine))
        as_(BUYER)
        case_id = j(c.bench.open_case(SELLER, template["subject"], CLAIM_URL, TERMS, DAY))["case_id"]
        as_(BUYER)
        self.reverts(lambda: other.attach_case(theirs, case_id), "subject does not name")
        as_(BUYER)
        self.assertEqual(j(c.escrow.attach_case(mine, case_id))["status"], "DISPUTED")

    def test_a_seller_cannot_dodge_a_bad_ruling_by_refunding_before_it_is_final(self):
        c = self.chain
        self.bond(amount=GEN)
        deal_id = c.open_deal()
        c.deliver(deal_id)
        case_id = c.dispute(deal_id)
        c.rule(case_id, 100)
        as_(SELLER)
        self.reverts(lambda: c.escrow.refund_buyer(deal_id), "not FUNDED or DELIVERED")
        c.finalize()
        c.settle(deal_id)
        as_(BUYER)
        self.assertTrue(j(c.bond.claim_compensation(deal_id))["ok"])

    def test_a_seller_cannot_freeze_a_dispute_by_pointing_their_own_case_at_a_dead_page(self):
        c = self.chain = Chain(review_window=3 * DAY)
        deal_id = c.open_deal()
        c.deliver(deal_id)
        template = j(c.escrow.get_dispute_template(deal_id))
        as_(SELLER)
        case_id = j(c.bench.open_case(BUYER, template["subject"], CLAIM_URL, template["standard"], DAY))["case_id"]
        as_(SELLER)
        j(c.escrow.attach_case(deal_id, case_id))
        as_(BUYER)
        j(c.bench.respond(case_id, RESPONSE_URL))
        script_ruling(100, pages={"evidence.example": "unused", "claim.example": RuntimeError("down"),
            "reply.example": "The buyer's evidence."})
        as_(OUTSIDER)
        with self.assertRaises(stub.UserError):
            c.bench.rule_case(case_id)
        stub.advance(DAY + 3 * DAY)
        as_(OUTSIDER)
        out = j(c.bench.rule_case(case_id))
        self.assertEqual((out["ok"], out["claimant_share"]), (True, 0))
        c.finalize()
        settled = c.settle(deal_id)
        self.assertEqual((settled["buyer_pct"], settled["buyer_amount"]), (100, str(5 * GEN)))

    def test_a_seller_cannot_bind_a_case_and_withdraw_it_after_the_review_window(self):
        c = self.chain = Chain(review_window=DAY)
        deal_id = c.open_deal()
        c.deliver(deal_id)
        case_id = c.open_case_for(deal_id, SELLER, BUYER)
        as_(SELLER)
        j(c.escrow.attach_case(deal_id, case_id))
        as_(BUYER)
        self.reverts(lambda: c.escrow.confirm_receipt(deal_id), "not FUNDED or DELIVERED")
        stub.advance(DAY + HOUR)
        as_(SELLER)
        j(c.bench.withdraw_case(case_id))
        c.settle(deal_id)
        as_(OUTSIDER)
        self.reverts(lambda: c.escrow.release_after_review(deal_id), "still open")
        as_(SELLER)
        retry = c.open_case_for(deal_id, SELLER, BUYER)
        self.reverts(lambda: c.escrow.attach_case(deal_id, retry), "only the other party")
        mine = c.open_case_for(deal_id, BUYER, SELLER)
        as_(BUYER)
        self.assertEqual(j(c.escrow.attach_case(deal_id, mine))["status"], "DISPUTED")
        stub.advance(DAY)  # the seller never answers
        c.rule(mine, 100)
        c.finalize()
        self.assertEqual(c.settle(deal_id)["buyer_pct"], 100)

    def test_a_case_opened_long_before_delivery_cannot_ambush_the_seller(self):
        c = self.chain
        as_(BUYER, 5 * GEN)
        deal_id = j(c.escrow.open_deal(SELLER, TERMS, 10 * DAY))["deal_id"]
        template = j(c.escrow.get_dispute_template(deal_id))
        as_(BUYER)
        early = j(c.bench.open_case(SELLER, template["subject"], CLAIM_URL, TERMS, DAY))["case_id"]
        stub.advance(2 * DAY)
        c.deliver(deal_id)
        as_(BUYER)
        self.reverts(lambda: c.escrow.attach_case(deal_id, early), "would have only")

    def test_the_bond_cannot_be_slashed_by_anyone_for_a_deal_it_does_not_stand_behind(self):
        c = self.chain
        self.bond(OTHER_SELLER, GEN)
        deal_id, _ = self.disputed_and_final(share=100)
        c.settle(deal_id)
        as_(BUYER)
        self.reverts(lambda: c.bond.claim_compensation(deal_id), "no bond to claim")
        self.assertEqual(j(c.bond.get_bond(OTHER_SELLER))["bonded"], str(GEN))

    def test_nobody_but_the_owner_touches_fees_and_the_owner_cannot_touch_anything_else(self):
        c = self.chain
        self.bond(amount=GEN)
        deal_id = c.open_deal()
        c.deliver(deal_id)
        as_(BUYER)
        j(c.escrow.confirm_receipt(deal_id))
        fee = self.split(5 * GEN, 0)[2]
        as_(OWNER)
        c.escrow.withdraw_fees(OWNER, fee)
        as_(OWNER)
        self.reverts(lambda: c.escrow.withdraw_fees(OWNER, 1), "amount must be")
        as_(OWNER)
        self.reverts(lambda: c.bond.withdraw_unbonded(), "no unbond request")
        self.assertEqual(stub.balance_of(BOND_ADDR), GEN)
        self.assertEqual(stub.balance_of(ESCROW_ADDR), 5 * GEN - fee)

    # ------------------------------------------------------------------ #
    # wiring
    # ------------------------------------------------------------------ #

    def test_each_contract_is_wired_to_exactly_its_predecessor(self):
        c = self.chain
        self.assertEqual(j(c.escrow.get_config())["bench"], BENCH_ADDR)
        self.assertEqual(j(c.bond.get_config())["escrow"], ESCROW_ADDR)

    def test_the_bench_never_calls_another_contract(self):
        c = self.chain
        self.bond()
        self.disputed_and_final(share=70)
        callers = {caller for (caller, _target, _method) in stub.CONTRACT_CALLS}
        self.assertNotIn(BENCH_ADDR, callers)

    def test_no_value_ever_sits_in_the_bench(self):
        c = self.chain
        self.disputed_and_final(share=70)
        self.assertEqual(stub.balance_of(BENCH_ADDR), 0)


if __name__ == "__main__":
    unittest.main()
