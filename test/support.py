"""
Shared fixtures for the offline test suite: well-known actors and contract
addresses, loaders for the three contracts, a scripted "world" for the
non-deterministic primitives, and a small `Chain` helper that wires the real
ArbitrationBench -> DealEscrow -> MerchantBond exactly the way three
`genlayer deploy` calls would, each constructed with the address of the
contract before it.
"""
import json
from pathlib import Path

import genlayer_stub as stub

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"

GEN = 10 ** 18
MINUTE = 60
HOUR = 3600
DAY = 86400

OWNER = "0x" + "11" * 20
BUYER = "0x" + "22" * 20
SELLER = "0x" + "33" * 20
OUTSIDER = "0x" + "44" * 20
OTHER_BUYER = "0x" + "55" * 20
OTHER_SELLER = "0x" + "66" * 20

BENCH_ADDR = "0x" + "b0" * 20
ESCROW_ADDR = "0x" + "b1" * 20
BOND_ADDR = "0x" + "b2" * 20

CLAIM_URL = "https://claim.example/evidence"
RESPONSE_URL = "https://reply.example/evidence"
DEFAULT_PAGES = {"claim.example": "The buyer's evidence: only half of the files arrived.",
    "reply.example": "The seller's evidence: everything was delivered on time."}

TERMS = "Deliver a complete 4k logo pack as PNG and SVG files."


def load_bench(name="bench_under_test"):
    return stub.load_contract(CONTRACTS_DIR / "ArbitrationBench.py", name)


def load_escrow(name="escrow_under_test"):
    return stub.load_contract(CONTRACTS_DIR / "DealEscrow.py", name)


def load_bond(name="bond_under_test"):
    return stub.load_contract(CONTRACTS_DIR / "MerchantBond.py", name)


def as_(sender, value=0):
    stub.send_as(sender, value)


def j(raw):
    return json.loads(raw)


def script_ruling(share, confidence=90, reasoning="the evidence supports this share", pages=None,
        validator_share=None, validator_confidence=None):
    """Scripts what the leader's fetches and LLM return. When
    validator_share is given, the validator's own re-run sees a different
    LLM answer, which is how disagreement is simulated."""
    stub.LEADER.pages = dict(pages if pages is not None else DEFAULT_PAGES)
    stub.LEADER.llm = {"claimant_share": share, "confidence": confidence, "reasoning": reasoning}
    stub.VALIDATOR.pages = None
    stub.VALIDATOR.llm = None
    if validator_share is not None:
        stub.VALIDATOR.pages = dict(stub.LEADER.pages)
        stub.VALIDATOR.llm = {"claimant_share": validator_share,
            "confidence": validator_confidence if validator_confidence is not None else confidence,
            "reasoning": reasoning}


class Chain:
    """The real three-contract chain on the offline stub."""

    def __init__(self, appeal_window=HOUR, fee_bps=250, review_window=DAY, penalty_bps=2000,
            unbond_delay=DAY):
        stub.reset()
        self.bench_mod = load_bench("chain_bench")
        self.escrow_mod = load_escrow("chain_escrow")
        self.bond_mod = load_bond("chain_bond")
        self.bench = stub.deploy(self.bench_mod.ArbitrationBench, BENCH_ADDR, appeal_window,
            sender=OWNER)
        self.escrow = stub.deploy(self.escrow_mod.DealEscrow, ESCROW_ADDR, BENCH_ADDR, fee_bps,
            review_window, sender=OWNER)
        self.bond = stub.deploy(self.bond_mod.MerchantBond, BOND_ADDR, ESCROW_ADDR, penalty_bps,
            unbond_delay, sender=OWNER)
        script_ruling(50)

    # -- bonds -- #
    def post_bond(self, seller, amount):
        as_(seller, amount)
        return j(self.bond.post_bond())

    # -- deals -- #
    def open_deal(self, buyer=BUYER, seller=SELLER, price=5 * GEN, terms=TERMS, window=DAY):
        as_(buyer, price)
        out = j(self.escrow.open_deal(seller, terms, window))
        assert out["ok"], out
        return out["deal_id"]

    def deliver(self, deal_id, seller=SELLER):
        as_(seller)
        return j(self.escrow.mark_delivered(deal_id, "delivered"))

    def open_case_for(self, deal_id, claimant=BUYER, respondent=SELLER, window=DAY,
            claim_url=CLAIM_URL):
        template = j(self.escrow.get_dispute_template(deal_id))
        as_(claimant)
        out = j(self.bench.open_case(respondent, template["subject"], claim_url,
            template["standard"], window))
        return out["case_id"]

    def dispute(self, deal_id, claimant=BUYER, respondent=SELLER, respond=True):
        """Opens a case for the deal, attaches it, and lets the respondent
        answer. Returns the case id."""
        case_id = self.open_case_for(deal_id, claimant, respondent)
        as_(claimant)
        j(self.escrow.attach_case(deal_id, case_id))
        if respond:
            as_(respondent)
            j(self.bench.respond(case_id, RESPONSE_URL))
        return case_id

    def rule(self, case_id, share=80, **kwargs):
        script_ruling(share, **kwargs)
        as_(OUTSIDER)
        return j(self.bench.rule_case(case_id))

    def finalize(self):
        """Lets any provisional ruling's appeal window run out."""
        stub.advance(self.bench.appeal_window + 1)

    def settle(self, deal_id):
        as_(OUTSIDER)
        return j(self.escrow.settle_deal(deal_id))

    def withdraw(self, who):
        as_(who)
        return j(self.escrow.withdraw())

    # -- invariants -- #
    def assert_solvent(self, test):
        escrow_owed = int(self.escrow.owed_total)
        fees_left = int(self.escrow.fees_accrued) - int(self.escrow.fees_withdrawn)
        test.assertEqual(stub.balance_of(ESCROW_ADDR), escrow_owed + fees_left,
            "DealEscrow balance must equal what it owes plus the fees it has not swept")
        test.assertEqual(stub.balance_of(BOND_ADDR), int(self.bond.total_bonded),
            "MerchantBond balance must equal the sum of all bonds")


