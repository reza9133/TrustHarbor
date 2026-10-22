"""
Optional end-to-end tests that run the three contracts against the REAL
GenLayer Python SDK (through the `glsim` engine that ships with the
`genlayer-test` package) instead of the offline stand-in.

They are skipped automatically when `glsim` cannot be imported, so the plain
`python3 -m unittest discover -s test` run never depends on them. To run them:

    pip install genlayer-test
    python3 -m unittest discover -s test/real_sdk -v

See README.md ("Optional: tests against the real SDK") for the one-time
runner download that the official linter and the engine share.
"""
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
DAY = 86400
TERMS = "Deliver a complete 4k logo pack as PNG and SVG files."
BUYER_PAGE = "https://evidence.example/buyer"
SELLER_PAGE = "https://evidence.example/seller"


def message_of(error):
    return getattr(error, "message", str(error))


@unittest.skipUnless(HAVE_REAL_SDK, "genlayer-test (glsim) is not installed")
class RealSdkChainTests(unittest.TestCase):
    def setUp(self):
        self.c = RealChain()
        self.deployer = make_address("deployer")
        self.buyer = make_address("buyer")
        self.seller = make_address("seller")
        self.stranger = make_address("stranger")
        self.bench = self.c.deploy("ArbitrationBench", 3600, sender=self.deployer)
        self.escrow = self.c.deploy("DealEscrow", self.bench, sender=self.deployer)
        self.bond = self.c.deploy("MerchantBond", self.escrow, sender=self.deployer)
        self.c.leader.pages = {"evidence.example/buyer": "Only the PNG files arrived.",
            "evidence.example/seller": "Everything was delivered on time."}
        self.c.validator.pages = dict(self.c.leader.pages)

    def tearDown(self):
        c = self.c
        stats = jload(c.read(self.escrow, "get_platform_stats"))
        self.assertEqual(c.balance_of(self.escrow), int(stats["owed_total"]) + int(stats["fees_available"]))
        bond_stats = jload(c.read(self.bond, "get_stats"))
        self.assertEqual(c.balance_of(self.bond), int(bond_stats["total_bonded"]))

    # -- helpers -- #

    def script(self, share, confidence=85, validator_share=None):
        c = self.c
        c.leader.llm = {"claimant_share": share, "confidence": confidence, "reasoning": "scripted"}
        c.validator.llm = {"claimant_share": share if validator_share is None else validator_share,
            "confidence": confidence, "reasoning": "scripted"}

    def new_delivered_deal(self, price=5 * GEN):
        c = self.c
        out = jload(c.call(self.escrow, "open_deal", self.seller, TERMS, DAY, sender=self.buyer, value=price))
        self.assertTrue(out["ok"], out)
        deal_id = out["deal_id"]
        c.call(self.escrow, "mark_delivered", deal_id, "sent", sender=self.seller)
        return deal_id

    def open_and_attach(self, deal_id):
        c = self.c
        template = jload(c.read(self.escrow, "get_dispute_template", deal_id))
        case = jload(c.call(self.bench, "open_case", self.seller, template["subject"], BUYER_PAGE,
            template["standard"], DAY, sender=self.buyer))
        case_id = case["case_id"]
        c.call(self.escrow, "attach_case", deal_id, case_id, sender=self.buyer)
        c.call(self.bench, "respond", case_id, SELLER_PAGE, sender=self.seller)
        return case_id

    # -- tests -- #

    def test_full_flow_with_validator_replay_and_compensation(self):
        c = self.c
        c.fund(self.buyer, 100 * GEN)
        c.fund(self.seller, 100 * GEN)
        self.assertTrue(jload(c.call(self.bond, "post_bond", sender=self.seller, value=2 * GEN))["ok"])
        deal_id = self.new_delivered_deal()
        case_id = self.open_and_attach(deal_id)

        self.script(80, validator_share=75)
        verdicts = []
        ruling = jload(c.call(self.bench, "rule_case", case_id, sender=self.deployer, validator_agrees_with=verdicts))
        self.assertTrue(ruling["ok"])
        self.assertEqual(ruling["claimant_share"], 80)
        self.assertEqual(verdicts, [True], "a validator 5 points away agrees")

        c.advance(3601)
        settled = jload(c.call(self.escrow, "settle_deal", deal_id, sender=self.stranger))
        buyer_amount = 5 * GEN * 80 // 100
        gross = 5 * GEN - buyer_amount
        fee = gross * 250 // 10000
        self.assertEqual((int(settled["buyer_amount"]), int(settled["seller_amount"]), int(settled["fee"])),
            (buyer_amount, gross - fee, fee))

        self.assertEqual(jload(c.call(self.escrow, "withdraw", sender=self.buyer))["withdrawn"], str(buyer_amount))
        self.assertEqual(jload(c.call(self.escrow, "withdraw", sender=self.seller))["withdrawn"], str(gross - fee))
        claim = jload(c.call(self.bond, "claim_compensation", deal_id, sender=self.buyer))
        self.assertEqual(claim["paid"], str(buyer_amount * 2000 // 10000))
        self.assertEqual(c.paid_to(self.buyer), buyer_amount + buyer_amount * 2000 // 10000)

    def test_an_appeal_replaces_the_ruling_and_the_deal_follows_it(self):
        c = self.c
        deal_id = self.new_delivered_deal()
        case_id = self.open_and_attach(deal_id)
        self.script(80)
        c.call(self.bench, "rule_case", case_id, sender=self.deployer)
        self.script(20)
        appeal = jload(c.call(self.bench, "appeal_case", case_id, sender=self.seller))
        self.assertEqual((appeal["first_share"], appeal["claimant_share"], appeal["phase"]), (80, 20, "FINAL"))
        settled = jload(c.call(self.escrow, "settle_deal", deal_id, sender=self.stranger))
        self.assertEqual(settled["buyer_pct"], 20)
        self.assertEqual(settled["buyer_amount"], str(5 * GEN * 20 // 100))

    def test_a_validator_far_from_the_leader_disagrees(self):
        c = self.c
        deal_id = self.new_delivered_deal()
        case_id = self.open_and_attach(deal_id)
        self.script(80, validator_share=40)
        verdicts = []
        c.call(self.bench, "rule_case", case_id, sender=self.deployer, validator_agrees_with=verdicts)
        self.assertEqual(verdicts, [False])

    def test_a_ruled_case_cannot_be_attached_and_an_open_one_can(self):
        c = self.c
        deal_id = self.new_delivered_deal()
        template = jload(c.read(self.escrow, "get_dispute_template", deal_id))
        case_id = jload(c.call(self.bench, "open_case", self.seller, template["subject"], BUYER_PAGE,
            template["standard"], DAY, sender=self.buyer))["case_id"]
        c.call(self.bench, "respond", case_id, SELLER_PAGE, sender=self.seller)
        self.script(100)
        c.call(self.bench, "rule_case", case_id, sender=self.deployer)
        with self.assertRaises(Exception) as ctx:
            c.call(self.escrow, "attach_case", deal_id, case_id, sender=self.buyer)
        self.assertIn("before it is ruled", message_of(ctx.exception))

    def test_a_case_that_names_another_deal_is_refused(self):
        c = self.c
        first = self.new_delivered_deal()
        second = self.new_delivered_deal()
        template = jload(c.read(self.escrow, "get_dispute_template", first))
        case_id = jload(c.call(self.bench, "open_case", self.seller, template["subject"], BUYER_PAGE,
            template["standard"], DAY, sender=self.buyer))["case_id"]
        with self.assertRaises(Exception) as ctx:
            c.call(self.escrow, "attach_case", second, case_id, sender=self.buyer)
        self.assertIn("subject does not name", message_of(ctx.exception))

    def test_bond_stays_locked_while_a_deal_is_unresolved(self):
        c = self.c
        c.call(self.bond, "post_bond", sender=self.seller, value=GEN)
        jload(c.call(self.bond, "request_unbond", GEN, sender=self.seller))
        deal_id = self.new_delivered_deal()
        c.advance(7 * DAY + 1)
        with self.assertRaises(Exception) as ctx:
            c.call(self.bond, "withdraw_unbonded", sender=self.seller)
        self.assertIn("unresolved deal", message_of(ctx.exception))
        c.call(self.escrow, "confirm_receipt", deal_id, sender=self.buyer)
        out = jload(c.call(self.bond, "withdraw_unbonded", sender=self.seller))
        self.assertEqual(out["withdrawn"], str(GEN))

    def test_payable_methods_refund_instead_of_reverting(self):
        c = self.c
        out = jload(c.call(self.escrow, "open_deal", self.seller, "", DAY, sender=self.buyer, value=GEN))
        self.assertFalse(out["ok"])
        self.assertEqual(c.paid_to(self.buyer), GEN)
        self.assertEqual(c.balance_of(self.escrow), 0)
        out = jload(c.call(self.bond, "post_bond", sender=self.seller, value=1))
        self.assertFalse(out["ok"])
        self.assertEqual(c.paid_to(self.seller), 1)
        self.assertEqual(c.balance_of(self.bond), 0)


if __name__ == "__main__":
    unittest.main()
