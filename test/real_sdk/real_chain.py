"""
A multi-contract, in-process harness that runs the three TrustHarbor
contracts against the REAL GenLayer Python SDK instead of the offline stub.

It is an optional extra, not part of the default test run: it needs the
`genlayer-test` package (which provides the `gltest.direct` and `glsim`
modules) plus a one-time download of the GenVM runner artifacts that the
official linter also uses. See README.md ("Optional: tests against the real
SDK") for the exact commands.

What it adds on top of glsim's own engine:

  * a balance ledger, so payable calls credit the contract and
    `emit_transfer` debits it (glsim ignores plain value transfers);
  * rollback of every contract's storage and of the ledger when a call
    raises, like a reverted transaction;
  * leader / validator emulation: after each write call it replays every
    captured validator function against a separately scripted "validator
    view of the world", so a test can make the validator's LLM or web fetch
    disagree with the leader's.
"""
import datetime
import hashlib
import json
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parent.parent.parent / "contracts"


def make_address(seed):
    """A deterministic 0x-prefixed 20-byte hex address for a test actor."""
    return "0x" + hashlib.sha256(str(seed).encode()).hexdigest()[:40]


def _addr_hex(address):
    if isinstance(address, str):
        return address
    if hasattr(address, "as_hex"):
        return address.as_hex
    return "0x" + bytes(address).hex()


class Scripted:
    """Per-call scripted answers for the two non-deterministic primitives.

    `pages` maps a url substring to page text (or to an Exception instance to
    simulate a failed fetch); `llm` is either a JSON-able dict, a raw string,
    an Exception instance, or a callable(prompt) returning one of those."""

    def __init__(self):
        self.pages = {}
        self.llm = None
        self.prompts = []

    def web(self, url):
        for needle, body in self.pages.items():
            if needle in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise RuntimeError("no scripted page for " + url)

    def answer(self, prompt):
        self.prompts.append(prompt)
        value = self.llm(prompt) if callable(self.llm) else self.llm
        if isinstance(value, Exception):
            raise value
        return value


class RealChain:
    def __init__(self):
        from glsim.engine import SimEngine
        from glsim.state import StateStore

        self.leader = Scripted()
        self.validator = Scripted()
        self._active = self.leader
        self.state = StateStore(seed="trustharbor")

        def web_handler(request):
            body = self._active.web(request["url"])
            return {"ok": {"response": {"status": 200, "headers": {}, "body": body.encode()}}}

        def llm_handler(data):
            try:
                value = self._active.answer(data.get("prompt", ""))
            except Exception as e:
                return {"error": {"message": str(e)}}
            return {"ok": value}

        self.engine = SimEngine(self.state, web_handler=web_handler, llm_handler=llm_handler)
        self.engine.activate()
        self.vm = self.engine.vm
        self.balances = self.vm._balances
        self.payments = []
        self.captured = []
        self._wrap_hook()
        self.contracts = {}
        self._clock = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
        self._push_clock()

    # ---------------- clock ---------------- #

    def _push_clock(self):
        self.vm.warp(self._clock.isoformat().replace("+00:00", "Z"))

    @property
    def now(self):
        return int(self._clock.timestamp())

    def advance(self, seconds):
        self._clock += datetime.timedelta(seconds=int(seconds))
        self._push_clock()

    # ---------------- value transfers ---------------- #

    def _wrap_hook(self):
        original = self.vm._gl_call_hook

        def hook(vm, request):
            if "PostMessage" in request:
                data = request["PostMessage"]
                calldata = data.get("calldata") or {}
                value = int(data.get("value", 0) or 0)
                if not calldata.get("method") and value > 0:
                    sender = vm._contract_address
                    target = data["address"]
                    target_bytes = target.as_bytes if hasattr(target, "as_bytes") else bytes(target)
                    have = self.balances.get(sender, 0)
                    if have < value:
                        raise RuntimeError("insufficient contract balance for transfer: "
                            + str(have) + " < " + str(value))
                    self.balances[sender] = have - value
                    self.balances[target_bytes] = self.balances.get(target_bytes, 0) + value
                    self.payments.append(("0x" + target_bytes.hex(), value))
                    return {"ok": None}
            return original(vm, request)

        self.vm._gl_call_hook = hook

    def balance_of(self, address):
        raw = _addr_hex(address)
        return self.balances.get(bytes.fromhex(raw[2:]), 0)

    def fund(self, address, amount):
        raw = _addr_hex(address)
        self.balances[bytes.fromhex(raw[2:])] = int(amount)

    def paid_to(self, address):
        raw = _addr_hex(address).lower()
        return sum(v for (a, v) in self.payments if a.lower() == raw)

    # ---------------- deploy / call ---------------- #

    def deploy(self, name, *args, sender, **kwargs):
        path = CONTRACTS_DIR / (name + ".py")
        addr, instance = self.engine.deploy(str(path), list(args), dict(kwargs),
            sender=_addr_hex(sender))
        self.contracts[name] = addr
        return addr

    def _snapshot(self):
        return ({k: v.snapshot() for k, v in self.engine._storages.items()}, dict(self.balances))

    def _restore(self, snap):
        storages, balances = snap
        for key, data in storages.items():
            self.engine._storages[key].restore(data)
        self.balances.clear()
        self.balances.update(balances)

    def call(self, address, method, *args, sender, value=0, validator_agrees_with=None):
        """Runs one transaction. Returns the method's return value (decoded
        from JSON when it is a JSON string). `validator_agrees_with`, when
        given, is a list that receives one bool per validator function the
        call captured (each replayed against self.validator's script)."""
        snap = self._snapshot()
        self.vm._captured_validators.clear()
        contract_bytes = bytes.fromhex(address[2:])
        if value:
            self.balances[contract_bytes] = self.balances.get(contract_bytes, 0) + int(value)
        self.vm.value = int(value)
        try:
            result = self.engine.call_method(address, method, list(args), {}, sender=_addr_hex(sender))
        except BaseException:
            self._restore(snap)
            raise
        finally:
            self.vm.value = 0
        self.captured = list(self.vm._captured_validators)
        if validator_agrees_with is not None:
            validator_agrees_with.extend(self.replay_validators())
        return result

    def replay_validators(self):
        verdicts = []
        previous = self._active
        self._active = self.validator
        try:
            for index in range(len(self.vm._captured_validators)):
                verdicts.append(bool(self.vm.run_validator(index=index)))
        finally:
            self._active = previous
        return verdicts

    def read(self, address, method, *args):
        """A view call; never changes storage."""
        snap = self._snapshot()
        try:
            return self.engine.call_method(address, method, list(args), {},
                sender="0x" + "00" * 19 + "01")
        finally:
            self._restore(snap)


def jload(value):
    return json.loads(value) if isinstance(value, str) else value
