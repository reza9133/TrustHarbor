"""
Randomised walk over the three contracts on the REAL GenLayer Python SDK
(through the `glsim` engine of the `genlayer-test` package). It plays the
same role as test/test_fuzz_chain.py does on the offline stand-in, but every
invariant is read back through the contracts' own public views, so it also
proves the views stay consistent with the money.

Skipped automatically when `glsim` is not installed.

    pip install genlayer-test numpy
    python3 -m unittest discover -s test/real_sdk -v
"""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import glsim  # noqa: F401
    from real_chain import RealChain, jload, make_address
    HAVE_REAL_SDK = True
except Exception:  # pragma: no cover - depends on the environment
    HAVE_REAL_SDK = False

GEN = 10 ** 18
HOUR = 3600
DAY = 86400
TERMS = "Deliver a complete 4k logo pack as PNG and SVG files."
CLAIM_URL = "https://evidence.example/buyer"
RESPONSE_URL = "https://evidence.example/seller"
UNRESOLVED = {"FUNDED", "DELIVERED", "DISPUTED"}


@unittest.skipUnless(HAVE_REAL_SDK, "genlayer-test (glsim) is not installed")
class RealSdkFuzzTests(unittest.TestCase):
    def build(self, seed):
        self.rng = random.Random(seed)
        c = self.c = RealChain()
        self.owner = make_address("owner")
        self.actors = [make_address("actor-%d" % i) for i in range(3)]
        self.stranger = make_address("stranger")
        self.bench = c.deploy("ArbitrationBench", 3600, sender=self.owner)
        self.escrow = c.deploy("DealEscrow", self.bench, sender=self.owner)
        self.bond = c.deploy("MerchantBond", self.escrow, sender=self.owner)
        c.leader.pages = {"evidence.example/buyer": "Only the PNG files arrived.",
            "evidence.example/seller": "Everything was delivered on time."}
        c.validator.pages = dict(c.leader.pages)
        self.deal_ids = []
        self.cases = {}

    # -- plumbing -- #

    def tx(self, address, method, *args, sender, value=0):
        try:
            out = self.c.call(address, method, *args, sender=sender, value=value)
        except Exception as e:  # a revert is an acceptable outcome of a hostile call
            if "does not fit" in str(e) or "Overflow" in type(e).__name__:
                self.fail("integer-width error leaked out of " + method + ": " + str(e))
            return None
        return jload(out) if isinstance(out, str) else out

    def script(self, share, confidence, validator_share=None):
        c = self.c
        c.leader.llm = {"claimant_share": share, "confidence": confidence, "reasoning": "scripted"}
        v = share if validator_share is None else validator_share
        c.validator.llm = {"claimant_share": v, "confidence": confidence, "reasoning": "scripted"}

    def deals_of(self, who):
        out = []
        offset = 0
        while True:
            page = jload(self.c.read(self.escrow, "get_deals_by_party", who, offset, 40))
            out.extend(page["deals"])
            offset += 40
            if offset >= page["total"]:
                return out

    def all_deals(self):
        seen = {}
        for who in self.actors:
            for d in self.deals_of(who):
                seen[d["deal_id"]] = d
        return list(seen.values())

    # -- one random step -- #

    def step(self):
        rng, c = self.rng, self.c
        deals = self.all_deals()
        if not deals or rng.random() < 0.15:
            buyer = rng.choice(self.actors)
            seller = rng.choice([a for a in self.actors if a != buyer])
            out = self.tx(self.escrow, "open_deal", seller, TERMS, rng.choice([DAY, 3 * DAY]),
                sender=buyer, value=rng.choice([GEN, 3 * GEN + 11, 10 ** 15]))
            if out and out.get("ok"):
                self.deal_ids.append(out["deal_id"])
            if rng.random() < 0.5:
                self.tx(self.bond, "post_bond", sender=seller, value=rng.choice([GEN, 2 * GEN]))
            return
        d = rng.choice(deals)
        did, buyer, seller, status = d["deal_id"], d["buyer"], d["seller"], d["status"]
        roll = rng.random()
        if roll < 0.04:
            # hostile ids must revert or answer "not found", never leak an integer error
            for bad in (-1, 2 ** 40, 2 ** 70):
                self.tx(self.escrow, "get_deal", bad, sender=self.stranger)
                self.tx(self.bench, "get_case", bad, sender=self.stranger)
                self.tx(self.escrow, "settle_deal", bad, sender=self.stranger)
                self.tx(self.bench, "rule_case", bad, sender=self.stranger)
                self.tx(self.bond, "claim_compensation", bad, sender=self.stranger)
            return
        if status == "FUNDED":
            choice = rng.choice(["deliver"] * 6 + ["refund", "confirm", "reclaim"])
        elif status == "DELIVERED":
            choice = rng.choice(["dispute"] * 5 + ["confirm", "release", "refund", "claim"])
        elif status == "DISPUTED":
            choice = rng.choice(["rule"] * 3 + ["appeal", "settle", "settle", "respond", "withdraw_case"])
        else:
            choice = rng.choice(["claim", "withdraw", "withdraw"])

        if choice == "deliver":
            self.tx(self.escrow, "mark_delivered", did, "sent", sender=seller)
        elif choice == "refund":
            self.tx(self.escrow, "refund_buyer", did, sender=seller)
        elif choice == "confirm":
            self.tx(self.escrow, "confirm_receipt", did, sender=buyer)
        elif choice == "reclaim":
            c.advance(rng.choice([HOUR, 2 * DAY]))
            self.tx(self.escrow, "reclaim_undelivered", did, sender=self.stranger)
        elif choice == "release":
            c.advance(rng.choice([HOUR, 2 * DAY]))
            self.tx(self.escrow, "release_after_review", did, sender=self.stranger)
        elif choice == "dispute":
            claimant = rng.choice([buyer, seller])
            respondent = seller if claimant == buyer else buyer
            tpl = jload(c.read(self.escrow, "get_dispute_template", did))
            case = self.tx(self.bench, "open_case", respondent, tpl["subject"], CLAIM_URL, tpl["standard"],
                rng.choice([DAY, 2 * DAY, HOUR]), sender=claimant)
            if case and case.get("ok"):
                self.cases[did] = case["case_id"]
                self.tx(self.escrow, "attach_case", did, case["case_id"], sender=claimant)
                if rng.random() < 0.8:
                    self.tx(self.bench, "respond", case["case_id"], RESPONSE_URL, sender=respondent)
        elif choice == "rule" and did in self.cases:
            share = rng.choice([0, 20, 50, 80, 100])
            self.script(share, rng.choice([90, 90, 20]), rng.choice([None, None, min(100, share + 10)]))
            self.tx(self.bench, "rule_case", self.cases[did], sender=self.stranger)
        elif choice == "appeal" and did in self.cases:
            self.script(rng.choice([0, 50, 100]), 90)
            self.tx(self.bench, "appeal_case", self.cases[did], sender=rng.choice([buyer, seller]))
        elif choice == "settle":
            c.advance(rng.choice([HOUR, HOUR + 1, DAY]))
            self.tx(self.escrow, "settle_deal", did, sender=self.stranger)
        elif choice == "respond" and did in self.cases:
            self.tx(self.bench, "respond", self.cases[did], RESPONSE_URL, sender=rng.choice([buyer, seller]))
        elif choice == "withdraw_case" and did in self.cases:
            self.tx(self.bench, "withdraw_case", self.cases[did], sender=rng.choice([buyer, seller]))
        elif choice == "claim":
            self.tx(self.bond, "claim_compensation", did, sender=buyer)
        elif choice == "withdraw":
            self.tx(self.escrow, "withdraw", sender=rng.choice([buyer, seller]))

        if rng.random() < 0.06:
            who = rng.choice(self.actors)
            self.tx(self.bond, "request_unbond", rng.choice([GEN, 2 * GEN]), sender=who)
            c.advance(rng.choice([HOUR, DAY, 2 * DAY]))
            self.tx(self.bond, "withdraw_unbonded", sender=who)

    # -- invariants, read back through the public views -- #

    def check(self, label):
        c = self.c
        stats = jload(c.read(self.escrow, "get_platform_stats"))
        self.assertEqual(c.balance_of(self.escrow), int(stats["owed_total"]) + int(stats["fees_available"]),
            "escrow balance != owed + fees; " + label)
        deals = self.all_deals()
        claimable = sum(int(c.read(self.escrow, "get_claimable", a)) for a in self.actors)
        unresolved = sum(int(d["price"]) for d in deals if d["status"] in UNRESOLVED)
        self.assertEqual(int(stats["owed_total"]), claimable + unresolved, "owed_total drift; " + label)
        for d in deals:
            if d["status"] in ("RELEASED", "REFUNDED", "SETTLED"):
                self.assertEqual(int(d["buyer_amount"]) + int(d["seller_amount"]) + int(d["fee"]),
                    int(d["price"]), "inexact split; " + label)
        for who in self.actors:
            exposure = jload(c.read(self.escrow, "get_seller_exposure", who))
            mine = [d for d in deals if d["seller"].lower() == who.lower()]
            self.assertEqual(exposure["live_deals"], sum(1 for d in mine if d["status"] in UNRESOLVED),
                "live deal counter drift; " + label)
            self.assertEqual(exposure["open_disputes"], sum(1 for d in mine if d["status"] == "DISPUTED"),
                "dispute counter drift; " + label)
        bond_stats = jload(c.read(self.bond, "get_stats"))
        self.assertEqual(c.balance_of(self.bond), int(bond_stats["total_bonded"]), "bond insolvent; " + label)
        bonded = sum(int(jload(c.read(self.bond, "get_bond", a))["bonded"]) for a in self.actors)
        self.assertEqual(bonded, int(bond_stats["total_bonded"]), "total_bonded drift; " + label)
        self.assertEqual(c.balance_of(self.bench), 0, "the bench must never hold value; " + label)

    def test_random_walks_keep_every_invariant_on_the_real_sdk(self):
        for seed in range(6):
            with self.subTest(seed=seed):
                self.build(seed)
                for i in range(70):
                    self.step()
                    self.check("seed %d step %d" % (seed, i))


if __name__ == "__main__":
    unittest.main()
