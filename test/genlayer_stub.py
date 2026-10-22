"""
A small, dependency-free stand-in for the `genlayer` GenVM SDK, written so
the three contract files in `contracts/` can be imported and exercised as
plain Python with nothing but the standard library installed.

It is NOT a reimplementation of GenVM's consensus, gas or storage engine.
What it does model, because the contracts depend on it and because getting
it wrong silently is how real bugs hide:

  * storage typing -- every assignment into a contract field, a storage
    dataclass field, a TreeMap value or a DynArray element is checked
    against its declared type (sized ints are range-checked, so an
    overflowing u128 or a negative u32 fails the test instead of passing);
    TreeMap keys are type-checked too (a plain str used on an
    Address-keyed map is an error, as it is on the real SDK);
  * zero-initialised storage, including nested @allow_storage dataclasses
    created by TreeMap.get_or_insert_default();
  * view methods are read-only: any storage write, transfer or non-view
    call made while a view runs raises;
  * a reverted write really reverts: if a public write method raises, every
    contract's storage and the balance ledger are restored;
  * message context: nested contract-to-contract calls see the calling
    contract as `gl.message.sender_address` and their own address as
    `gl.message.contract_address`, and attached value is credited only to
    payable methods and consumed by the call;
  * a balance ledger: payable calls credit the contract, `emit_transfer`
    debits it immediately (and fails on an overdraft), and `self.balance`
    reads it;
  * non-deterministic blocks: `gl.nondet.*` is usable only inside
    `gl.vm.run_nondet`, storage and cross-contract calls are forbidden
    inside one, results must be calldata-encodable, and the validator runs
    sandboxed with the same agree / disagree rules as the real SDK, against
    a separately scriptable "validator's view of the world".

Run the whole suite with:  python3 -m unittest discover -s test -v
"""
import copy
import dataclasses
import functools
import importlib.util
import json
import sys
import types

ZERO_ADDRESS = "0x" + "00" * 20
BASE_EPOCH = 1_900_000_000


# --------------------------------------------------------------------------- #
# addresses and sized integers
# --------------------------------------------------------------------------- #

class Address:
    def __init__(self, value=ZERO_ADDRESS):
        if isinstance(value, Address):
            raw = value._hex
        elif isinstance(value, (bytes, bytearray)):
            if len(value) != 20:
                raise Exception("invalid address " + repr(value))
            raw = "0x" + bytes(value).hex()
        else:
            text = str(value)
            if len(text) != 42 or not text.startswith("0x"):
                raise Exception("invalid address " + text)
            try:
                int(text[2:], 16)
            except ValueError:
                raise Exception("invalid address " + text)
            raw = text.lower()
        self._hex = raw

    @property
    def as_hex(self):
        return self._hex

    def __str__(self):
        return self._hex

    def __repr__(self):
        return 'Address("' + self._hex + '")'

    def __eq__(self, other):
        return isinstance(other, Address) and other._hex == self._hex

    def __lt__(self, other):
        assert isinstance(other, Address)
        return self._hex < other._hex

    def __hash__(self):
        return hash(self._hex)


