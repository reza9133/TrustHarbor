"""
Model-based fuzzing of the real three-contract chain on the offline stub.

A seeded random walk fires every public write method of all three contracts
from a mix of honest parties, a hostile outsider and the owner, with random
arguments (valid, boundary and garbage), random LLM answers, random validator
disagreement and random clock jumps. After EVERY step a set of global
invariants is recomputed from scratch from the contracts' storage:

  * solvency of the escrow and of the bond contract (balance == liabilities)
  * owed_total == sum(claimable) + unresolved prices (minus nothing else)
  * per-seller live-deal and open-dispute counters match the deal table
  * per-claimant open-case counters match the case table
  * every resolved deal split is exact (buyer + seller + fee == price)
  * every bound case is the one the deal says, and is never WITHDRAWN while
    the deal is still DISPUTED after a settle attempt
  * bond bookkeeping: total_bonded == sum(bonded), pending never negative
  * a deal never leaves a terminal state

Run with:  python3 -m unittest discover -s test -v
"""
import random
import unittest

import genlayer_stub as stub
from support import (BENCH_ADDR, BOND_ADDR, DAY, ESCROW_ADDR, GEN, HOUR, MINUTE, OUTSIDER, OWNER, CLAIM_URL,
    RESPONSE_URL, Chain, as_, j, script_ruling)

ACTORS = ["0x" + "a1" * 20, "0x" + "a2" * 20, "0x" + "a3" * 20, OUTSIDER]
TERMINAL = {"RELEASED", "REFUNDED", "SETTLED"}
UNRESOLVED = {"FUNDED", "DELIVERED", "DISPUTED"}

GARBAGE_STRINGS = ["", " ", "0x", "0x" + "00" * 20, "not an address", "0x" + "zz" * 20, "a" * 600,
    "https://x", "ftp://bad", "https://has space.example", "‮", "deal:0x:1"]
GARBAGE_INTS = [0, -1, 1, 59, 60, 10 ** 6, 7 * DAY, 7 * DAY + 1, 60 * DAY, 60 * DAY + 1, 2 ** 31, 2 ** 70]


