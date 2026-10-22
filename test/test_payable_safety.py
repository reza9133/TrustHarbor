"""
Static checks on the three contract files, done by walking their syntax
trees rather than by running them.

The rule being enforced: a method that can receive GEN must never be a reason
for that GEN to get stuck. Concretely, every `@gl.public.write.payable`
method, and everything it reaches through `self.<helper>()` or a module-level
function, must

  * contain no `raise` (a rejection refunds the sender and returns normally);
  * make no cross-contract call and read nothing from another contract;
  * touch nothing non-deterministic.

The same walk also checks that non-deterministic code never touches storage,
that only the expected methods can move GEN out, and that the files stay free
of ambient sources of non-determinism.

Run with:  python3 -m unittest discover -s test -v
"""
import ast
import unittest
from pathlib import Path

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"

EXPECTED_PAYABLE = {"ArbitrationBench": set(), "DealEscrow": {"open_deal"}, "MerchantBond": {"post_bond"}}
EXPECTED_PAYERS = {"ArbitrationBench": set(), "DealEscrow": {"open_deal", "withdraw", "withdraw_fees"},
    "MerchantBond": {"post_bond", "claim_compensation", "withdraw_unbonded"}}
CROSS_CONTRACT_HELPERS = {"_bench_brief", "_deal_brief", "_exposure", "_escrow"}
INTERFACE_NAMES = {"_ArbitrationBench", "_DealEscrow"}
ALLOWED_IMPORTS = {"genlayer", "dataclasses", "datetime", "json"}


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class Source:
    def __init__(self, name):
        self.name = name
        self.tree = ast.parse((CONTRACTS / (name + ".py")).read_text())
        self.cls = next(n for n in self.tree.body if isinstance(n, ast.ClassDef) and n.name == name)
        self.methods = {n.name: n for n in self.cls.body if isinstance(n, ast.FunctionDef)}
        self.functions = {n.name: n for n in self.tree.body if isinstance(n, ast.FunctionDef)}

    def kind(self, fn):
        for dec in fn.decorator_list:
            name = dotted(dec)
            if name == "gl.public.write.payable":
                return "payable"
            if name == "gl.public.write":
                return "write"
            if name == "gl.public.view":
                return "view"
        return None

    def public(self, kind=None):
        return {n: f for n, f in self.methods.items() if self.kind(f) is not None
            and (kind is None or self.kind(f) == kind)}

    def reach(self, root):
        """Every function node reachable from `root` (inclusive), following
        self.<method>() calls and module-level function calls."""
        seen, order, stack = set(), [], [root]
        while stack:
            fn = stack.pop()
            if fn.name in seen and fn is not root:
                continue
            seen.add(fn.name)
            order.append(fn)
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "self":
                    target = self.methods.get(f.attr)
                elif isinstance(f, ast.Name):
                    target = self.functions.get(f.id)
                else:
                    target = None
                if target is not None and target.name not in seen:
                    stack.append(target)
        return order

    def self_calls(self, fns):
        out = set()
        for fn in fns:
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"):
                    out.add(node.func.attr)
        return out


SOURCES = {name: Source(name) for name in EXPECTED_PAYABLE}