# --------------------------------------------------------------------------- #
# stand-ins for the neighbouring contracts, used by the single-contract suites
# --------------------------------------------------------------------------- #

class FakeBench(stub.gl.Contract):
    """Stands in for ArbitrationBench: serves exactly the case briefs a test
    installs, so DealEscrow can be exercised without a real bench."""

    @stub.gl.public.view
    def get_case_brief(self, case_id: int) -> str:
        raw = self._raw.get(int(case_id))
        if raw is not None:
            return raw
        brief = self._briefs.get(int(case_id))
        return json.dumps(brief if brief is not None else {"found": False})


def deploy_fake_bench(address=BENCH_ADDR):
    fake = stub.deploy(FakeBench, address, sender=OWNER)
    fake._briefs = {}
    fake._raw = {}
    return fake


def make_brief(case_id, claimant, respondent, subject, standard, phase="OPEN", opened_epoch=0,
        claimant_share=-1, response_window=DAY):
    return {"found": True, "case_id": case_id, "claimant": claimant, "respondent": respondent,
        "subject": subject, "standard": standard, "phase": phase, "opened_epoch": opened_epoch,
        "response_deadline": opened_epoch + response_window,
        "claimant_share": claimant_share, "final": phase == "FINAL"}


class FakeEscrow(stub.gl.Contract):
    """Stands in for DealEscrow: serves exactly the deal briefs and seller
    exposure figures a test installs, so MerchantBond can be exercised
    without a real escrow."""

    @stub.gl.public.view
    def get_deal_brief(self, deal_id: int) -> str:
        raw = self._raw.get(int(deal_id))
        if raw is not None:
            return raw
        brief = self._deals.get(int(deal_id))
        return json.dumps(brief if brief is not None else {"found": False})

    @stub.gl.public.view
    def get_seller_exposure(self, seller: str) -> str:
        data = self._exposure.get(str(seller).lower(), {})
        return json.dumps({"seller": str(seller), "live_deals": data.get("live_deals", 0),
            "open_disputes": data.get("open_disputes", 0),
            "last_adverse_epoch": data.get("last_adverse_epoch", 0)})


def deploy_fake_escrow(address=ESCROW_ADDR):
    fake = stub.deploy(FakeEscrow, address, sender=OWNER)
    fake._deals = {}
    fake._raw = {}
    fake._exposure = {}
    return fake


def make_deal_brief(deal_id, buyer, seller, price, status="SETTLED", resolution="RULING", buyer_pct=80,
        resolved_epoch=0):
    buyer_amount = price * buyer_pct // 100
    return {"found": True, "deal_id": deal_id, "buyer": buyer, "seller": seller, "price": str(price),
        "status": status, "resolution": resolution, "buyer_pct": buyer_pct,
        "buyer_amount": str(buyer_amount), "resolved_epoch": resolved_epoch}
