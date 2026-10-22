"""
Bridge used by `node run.mjs --local`: it runs the real contracts on the real
GenLayer Python SDK (the `glsim` engine from the `genlayer-test` package) and
answers JSON requests from the Node driver on stdin / stdout, one per line.

Every response line is prefixed with "@@" so that the engine's own log lines on
stdout are never mistaken for answers.

Requires:  pip install genlayer-test numpy
"""
import json
import os
import sys
from pathlib import Path

# The GenLayer engine reads file descriptor 0 and writes its own log lines to
# file descriptor 1, so keep private copies of both for the JSON conversation
# and point the originals somewhere harmless.
_REQUESTS = os.fdopen(os.dup(0), "r")
_RESPONSES = os.fdopen(os.dup(1), "w")
os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
os.dup2(os.open(os.devnull, os.O_WRONLY) if not os.environ.get("E2E_DEBUG") else 2, 1)

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "test" / "real_sdk"))

from real_chain import RealChain, make_address  # noqa: E402

BIG = 2 ** 53
STATE = {"chain": None, "counter": 0, "pages": {}, "llm": None}


def decode(value):
    if isinstance(value, dict) and "$big" in value:
        return int(value["$big"])
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        return {k: decode(v) for k, v in value.items()}
    return value


def encode(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) >= BIG:
        return {"$big": str(value)}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, bytes):
        return "0x" + value.hex()
    return value


def error_text(error):
    return str(getattr(error, "message", error))[:300]


def handle(req):
    op = req["op"]
    chain = STATE["chain"]
    if op == "init":
        STATE["chain"] = chain = RealChain()
        STATE["pages"] = req.get("pages") or {}
        share = int(req.get("share", 80))
        confidence = int(req.get("confidence", 90))
        llm = {"claimant_share": share, "confidence": confidence, "reasoning": "scripted local ruling"}
        chain.leader.pages = dict(STATE["pages"])
        chain.validator.pages = dict(STATE["pages"])
        chain.leader.llm = dict(llm)
        chain.validator.llm = dict(llm)
        return {"engine": "glsim (real GenLayer Python SDK)"}
    if op == "account":
        return {"address": make_address("e2e-" + req["label"])}
    if op == "fund":
        chain.fund(req["address"], int(req["amount"]))
        return {}
    if op == "deploy":
        address = chain.deploy(req["name"], *req["args"], sender=req["sender"])
        STATE["counter"] += 1
        return {"address": address, "hash": "0x%064x" % STATE["counter"]}
    if op == "write":
        STATE["counter"] += 1
        tx = "0x%064x" % STATE["counter"]
        try:
            chain.call(req["address"], req["method"], *req["args"], sender=req["sender"],
                value=int(req.get("value", 0)))
            return {"ok": True, "hash": tx}
        except Exception as e:  # a revert is an ordinary outcome
            return {"ok": False, "hash": tx, "error": error_text(e)}
    if op == "read":
        return {"value": chain.read(req["address"], req["method"], *req["args"])}
    if op == "balance":
        return {"value": chain.balance_of(req["address"])}
    if op == "advance":
        chain.advance(int(req["seconds"]))
        return {}
    raise ValueError("unknown op " + op)


def main():
    for line in _REQUESTS:
        line = line.strip()
        if not line:
            continue
        req = decode(json.loads(line))
        try:
            body = {"id": req["id"], "ok": True, "result": encode(handle(req))}
        except Exception as e:  # noqa: BLE001
            body = {"id": req["id"], "ok": False, "error": error_text(e)}
        _RESPONSES.write("@@" + json.dumps(body) + "\n")
        _RESPONSES.flush()


if __name__ == "__main__":
    main()