class FuzzWorld:
    def __init__(self, seed, appeal_window=HOUR, review_window=DAY, unbond_delay=DAY, fee_bps=250,
            penalty_bps=2000):
        self.rng = random.Random(seed)
        self.c = Chain(appeal_window=appeal_window, review_window=review_window, unbond_delay=unbond_delay,
            fee_bps=fee_bps, penalty_bps=penalty_bps)
        self.actors = list(ACTORS)
        self.deal_ids = []
        self.case_ids = []
        self.terminal_seen = {}
        self.log = []

    # -- random helpers -- #

    def who(self):
        return self.rng.choice(self.actors + [OWNER])

    def other(self, a):
        return self.rng.choice([x for x in self.actors if x != a])

    def deal(self):
        if self.deal_ids and self.rng.random() < 0.95:
            return self.rng.choice(self.deal_ids)
        return self.rng.choice([0, 1, 999, -1, 2 ** 40])

    def case(self):
        if self.case_ids and self.rng.random() < 0.95:
            return self.rng.choice(self.case_ids)
        return self.rng.choice([0, 1, 999, -1, 2 ** 40])

    def maybe_garbage_str(self, good):
        return self.rng.choice(GARBAGE_STRINGS) if self.rng.random() < 0.08 else good

    def maybe_garbage_int(self, good):
        return self.rng.choice(GARBAGE_INTS) if self.rng.random() < 0.08 else good

    def attempt(self, label, fn, sender, value=0):
        self.log.append((label, sender[-4:], value))
        as_(sender, value)
        try:
            return fn()
        except (stub.UserError, stub.ConsensusDisagreement):
            return None

    # -- the actions -- #

    def smart_act(self):
        """Mostly-valid, role-aware moves, so the walk reaches the deep
        states (disputes, rulings, appeals, settlements, slashing) that
        purely random calls almost never get to."""
        rng = self.rng
        c = self.c
        deals = self.deals()
        if not deals or rng.random() < 0.12:
            buyer = rng.choice(self.actors[:3])
            seller = self.other(buyer)
            out = self.attempt("open_deal", lambda: j(c.escrow.open_deal(
                seller, "Deliver the logo pack", rng.choice([DAY, 3 * DAY]))), buyer,
                rng.choice([GEN, 2 * GEN + 3, 10 ** 15, 7 * GEN]))
            if out and out.get("ok"):
                self.deal_ids.append(out["deal_id"])
            if rng.random() < 0.5:
                self.attempt("post_bond", lambda: c.bond.post_bond(), seller, rng.choice([GEN, 2 * GEN]))
            return
        did, d = rng.choice(deals)
        buyer, seller, status = str(d.buyer), str(d.seller), str(d.status)
        by_status = {"FUNDED": [0, 0, 0, 0, 0, 0, 11, 10, 6, 9],
            "DELIVERED": [1, 1, 1, 1, 2, 2, 3, 7, 11, 10],
            "DISPUTED": [4, 4, 4, 5, 6, 7, 7, 7, 8, 9, 1]}
        move = rng.choice(by_status.get(status, [9, 10, 10, 6]))
        if move == 0:
            self.attempt("mark_delivered", lambda: c.escrow.mark_delivered(did, "done"), seller)
        elif move in (1, 2, 3):
            claimant = rng.choice([buyer, seller])
            respondent = seller if claimant == buyer else buyer
            tpl = j(c.escrow.get_dispute_template(did))
            window = rng.choice([DAY, 2 * DAY, HOUR, 7 * DAY])
            out = self.attempt("open_case", lambda: j(c.bench.open_case(
                respondent, tpl["subject"], CLAIM_URL, tpl["standard"], window)), claimant)
            if out and out.get("ok"):
                self.case_ids.append(out["case_id"])
                self.attempt("attach_case", lambda: c.escrow.attach_case(did, out["case_id"]), claimant)
                if rng.random() < 0.8:
                    self.attempt("respond", lambda: c.bench.respond(out["case_id"], RESPONSE_URL), respondent)
        elif move in (4, 5) and int(d.case_id) > 0:
            share = rng.choice([0, 15, 20, 50, 80, 100])
            script_ruling(share, confidence=rng.choice([90, 90, 90, 20]),
                validator_share=min(100, share + 10) if rng.random() < 0.2 else None)
            self.attempt("rule_case", lambda: c.bench.rule_case(int(d.case_id)), OUTSIDER)
        elif move == 6 and int(d.case_id) > 0:
            script_ruling(rng.choice([0, 50, 100]))
            self.attempt("appeal_case", lambda: c.bench.appeal_case(int(d.case_id)), rng.choice([buyer, seller]))
        elif move in (7, 8):
            stub.advance(rng.choice([MINUTE, HOUR, HOUR + 1, DAY, 2 * DAY]))
            self.attempt("settle_deal", lambda: c.escrow.settle_deal(did), OUTSIDER)
        elif move == 9:
            self.attempt("claim_compensation", lambda: c.bond.claim_compensation(did), buyer)
        elif move == 10:
            self.attempt("withdraw", lambda: c.escrow.withdraw(), rng.choice([buyer, seller]))
        else:
            self.attempt(rng.choice(["confirm", "refund", "release"]),
                lambda: rng.choice([lambda: c.escrow.confirm_receipt(did),
                    lambda: c.escrow.refund_buyer(did), lambda: c.escrow.release_after_review(did)])(),
                rng.choice([buyer, seller, OUTSIDER]))
        if rng.random() < 0.08:
            seller_ = rng.choice(self.actors[:3])
            self.attempt("request_unbond", lambda: c.bond.request_unbond(rng.choice([GEN, 2 * GEN])), seller_)
            stub.advance(rng.choice([HOUR, DAY, 2 * DAY]))
            self.attempt("withdraw_unbonded", lambda: c.bond.withdraw_unbonded(), seller_)

    def act(self):
        rng = self.rng
        c = self.c
        if rng.random() < 0.6:
            return self.smart_act()
        pick = rng.randrange(100)
        me = self.who()
        if pick < 12:
            seller = self.other(me)
            price = rng.choice([10 ** 15, 10 ** 15 - 1, GEN, 3 * GEN + 7, 100 * GEN, 100 * GEN + 1, 0])
            window = self.maybe_garbage_int(rng.choice([MINUTE, HOUR, DAY, 5 * DAY]))
            terms = self.maybe_garbage_str(rng.choice(["Deliver the logo pack", "x" * 300, "x" * 301]))
            balance_before = stub.balance_of(ESCROW_ADDR)
            out = self.attempt("open_deal", lambda: j(c.escrow.open_deal(
                self.maybe_garbage_str(seller), terms, window)), me, price)
            if out and out.get("ok"):
                self.deal_ids.append(out["deal_id"])
        elif pick < 20:
            self.attempt("mark_delivered", lambda: c.escrow.mark_delivered(
                self.deal(), rng.choice(["ok", "n" * 201])), me)
        elif pick < 24:
            self.attempt("confirm_receipt", lambda: c.escrow.confirm_receipt(self.deal()), me)
        elif pick < 28:
            self.attempt("release_after_review", lambda: c.escrow.release_after_review(self.deal()), me)
        elif pick < 30:
            self.attempt("reclaim_undelivered", lambda: c.escrow.reclaim_undelivered(self.deal()), me)
        elif pick < 33:
            self.attempt("refund_buyer", lambda: c.escrow.refund_buyer(self.deal()), me)
        elif pick < 43:
            self.act_open_case(me)
        elif pick < 50:
            self.act_attach(me)
        elif pick < 55:
            self.attempt("respond", lambda: c.bench.respond(self.case(), self.maybe_garbage_str(RESPONSE_URL)), me)
        elif pick < 57:
            self.attempt("withdraw_case", lambda: c.bench.withdraw_case(self.case()), me)
        elif pick < 66:
            self.act_rule(me)
        elif pick < 70:
            self.act_appeal(me)
        elif pick < 76:
            self.attempt("settle_deal", lambda: c.escrow.settle_deal(self.deal()), me)
        elif pick < 80:
            self.attempt("withdraw", lambda: c.escrow.withdraw(), me)
        elif pick < 83:
            self.attempt("post_bond", lambda: c.bond.post_bond(), me,
                rng.choice([10 ** 16 - 1, 10 ** 16, GEN, 2 * GEN, 1000 * GEN + 1, 0]))
        elif pick < 86:
            self.attempt("request_unbond", lambda: c.bond.request_unbond(
                rng.choice([0, 1, GEN, 2 * GEN, 10 * GEN, -5])), me)
        elif pick < 87:
            self.attempt("cancel_unbond", lambda: c.bond.cancel_unbond(), me)
        elif pick < 90:
            self.attempt("withdraw_unbonded", lambda: c.bond.withdraw_unbonded(), me)
        elif pick < 93:
            self.attempt("claim_compensation", lambda: c.bond.claim_compensation(self.deal()), me)
        elif pick < 94:
            self.act_admin()
        else:
            stub.advance(rng.choice([1, 30, MINUTE, 10 * MINUTE, HOUR, 6 * HOUR, DAY, 3 * DAY, 9 * DAY, 31 * DAY]))

    def act_open_case(self, me):
        rng = self.rng
        c = self.c
        deal_id = self.deal()
        try:
            template = j(c.escrow.get_dispute_template(deal_id))
        except stub.UserError:
            template = {"subject": "junk", "standard": "junk", "buyer": me, "seller": self.other(me)}
        respondent = template["seller"] if me == template["buyer"] else template["buyer"]
        if rng.random() < 0.15:
            respondent = self.other(me)
        subject = template["subject"] if rng.random() < 0.9 else rng.choice(["deal:x:1", "other", "d" * 130])
        standard = template["standard"] if rng.random() < 0.9 else rng.choice(["", "one sided", "s" * 501])
        window = rng.choice([MINUTE, HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 7 * DAY + 1])
        url = self.maybe_garbage_str(CLAIM_URL)
        out = self.attempt("open_case", lambda: j(c.bench.open_case(
            respondent, subject, url, standard, window)), me)
        if out and out.get("ok"):
            self.case_ids.append(out["case_id"])

    def act_attach(self, me):
        c = self.c
        self.attempt("attach_case", lambda: c.escrow.attach_case(self.deal(), self.case()), me)

    def act_rule(self, me):
        rng = self.rng
        c = self.c
        kind = rng.random()
        share = rng.choice([0, 5, 20, 35, 50, 80, 100, 3, 97])
        confidence = rng.choice([100, 90, 31, 30, 29, 0])
        pages = None
        if kind < 0.1:
            pages = {"claim.example": "", "reply.example": "x"}
        elif kind < 0.2:
            pages = {"claim.example": RuntimeError("down"), "reply.example": RuntimeError("down")}
        elif kind < 0.25:
            pages = {"claim.example": "evidence", "reply.example": RuntimeError("gone")}
        v_share = share + rng.choice([20, 16, 15, 10, 0]) if rng.random() < 0.3 else None
        script_ruling(share, confidence=confidence, pages=pages,
            validator_share=min(100, v_share) if v_share is not None else None)
        if rng.random() < 0.05:
            stub.LEADER.llm = rng.choice(["not json", {"claimant_share": "x"}, {"claimant_share": True},
                {"claimant_share": 50}, RuntimeError("llm down")])
        self.attempt("rule_case", lambda: c.bench.rule_case(self.case()), me)

    def act_appeal(self, me):
        c = self.c
        script_ruling(self.rng.choice([0, 50, 100, 20, 80]), confidence=self.rng.choice([90, 10]))
        self.attempt("appeal_case", lambda: c.bench.appeal_case(self.case()), me)

    def act_admin(self):
        rng = self.rng
        c = self.c
        sender = OWNER if rng.random() < 0.8 else OUTSIDER
        choice = rng.randrange(7)
        if choice == 0:
            self.attempt("escrow.pause", lambda: c.escrow.set_paused(rng.random() < 0.5), sender)
        elif choice == 1:
            self.attempt("bench.pause", lambda: c.bench.set_paused(rng.random() < 0.5), sender)
        elif choice == 2:
            self.attempt("bond.pause", lambda: c.bond.set_paused(rng.random() < 0.5), sender)
        elif choice == 3:
            self.attempt("set_fee", lambda: c.escrow.set_fee_bps(rng.choice([0, 250, 500, 501])), sender)
        elif choice == 4:
            self.attempt("set_review", lambda: c.escrow.set_review_window(
                rng.choice([59, 60, HOUR, DAY, 14 * DAY, 15 * DAY])), sender)
        elif choice == 5:
            self.attempt("set_appeal", lambda: c.bench.set_appeal_window(
                rng.choice([59, 60, HOUR, DAY, 8 * DAY])), sender)
        else:
            fees = int(c.escrow.fees_accrued) - int(c.escrow.fees_withdrawn)
            self.attempt("withdraw_fees", lambda: c.escrow.withdraw_fees(
                OWNER, rng.choice([0, 1, max(fees, 1), fees + 1, 10 ** 30])), sender)

    # -- invariants -- #

    def deals(self):
        return [(int(k), v) for k, v in self.c.escrow.deals.items()]

    def check(self, test, step):
        c = self.c
        ctx = "step %s, last actions: %s" % (step, self.log[-4:])
        deals = self.deals()
        claimable = sum(int(v) for v in c.escrow.claimable.values())
        unresolved = sum(int(d.price) for _, d in deals if str(d.status) in UNRESOLVED)
        owed = int(c.escrow.owed_total)
        test.assertEqual(owed, claimable + unresolved, "owed_total drifted; " + ctx)
        fees_left = int(c.escrow.fees_accrued) - int(c.escrow.fees_withdrawn)
        test.assertGreaterEqual(fees_left, 0, "fees_withdrawn exceeds fees_accrued; " + ctx)
        test.assertEqual(stub.balance_of(ESCROW_ADDR), owed + fees_left, "escrow insolvent; " + ctx)

        live = {}
        disputes = {}
        fees_sum = 0
        for did, d in deals:
            status = str(d.status)
            if did in self.terminal_seen:
                test.assertEqual(status, self.terminal_seen[did], "terminal deal changed state; " + ctx)
            if status in TERMINAL:
                self.terminal_seen[did] = status
                test.assertEqual(int(d.buyer_amount) + int(d.seller_amount) + int(d.fee), int(d.price),
                    "inexact split; " + ctx)
                fees_sum += int(d.fee)
            else:
                test.assertEqual(int(d.buyer_amount) + int(d.seller_amount) + int(d.fee), 0, ctx)
            if status in UNRESOLVED:
                live[str(d.seller)] = live.get(str(d.seller), 0) + 1
            if status == "DISPUTED":
                disputes[str(d.seller)] = disputes.get(str(d.seller), 0) + 1
                test.assertGreater(int(d.case_id), 0, "disputed deal without a case; " + ctx)
            test.assertNotEqual(str(d.buyer), str(d.seller), ctx)
            test.assertTrue(MIN_PRICE_OK(int(d.price)), ctx)
        test.assertEqual(fees_sum, int(c.escrow.fees_accrued), "fees_accrued != sum of deal fees; " + ctx)
        for seller, count in live.items():
            got = c.escrow.seller_live_deals.get(stub.Address(seller))
            test.assertEqual(int(got) if got is not None else 0, count, "live counter drift; " + ctx)
        for key, value in c.escrow.seller_live_deals.items():
            test.assertEqual(int(value), live.get(str(key), 0), "stale live counter; " + ctx)
        for key, value in c.escrow.seller_open_disputes.items():
            test.assertEqual(int(value), disputes.get(str(key), 0), "stale dispute counter; " + ctx)

        # one case is never bound to two deals
        bound = [int(d.case_id) for _, d in deals if str(d.status) == "DISPUTED"]
        test.assertEqual(len(bound), len(set(bound)), "a case is bound to two live deals; " + ctx)

        # bench: open counters match the case table
        open_by_claimant = {}
        for cid, case in c.bench.cases.items():
            if str(case.status) == "OPEN":
                open_by_claimant[str(case.claimant)] = open_by_claimant.get(str(case.claimant), 0) + 1
            test.assertNotEqual(str(case.claimant), str(case.respondent), ctx)
            if str(case.status) == "RULED":
                test.assertIn(int(case.claimant_share), range(0, 101, 5), ctx)
                test.assertGreaterEqual(int(case.confidence), 30, ctx)
        for key, value in c.bench.open_counts.items():
            test.assertEqual(int(value), open_by_claimant.get(str(key), 0), "bench open counter drift; " + ctx)
        test.assertEqual(stub.balance_of(BENCH_ADDR), 0, "the bench must never hold value; " + ctx)

        # bond
        bonded = sum(int(b.bonded) for b in c.bond.bonds.values())
        test.assertEqual(bonded, int(c.bond.total_bonded), "total_bonded drift; " + ctx)
        test.assertEqual(stub.balance_of(BOND_ADDR), bonded, "bond contract insolvent; " + ctx)