class PayableSafetyTests(unittest.TestCase):
    def each(self):
        for name, src in SOURCES.items():
            yield name, src

    def test_the_payable_methods_are_exactly_the_expected_ones(self):
        for name, src in self.each():
            self.assertEqual(set(src.public("payable")), EXPECTED_PAYABLE[name], name)

    def test_payable_methods_and_everything_they_reach_never_raise(self):
        for name, src in self.each():
            for method, fn in src.public("payable").items():
                for reached in src.reach(fn):
                    raises = [n for n in ast.walk(reached) if isinstance(n, ast.Raise)]
                    self.assertEqual(raises, [], name + "." + method + " reaches a raise in " + reached.name)

    def test_payable_methods_make_no_cross_contract_call(self):
        for name, src in self.each():
            for method, fn in src.public("payable").items():
                reached = src.reach(fn)
                self.assertEqual(src.self_calls(reached) & CROSS_CONTRACT_HELPERS, set(), name + "." + method)
                for node in (n for r in reached for n in ast.walk(r)):
                    if isinstance(node, ast.Attribute):
                        self.assertNotIn(node.attr, {"view", "emit"}, name + "." + method)
                    if isinstance(node, ast.Name):
                        self.assertNotIn(node.id, INTERFACE_NAMES, name + "." + method)

    def test_payable_methods_touch_nothing_non_deterministic(self):
        for name, src in self.each():
            for method, fn in src.public("payable").items():
                for reached in src.reach(fn):
                    for node in ast.walk(reached):
                        label = dotted(node) if isinstance(node, ast.Attribute) else None
                        if label is not None:
                            self.assertFalse(label.startswith("gl.nondet") or label.startswith("gl.vm"),
                                name + "." + method + " uses " + label)

    def test_the_validation_helpers_swallow_every_surprise(self):
        for name, helper in (("DealEscrow", "_open_problem"), ("MerchantBond", "_deposit_problem")):
            fn = SOURCES[name].methods[helper]
            body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
            self.assertEqual(len(body), 1, helper + " must be a single try block")
            self.assertIsInstance(body[0], ast.Try, helper)
            handlers = body[0].handlers
            self.assertEqual(len(handlers), 1)
            self.assertEqual(dotted(handlers[0].type), "Exception", "must catch Exception, not a narrower type")

    def test_payable_methods_start_by_reading_the_attached_value(self):
        for name, src in self.each():
            for method, fn in src.public("payable").items():
                labels = {dotted(n) for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
                self.assertIn("gl.message.value", labels, name + "." + method)

    def test_only_payable_code_reads_the_attached_value(self):
        for name, src in self.each():
            allowed = set()
            for fn in src.public("payable").values():
                allowed.update(f.name for f in src.reach(fn))
            for method, fn in src.methods.items():
                labels = {dotted(n) for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
                if "gl.message.value" in labels:
                    self.assertIn(method, allowed, name + "." + method + " reads msg.value but is not payable")

    # ------------------------------------------------------------------ #

    def test_only_the_expected_methods_can_move_gen_out(self):
        for name, src in self.each():
            payers = set()
            for method, fn in src.public().items():
                if any(r.name == "_pay" for r in src.reach(fn)):
                    payers.add(method)
            self.assertEqual(payers, EXPECTED_PAYERS[name], name)

    def test_emit_transfer_appears_only_inside_the_pay_helper(self):
        for name, src in self.each():
            for fname, fn in list(src.methods.items()) + list(src.functions.items()):
                uses = [n for n in ast.walk(fn) if isinstance(n, ast.Attribute) and n.attr == "emit_transfer"]
                if fname == "_pay":
                    self.assertEqual(len(uses), 1)
                else:
                    self.assertEqual(uses, [], name + "." + fname)

    def test_the_pay_helper_ignores_non_positive_amounts(self):
        for name, src in self.each():
            if "_pay" not in src.methods:
                continue
            first = [s for s in src.methods["_pay"].body if not (isinstance(s, ast.Expr)
                and isinstance(s.value, ast.Constant))][0]
            self.assertIsInstance(first, ast.If, name)
            self.assertEqual(ast.unparse(first.test).replace(" ", ""), "amount<=0")

    def test_no_public_method_lets_the_caller_name_the_recipient_of_contract_money(self):
        """The only public methods with an address parameter that can send
        value are the owner's fee sweep; everything else pays msg.sender."""
        for name, src in self.each():
            for method, fn in src.public().items():
                if not any(r.name == "_pay" for r in src.reach(fn)):
                    continue
                pay_targets = []
                for reached in src.reach(fn):
                    for node in ast.walk(reached):
                        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                                and node.func.attr == "_pay"):
                            pay_targets.append(ast.unparse(node.args[0]))
                params = {a.arg for a in fn.args.args}
                for target in pay_targets:
                    root = target.split(".")[0].split("(")[0]
                    if method == "withdraw_fees":
                        self.assertEqual(target, "target")
                    else:
                        self.assertNotIn(root, params - {"self"}, name + "." + method + " pays to a caller-chosen "
                            "address: " + target)

    # ------------------------------------------------------------------ #

    def test_nondeterministic_helpers_never_touch_self_or_storage(self):
        src = SOURCES["ArbitrationBench"]
        nondet_functions = []
        for fname, fn in src.functions.items():
            labels = {dotted(n) for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
            if any(label and label.startswith("gl.nondet") for label in labels):
                nondet_functions.append(fn)
        self.assertGreaterEqual(len(nondet_functions), 3)
        for fn in nondet_functions:
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            self.assertNotIn("self", names, fn.name)

    def test_the_consensus_closures_do_not_capture_self(self):
        src = SOURCES["ArbitrationBench"]
        arbitrate = src.methods["_arbitrate"]
        closures = [n for n in ast.walk(arbitrate) if isinstance(n, ast.FunctionDef) and n is not arbitrate]
        self.assertEqual({c.name for c in closures}, {"leader_fn", "validator_fn"})
        for closure in closures:
            names = {n.id for n in ast.walk(closure) if isinstance(n, ast.Name)}
            self.assertNotIn("self", names, closure.name)

    def test_no_contract_method_reads_the_web_or_an_llm_directly(self):
        for name, src in self.each():
            for method, fn in src.methods.items():
                labels = {dotted(n) for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
                self.assertFalse({label for label in labels if label and label.startswith("gl.nondet")},
                    name + "." + method)

    def test_escrow_and_bond_have_no_nondeterminism_at_all(self):
        for name in ("DealEscrow", "MerchantBond"):
            labels = {dotted(n) for n in ast.walk(SOURCES[name].tree) if isinstance(n, ast.Attribute)}
            offenders = {label for label in labels if label and (label.startswith("gl.nondet")
                or label.startswith("gl.vm.run_nondet"))}
            self.assertEqual(offenders, set(), name)

    def test_imports_are_limited_to_the_allowed_modules(self):
        for name, src in self.each():
            imported = set()
            for node in ast.walk(src.tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported.add((node.module or "").split(".")[0])
            self.assertLessEqual(imported, ALLOWED_IMPORTS, name)

    def test_the_clock_is_read_in_exactly_one_place_per_contract(self):
        for name, src in self.each():
            holders = []
            for fname, fn in src.functions.items():
                for node in ast.walk(fn):
                    if isinstance(node, ast.Attribute) and node.attr == "now":
                        holders.append(fname)
            self.assertEqual(holders, ["_now_epoch"], name)
            for method, fn in src.methods.items():
                labels = {dotted(n) for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
                self.assertFalse({label for label in labels if label and label.startswith("datetime")}, method)

    def test_the_first_line_pins_the_runner(self):
        for name in SOURCES:
            first = (CONTRACTS / (name + ".py")).read_text().splitlines()[0]
            self.assertTrue(first.startswith('# { "Depends": "py-genlayer:'), name)

    def test_no_contract_can_be_repointed_or_drained_by_an_admin(self):
        forbidden = {"set_bench", "set_escrow", "set_bench_address", "set_escrow_address", "rescue", "sweep",
            "emergency_withdraw", "withdraw_all", "admin_slash", "slash", "selfdestruct"}
        for name, src in self.each():
            self.assertEqual(set(src.methods) & forbidden, set(), name)

    def test_views_never_assign_to_self(self):
        for name, src in self.each():
            for method, fn in src.public("view").items():
                for node in ast.walk(fn):
                    targets = []
                    if isinstance(node, ast.Assign):
                        targets = node.targets
                    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                        targets = [node.target]
                    for t in targets:
                        for sub in ast.walk(t):
                            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                                    and sub.value.id == "self":
                                self.fail(name + "." + method + " is a view that assigns to self." + sub.attr)


if __name__ == "__main__":
    unittest.main()
