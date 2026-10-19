"""Python acknowledgement loss retains native CAS ownership and consumes work."""
import ctypes
import hashlib
from types import SimpleNamespace
import uuid

import pytest

from backend.core.sandbox_read import ObjectIdentity
from backend.core.storage import native_windows_cas as n
from backend.core.storage.native_windows_scope import NativeHandle


class Effects:
    def __init__(self, kind, fail=None):
        self.fail, self.calls = fail, []
        self.data = b"fixed planned bytes"
        self.record = n.CasFileRecord(n.CasFileIntent(hashlib.sha256(self.data).hexdigest(),
            str(uuid.uuid4()), len(self.data)), ObjectIdentity("win32", 17, 23))
        cls = n.WindowsCasWriterLibrary if kind == "writer" else n.WindowsCasGenerationLibrary
        self.library = object.__new__(cls)
        self.library._owners = {}
        self.library._calls = {name: self.callback(name) for name in
            ("reserve", "create", "created", "write", "seal", "identity", "read", "retire", "open", "delete", "absent")}
        self.kind = kind
        self.parent = NativeHandle(SimpleNamespace(borrow=lambda token:123), 1)
        self.parent_id = ObjectIdentity("win32", 17, 19)

    def callback(self, name):
        def call(*args):
            # Every callback, including reserve, is already owned by Python.
            assert self.library.retained_owners == 1
            self.calls.append(name)
            if name == "reserve": ctypes.cast(args[0], ctypes.POINTER(ctypes.c_uint64))[0] = 257
            elif name in {"created", "identity"}:
                out = args[1]
                values = [17,23,0,0 if name == "created" else len(self.data)]
                if self.kind == "generation": values.append(0)
                for i,value in enumerate(values): out[i] = value
            elif name == "read":
                ctypes.memmove(args[2], self.data[args[1]:args[1]+args[3]], args[3])
            elif name == "absent": ctypes.cast(args[1], ctypes.POINTER(ctypes.c_int))[0] = 1
            if name == self.fail: raise RuntimeError("injected lost "+name+" result")
            return 0
        return call

    def reserve(self):
        if self.kind == "writer": return self.library.reserve()
        return self.library.reserve(n.CasPurpose.RETIRE_COMMITTED)

    def open(self, owner):
        if self.kind == "writer": return owner.create(self.parent,self.parent_id,self.record.intent)
        return owner.open(self.parent,self.parent_id,self.record)


@pytest.fixture(autouse=True)
def clear_only_fake_owners():
    assert not n._WRITERS and not n._GENERATIONS
    yield
    # These tests own no OS resource. Production has no forgetting operation.
    for registry in (n._WRITERS,n._GENERATIONS):
        for owner in registry.values(): owner._library._owners.clear()
        registry.clear()


@pytest.mark.parametrize("kind", ["writer","generation"])
def test_unknown_reservation_retains_native_cookie(kind):
    effects = Effects(kind,"reserve")
    with pytest.raises(RuntimeError): effects.reserve()
    assert effects.library.retained_owners == 1
    owner = next(iter(effects.library._owners.values()))
    assert owner._cookie.value == 257 and owner._sealed
    owner.close(); assert not effects.library.retained_owners


@pytest.mark.parametrize("kind,operation", [("writer","create"),("writer","created"),
    ("generation","open"),("generation","identity")])
def test_unknown_open_cannot_materialize_or_adopt(kind,operation):
    effects = Effects(kind,operation); owner = effects.reserve()
    with pytest.raises(RuntimeError): effects.open(owner)
    before = list(effects.calls)
    for _ in range(2):
        with pytest.raises(n.StorageCasError): effects.open(owner)
    assert effects.calls == before and effects.library.retained_owners == 1
    owner.close(); owner.close(); assert effects.calls.count("retire") == 1


@pytest.mark.parametrize("operation", ["write","seal","identity","read"])
def test_unknown_materialization_cannot_publish_or_repeat(operation):
    effects = Effects("writer",operation); owner = effects.reserve(); effects.open(owner)
    with pytest.raises(RuntimeError): owner.materialize(effects.data)
    before = list(effects.calls)
    with pytest.raises(n.StorageCasError): owner.materialize(effects.data)
    assert effects.calls == before and not owner._verified
    owner.close()


@pytest.mark.parametrize("operation", ["read","delete","absent"])
def test_unknown_retirement_never_repeats_disposition(operation):
    effects = Effects("generation",operation); owner = effects.reserve(); effects.open(owner)
    if operation == "read":
        with pytest.raises(RuntimeError): owner.read()
    else:
        assert owner.read() == effects.data
        if operation == "delete":
            with pytest.raises(RuntimeError): owner.delete()
        else:
            owner.delete()
            with pytest.raises(RuntimeError): owner.observe_absence()
    before = list(effects.calls)
    for action in (owner.read,owner.delete,owner.observe_absence):
        with pytest.raises(n.StorageCasError): action()
    assert effects.calls == before
    owner.close()


@pytest.mark.parametrize("kind", ["writer","generation"])
def test_retirement_unknown_keeps_enclosing_owner(kind):
    effects = Effects(kind,"retire"); owner = effects.reserve()
    with pytest.raises(RuntimeError): owner.close()
    assert effects.library.retained_owners == 1 and owner._sealed and not owner._closed
    with pytest.raises(n.StorageCasError): effects.open(owner)


@pytest.mark.parametrize("change", [{"digest":"0"*63}, {"generation":"../foreign"},
    {"generation":uuid.uuid4()}, {"length":True}, {"length":65537}])
def test_immutable_intent_rejects_ambiguous_or_unbounded_values(change):
    values = dict(digest="0"*64,generation=str(uuid.uuid4()),length=0); values.update(change)
    with pytest.raises(ValueError): n.CasFileIntent(**values)