class _IntType:
    def __init__(self, name, bits, signed):
        self.name = name
        self.lo = -(1 << (bits - 1)) if signed else 0
        self.hi = (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1

    def __call__(self, value):
        _check_value(self, value, self.name + "()")
        return int(value)

    def __repr__(self):
        return self.name


_INT_TYPES = {}
for _bits in range(8, 257, 8):
    _INT_TYPES["u" + str(_bits)] = _IntType("u" + str(_bits), _bits, False)
    _INT_TYPES["i" + str(_bits)] = _IntType("i" + str(_bits), _bits, True)
_INT_TYPES["bigint"] = _IntType("bigint", 4096, True)


def _check_value(tp, value, label):
    if isinstance(tp, _IntType):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(label + ": expected " + tp.name + ", got " + type(value).__name__)
        if value < tp.lo or value > tp.hi:
            raise OverflowError(label + ": " + str(value) + " does not fit " + tp.name)
    elif tp is bool:
        if not isinstance(value, bool):
            raise TypeError(label + ": expected bool, got " + type(value).__name__)
    elif tp is str:
        if not isinstance(value, str):
            raise TypeError(label + ": expected str, got " + type(value).__name__)
    elif tp is Address:
        if not isinstance(value, Address):
            raise TypeError(label + ": expected Address, got " + type(value).__name__)


# --------------------------------------------------------------------------- #
# execution context shared by the whole fake chain
# --------------------------------------------------------------------------- #

class _Ctx:
    def __init__(self):
        self.reset()

    def reset(self):
        self.stack = []        # addresses of the contracts currently executing
        self.readonly = 0
        self.nondet = 0
        self.phase = "leader"  # "leader" or "validator" while inside run_nondet
        self.now = BASE_EPOCH


CTX = _Ctx()
REGISTRY = {}        # lower-case address -> contract instance
LEDGER = {}          # lower-case address -> balance in wei
PAYMENTS = []        # (from_address, to_address, amount), in order
CONTRACT_CALLS = []  # (caller_address, target_address, method), for assertions


def _guard_write(what):
    if CTX.nondet > 0:
        raise RuntimeError("forbidden inside a non-deterministic block: " + what)
    if CTX.readonly > 0:
        raise RuntimeError("a view method tried to change state: " + what)


def _guard_read(what):
    if CTX.nondet > 0:
        raise RuntimeError("storage is not accessible inside a non-deterministic block: " + what)


# --------------------------------------------------------------------------- #
# storage collections and dataclasses
# --------------------------------------------------------------------------- #

def _zero_value(tp):
    if isinstance(tp, _IntType):
        return 0
    if tp is bool:
        return False
    if tp is str:
        return ""
    if tp is Address:
        return Address(ZERO_ADDRESS)
    if isinstance(tp, type) and issubclass(tp, (_TreeMapBase, _DynArrayBase)):
        return tp()
    if dataclasses.is_dataclass(tp):
        return tp(**{f.name: _zero_value(f.type) for f in dataclasses.fields(tp)})
    return None


class _DynArrayBase(list):
    _item_type = None

    def append(self, value):
        _guard_write("DynArray.append")
        _check_value(self._item_type, value, "DynArray element")
        list.append(self, value)

    def __setitem__(self, index, value):
        _guard_write("DynArray item assignment")
        _check_value(self._item_type, value, "DynArray element")
        list.__setitem__(self, index, value)

    def pop(self, *args):
        _guard_write("DynArray.pop")
        list.pop(self, *args)

    def clear(self):
        _guard_write("DynArray.clear")
        list.clear(self)


class DynArray(_DynArrayBase):
    def __class_getitem__(cls, item):
        return type("DynArray", (_DynArrayBase,), {"_item_type": item})


class _TreeMapBase(dict):
    _key_type = None
    _value_type = None

    def _key(self, key):
        _check_value(self._key_type, key, "TreeMap key")
        return key

    def get(self, key, default=None):
        _guard_read("TreeMap.get")
        return dict.get(self, self._key(key), default)

    def __getitem__(self, key):
        _guard_read("TreeMap[]")
        return dict.__getitem__(self, self._key(key))

    def __contains__(self, key):
        _guard_read("TreeMap in")
        return dict.__contains__(self, self._key(key))

    def __setitem__(self, key, value):
        _guard_write("TreeMap[] assignment")
        _check_value(self._value_type, value, "TreeMap value")
        dict.__setitem__(self, self._key(key), value)

    def get_or_insert_default(self, key):
        _guard_write("TreeMap.get_or_insert_default")
        key = self._key(key)
        if not dict.__contains__(self, key):
            dict.__setitem__(self, key, _zero_value(self._value_type))
        return dict.__getitem__(self, key)


class TreeMap(_TreeMapBase):
    def __class_getitem__(cls, item):
        key_type, value_type = item
        return type("TreeMap", (_TreeMapBase,), {"_key_type": key_type, "_value_type": value_type})


def allow_storage(cls):
    """Marks a dataclass as storable and makes its attribute assignments
    type-checked and write-guarded, like a real storage struct."""
    if dataclasses.is_dataclass(cls):
        hints = {f.name: f.type for f in dataclasses.fields(cls)}

        def __setattr__(self, name, value):
            _guard_write("storage struct field '" + name + "'")
            if name in hints:
                _check_value(hints[name], value, cls.__name__ + "." + name)
            object.__setattr__(self, name, value)

        cls.__setattr__ = __setattr__
    return cls


# --------------------------------------------------------------------------- #
# errors and calldata
# --------------------------------------------------------------------------- #

class UserError(Exception):
    def __init__(self, message=""):
        super().__init__(message)
        self.message = message

    def __repr__(self):
        return "UserError(message=" + repr(self.message) + ")"

    __str__ = __repr__


class VMError:
    def __init__(self, message=""):
        self.message = message


class Return:
    def __init__(self, calldata):
        self.calldata = calldata


class ConsensusDisagreement(AssertionError):
    """Raised when the single emulated validator would vote 'disagree'."""


def _calldata(value, path="value"):
    """Round-trips a value the way calldata would: only None, bool, int,
    str, bytes, Address, sequences and str-keyed mappings survive."""
    if value is None or isinstance(value, (bool, int, str, bytes, Address)):
        return value
    if isinstance(value, (list, tuple)):
        return [_calldata(v, path + "[]") for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError("calldata: " + path + " has a non-str key " + repr(k))
            out[k] = _calldata(v, path + "." + k)
        return out
    raise TypeError("calldata cannot encode " + path + " of type " + type(value).__name__)


# --------------------------------------------------------------------------- #
# non-deterministic primitives, scriptable per role
# --------------------------------------------------------------------------- #

class Scripted:
    """What a fetch or an LLM call returns. `pages` maps a URL substring to
    page text (or to an Exception instance to simulate a failed fetch);
    `llm` is a dict / str / Exception, or a callable(prompt) returning one."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.pages = None
        self.llm = None
        self.fetched = []
        self.prompts = []

    def web(self, url):
        self.fetched.append(url)
        for needle, body in (self.pages or {}).items():
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


LEADER = Scripted()
VALIDATOR = Scripted()


def _script():
    """The validator replays against its own script when one is configured
    and otherwise sees exactly what the leader saw."""
    if CTX.phase == "validator" and (VALIDATOR.pages is not None or VALIDATOR.llm is not None):
        return VALIDATOR
    return LEADER


def set_world(pages, llm, validator_pages=None, validator_llm=None):
    LEADER.pages = dict(pages)
    LEADER.llm = llm
    if validator_pages is not None:
        VALIDATOR.pages = dict(validator_pages)
    if validator_llm is not None:
        VALIDATOR.llm = validator_llm
    if validator_pages is not None and validator_llm is None:
        VALIDATOR.llm = llm
    if validator_llm is not None and validator_pages is None:
        VALIDATOR.pages = dict(pages)


class _Web:
    def render(self, url, *, mode="text", wait_after_loaded=None):
        if CTX.nondet == 0:
            raise RuntimeError("gl.nondet.web.render can only run inside a non-deterministic block")
        return _script().web(url)


class _Nondet:
    def __init__(self):
        self.web = _Web()

    def exec_prompt(self, prompt, *, response_format="text", **kwargs):
        if CTX.nondet == 0:
            raise RuntimeError("gl.nondet.exec_prompt can only run inside a non-deterministic block")
        return _script().answer(prompt)


nondet = _Nondet()


def run_nondet(leader_fn, validator_fn, /, *,
        compare_user_errors=lambda a, b: a.message == b.message,
        compare_vm_errors=lambda a, b: a.message == b.message):
    """Single-process emulation of leader + one validator, following the
    real SDK's rules: the validator runs sandboxed, the answer must have the
    same kind as the leader's result, a returned verdict must be a bool, and
    two failures are compared with `compare_user_errors`."""
    CTX.nondet += 1
    CTX.phase = "leader"
    try:
        try:
            leaders = Return(_calldata(leader_fn(), "leader result"))
        except UserError as e:
            leaders = e
    finally:
        CTX.nondet -= 1

    CTX.nondet += 1
    CTX.phase = "validator"
    try:
        try:
            answer = Return(validator_fn(leaders))
        except UserError as e:
            answer = e
        except Exception as e:
            answer = VMError(str(e))
    finally:
        CTX.nondet -= 1
        CTX.phase = "leader"

    if type(answer) is not type(leaders):
        agreed = False
    elif isinstance(answer, Return):
        if not isinstance(answer.calldata, bool):
            raise TypeError("validator function returned non-bool " + repr(answer.calldata))
        agreed = answer.calldata
    elif isinstance(answer, UserError):
        agreed = bool(compare_user_errors(leaders, answer))
    else:
        agreed = bool(compare_vm_errors(leaders, answer))

    if not agreed:
        raise ConsensusDisagreement("the validator disagreed with the leader")
    if isinstance(leaders, UserError):
        raise leaders
    return leaders.calldata


vm = types.SimpleNamespace(UserError=UserError, VMError=VMError, Return=Return,
    Result=object, run_nondet=run_nondet)


# --------------------------------------------------------------------------- #
# message context, balances and calls between contracts
# --------------------------------------------------------------------------- #

message = types.SimpleNamespace(
    sender_address=Address(ZERO_ADDRESS),
    origin_address=Address(ZERO_ADDRESS),
    contract_address=Address(ZERO_ADDRESS),
    value=0,
    chain_id=1,
)


def _ledger_key(address):
    return str(address).lower()


def balance_of(address):
    return LEDGER.get(_ledger_key(address), 0)


def fund(address, amount):
    LEDGER[_ledger_key(address)] = int(amount)


def paid_to(address):
    key = _ledger_key(address)
    return sum(amount for (_, to, amount) in PAYMENTS if to == key)


def send_as(sender, value=0):
    """Sets who the NEXT top-level call comes from and how much GEN rides
    with it. The value is consumed by that one call."""
    message.sender_address = Address(sender)
    message.origin_address = Address(sender)
    message.value = int(value)


def advance(seconds):
    CTX.now += int(seconds)


def _snapshot():
    state = {key: copy.deepcopy({k: v for k, v in inst.__dict__.items() if not k.startswith("_")})
        for key, inst in REGISTRY.items()}
    return state, dict(LEDGER), len(PAYMENTS)


def _restore(snap):
    state, ledger, payments = snap
    for key, fields in state.items():
        inst = REGISTRY[key]
        for k in [k for k in inst.__dict__ if not k.startswith("_")]:
            del inst.__dict__[k]
        inst.__dict__.update(fields)
    LEDGER.clear()
    LEDGER.update(ledger)
    del PAYMENTS[payments:]


def _invoke(instance, fn, readonly, payable, args, kwargs):
    nested = len(CTX.stack) > 0
    sender = message.contract_address if nested else message.sender_address
    value = 0 if nested else int(message.value)
    if value and not payable:
        message.value = 0
        raise UserError("this method is not payable but received value")

    saved = (message.sender_address, message.contract_address, message.value)
    snap = None if readonly or CTX.readonly > 0 else _snapshot()
    args = _calldata(list(args), "arguments")
    kwargs = _calldata(dict(kwargs), "arguments")

    CTX.stack.append(instance._address)
    if readonly:
        CTX.readonly += 1
    message.sender_address = sender
    message.contract_address = instance._address
    message.value = value
    if payable and value:
        LEDGER[_ledger_key(instance._address)] = balance_of(instance._address) + value
    try:
        result = fn(instance, *args, **kwargs)
    except BaseException:
        if snap is not None:
            _restore(snap)
        raise
    finally:
        if readonly:
            CTX.readonly -= 1
        CTX.stack.pop()
        message.sender_address, message.contract_address, message.value = saved
        if not nested:
            message.value = 0
    return _calldata(result, "return value")


def _make_public(fn):
    readonly = bool(getattr(fn, "__gl_readonly__", False))
    payable = bool(getattr(fn, "__gl_payable__", False))

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        return _invoke(self, fn, readonly, payable, args, kwargs)

    wrapper.__gl_public__ = True
    wrapper.__gl_readonly__ = readonly
    wrapper.__gl_payable__ = payable
    return wrapper


def _view(fn):
    fn.__gl_public__ = True
    fn.__gl_readonly__ = True
    return fn


def _write(fn):
    fn.__gl_public__ = True
    fn.__gl_readonly__ = False
    return fn


def _payable(fn):
    fn.__gl_public__ = True
    fn.__gl_readonly__ = False
    fn.__gl_payable__ = True
    return fn


_write.payable = _payable
public = types.SimpleNamespace(view=_view, write=_write)


class _ContractAt:
    def __init__(self, address):
        if not isinstance(address, Address):
            raise TypeError("address expected")
        self._address = address

    @property
    def address(self):
        return self._address

    @property
    def balance(self):
        return balance_of(self._address)

    def view(self):
        return _ViewNamespace(self._address)

    def emit_transfer(self, *, value, on="finalized"):
        _guard_write("emit_transfer")
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("transfer value must be an int")
        if value <= 0:
            raise ValueError("value must be greater than 0 for emit_transfer")
        if not CTX.stack:
            raise RuntimeError("emit_transfer outside of a contract call")
        source = CTX.stack[-1]
        have = balance_of(source)
        if have < value:
            raise RuntimeError("contract " + str(source) + " cannot send " + str(value)
                + " wei, its balance is " + str(have))
        LEDGER[_ledger_key(source)] = have - value
        LEDGER[_ledger_key(self._address)] = balance_of(self._address) + value
        PAYMENTS.append((_ledger_key(source), _ledger_key(self._address), value))


class _ViewNamespace:
    def __init__(self, address):
        self._address = address

    def __getattr__(self, name):
        if CTX.nondet > 0:
            raise RuntimeError("cross-contract calls are forbidden inside a non-deterministic block")
        target = REGISTRY.get(_ledger_key(self._address))
        if target is None:
            raise RuntimeError("no contract deployed at " + str(self._address))
        method = getattr(type(target), name, None)
        if method is None or not getattr(method, "__gl_public__", False):
            raise RuntimeError("contract at " + str(self._address) + " has no public method " + name)
        caller = CTX.stack[-1] if CTX.stack else None
        bound = getattr(target, name)

        def call(*args, **kwargs):
            CONTRACT_CALLS.append((str(caller) if caller else "", str(self._address), name))
            # a view() call can only ever run read-only, whatever the method is
            CTX.readonly += 1
            try:
                return bound(*args, **kwargs)
            finally:
                CTX.readonly -= 1

        return call


def get_contract_at(address):
    return _ContractAt(address)


def contract_interface(declaration):
    return get_contract_at


# --------------------------------------------------------------------------- #
# the Contract base class
# --------------------------------------------------------------------------- #

def _annotations_of(cls):
    merged = {}
    for klass in reversed(cls.__mro__):
        merged.update(klass.__dict__.get("__annotations__", {}))
    return merged


class Contract:
    def __new__(cls, *args, **kwargs):
        instance = object.__new__(cls)
        object.__setattr__(instance, "_address", Address(ZERO_ADDRESS))
        for name, tp in _annotations_of(cls).items():
            object.__setattr__(instance, name, _zero_value(tp))
        return instance

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for name, attr in list(cls.__dict__.items()):
            if callable(attr) and getattr(attr, "__gl_public__", False):
                setattr(cls, name, _make_public(attr))

    def __setattr__(self, name, value):
        if not name.startswith("_"):
            _guard_write("contract field '" + name + "'")
            hints = _annotations_of(type(self))
            if name in hints:
                _check_value(hints[name], value, type(self).__name__ + "." + name)
        object.__setattr__(self, name, value)

    def __getattribute__(self, name):
        if not name.startswith("_") and CTX.nondet > 0 and name in _annotations_of(type(self)):
            raise RuntimeError("storage field '" + name + "' read inside a non-deterministic block")
        return object.__getattribute__(self, name)

    @property
    def balance(self):
        return balance_of(self._address)


def deploy(cls, address, *args, sender, **kwargs):
    """Constructs a contract instance at `address` the way a deployment
    would: zero-initialised storage, the constructor running as `sender`,
    and a failed constructor leaving nothing behind."""
    instance = cls.__new__(cls)
    object.__setattr__(instance, "_address", Address(address))
    key = _ledger_key(instance._address)
    if key in REGISTRY:
        raise RuntimeError("address already in use: " + key)
    REGISTRY[key] = instance
    saved = (message.sender_address, message.contract_address, message.value)
    message.sender_address = Address(sender)
    message.contract_address = instance._address
    message.value = 0
    CTX.stack.append(instance._address)
    try:
        cls.__init__(instance, *_calldata(list(args)), **_calldata(dict(kwargs)))
    except BaseException:
        del REGISTRY[key]
        raise
    finally:
        CTX.stack.pop()
        message.sender_address, message.contract_address, message.value = saved
    return instance


gl = types.SimpleNamespace(
    message=message,
    public=public,
    vm=vm,
    nondet=nondet,
    contract_interface=contract_interface,
    get_contract_at=get_contract_at,
    Contract=Contract,
)

_module = types.ModuleType("genlayer")
_module.gl = gl
_module.Address = Address
_module.DynArray = DynArray
_module.TreeMap = TreeMap
_module.allow_storage = allow_storage
for _name, _tp in _INT_TYPES.items():
    setattr(_module, _name, _tp)


def install():
    sys.modules["genlayer"] = _module


def load_contract(path, module_name):
    """Imports a contract file from disk as a real Python module, against
    this stub. Its `_now_epoch()` is pointed at the shared fake clock, so
    `advance()` moves every loaded contract forward together."""
    install()
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._now_epoch = lambda: CTX.now
    return module


def reset():
    """Clears every shared, module-level piece of fake-chain state."""
    CTX.reset()
    REGISTRY.clear()
    LEDGER.clear()
    PAYMENTS.clear()
    CONTRACT_CALLS.clear()
    LEADER.reset()
    VALIDATOR.reset()
    message.sender_address = Address(ZERO_ADDRESS)
    message.origin_address = Address(ZERO_ADDRESS)
    message.contract_address = Address(ZERO_ADDRESS)
    message.value = 0