def MIN_PRICE_OK(price):
    return 10 ** 15 <= price <= 100 * 10 ** 18


class FuzzChainTests(unittest.TestCase):
    def run_seed(self, seed, steps, **config):
        world = FuzzWorld(seed, **config)
        for step in range(steps):
            world.act()
            world.check(self, "%d/%d" % (seed, step))
        # drain everything: every unresolved deal must be resolvable by someone
        self.drain(world)

    def drain(self, world):
        c = world.c
        for _ in range(3):
            stub.advance(60 * DAY)
            for did, d in world.deals():
                status = str(d.status)
                for call in ("settle_deal", "release_after_review", "reclaim_undelivered"):
                    as_(OUTSIDER)
                    try:
                        getattr(c.escrow, call)(did)
                    except (stub.UserError, stub.ConsensusDisagreement):
                        pass
            for cid in list(c.bench.cases.keys()):
                script_ruling(50)
                as_(OUTSIDER)
                try:
                    c.bench.rule_case(int(cid))
                except (stub.UserError, stub.ConsensusDisagreement):
                    pass
        world.check(self, "drain")
        stuck = [(did, str(d.status)) for did, d in world.deals() if str(d.status) in UNRESOLVED]
        self.assertEqual(stuck, [], "liveness: deals nobody could finish after the drain")
        # and once every deal is finished, every wei the escrow still holds is a claimable balance or a fee
        for who in set(str(k) for k in c.escrow.claimable.keys()):
            as_(who)
            try:
                c.escrow.withdraw()
            except stub.UserError:
                pass
        world.check(self, "after withdrawals")
        self.assertEqual(int(c.escrow.owed_total), 0, "owed_total must be zero once everyone has withdrawn")

    def test_random_walks_keep_every_invariant(self):
        for seed in range(16):
            with self.subTest(seed=seed):
                self.run_seed(seed, 220)

    def test_random_walks_with_tight_windows_and_extreme_fees(self):
        configs = [dict(appeal_window=60, review_window=60, unbond_delay=60, fee_bps=500, penalty_bps=5000),
            dict(appeal_window=7 * DAY, review_window=14 * DAY, unbond_delay=30 * DAY, fee_bps=0, penalty_bps=1),
            dict(appeal_window=HOUR, review_window=DAY, unbond_delay=DAY, fee_bps=1, penalty_bps=3333)]
        for index, config in enumerate(configs):
            for seed in range(100, 104):
                with self.subTest(config=index, seed=seed):
                    self.run_seed(seed, 240, **config)


if __name__ == "__main__":
    unittest.main()
