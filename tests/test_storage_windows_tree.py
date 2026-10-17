"""Owned native trees: real effects, exact identities and failure retention."""
from contextlib import contextmanager
from dataclasses import replace
import ctypes as C
import gc
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import uuid
import weakref

import pytest

from backend.core.storage.native_windows_scope import WindowsScopeLibrary
from backend.core.storage.native_windows_tree import (
    StorageTreeError, TreePurpose, WindowsTreeLibrary, _sid_bytes,
)


@pytest.fixture
def native():
    if sys.platform != "win32":
        pytest.skip("Windows native storage trees")
    selected = os.environ.get("GITGO_STORAGE_NATIVE_TEST_LIBRARY")
    if not selected:
        pytest.skip("Build native tests with scripts/build_storage_native.py")
    path = Path(selected).resolve(strict=True)
    scopes = WindowsScopeLibrary(C.CDLL(str(path)))
    trees = WindowsTreeLibrary(C.CDLL(str(path.with_name("gitgo-storage-tree.dll"))))
    yield scopes, trees, path.parent
    retained = scopes.retained_scopes + trees.retained_trees
    for owner in list(trees._owners.values()):
        owner.close()
    for owner in list(scopes._owners.values()):
        owner.close()
    assert retained == 0


def private_directory(path, sid, extra=""):
    """Atomic ACL on a new test directory; never modifies an existing object."""
    assert not path.exists()
    kernel, advapi = C.WinDLL("kernel32", use_last_error=True), C.WinDLL("advapi32", use_last_error=True)
    class Attributes(C.Structure):
        _fields_ = [("size", C.c_ulong), ("descriptor", C.c_void_p), ("inherit", C.c_int)]
    parse = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    parse.argtypes = [C.c_wchar_p, C.c_ulong, C.POINTER(C.c_void_p), C.c_void_p]
    parse.restype = C.c_int
    create = kernel.CreateDirectoryW
    create.argtypes, create.restype = [C.c_wchar_p, C.POINTER(Attributes)], C.c_int
    free = kernel.LocalFree
    free.argtypes, free.restype = [C.c_void_p], C.c_void_p
    descriptor = C.c_void_p()
    sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{sid}){extra}"
    assert parse(sddl, 1, C.byref(descriptor), None), C.get_last_error()
    try:
        attributes = Attributes(C.sizeof(Attributes), descriptor, False)
        assert create(str(path), C.byref(attributes)), C.get_last_error()
    finally:
        assert not free(descriptor)


@contextmanager
def fixture(native, temporary, extra=""):
    scopes, trees, _ = native
    scope = scopes.open_scope()
    try:
        sid, _ = scope.account()
        root = temporary / "private"
        private_directory(root, sid, extra)
        parent = scope.root(str(root))
        yield scope, trees, sid, root, parent, scope.identity(parent)[0]
    finally:
        for owner in list(trees._owners.values()):
            owner.close()
        scope.close()


def install(trees, sid, parent, identity, data=b"code"):
    generation = str(uuid.uuid4())
    owner = trees.reserve(TreePurpose.INSTALL, sid)
    owner.begin(parent, identity, generation)
    code = owner.add(0, "code.py", length=len(data))
    if data:
        owner.write(code, data)
    owner.seal(code)
    assert owner.digest(code) == hashlib.sha256(data).hexdigest()
    root_id = owner.identity(0)
    owner.finish()
    owner.close()
    return generation, root_id


def test_metadata_install_pin_and_exact_retirement_share_owned_parents(native, tmp_path_factory):
    with fixture(native, tmp_path_factory) as (scope, trees, sid, root, parent, parent_id):
        bootstrap = trees.reserve(TreePurpose.BOOTSTRAP, sid)
        bootstrap.begin(parent, parent_id, "registry")
        intent = bootstrap.add(0, "bootstrap.intent", ceiling=4096)
        bootstrap.write(intent, b"closed metadata plan")
        gate = bootstrap.add(0, "maintenance.lock")
        bootstrap.write(gate, b"")
        bootstrap.lock_gate(gate)
        assert bootstrap.read(intent, 0, 20) == b"closed metadata plan"
        identity = bootstrap.identity(0)
        bootstrap.finish()
        with pytest.raises(StorageTreeError):
            bootstrap.add(0, "late")
        host = scope.open("registry", parent, directory=True)
        assert scope.identity(host)[0] == identity
        bootstrap.close()
        generation, installed_id = install(trees, sid, host, identity)
        path = root / "registry" / generation
        pins = trees.reserve(TreePurpose.PIN, sid)
        pins.root_path(str(path), installed_id)
        code = pins.add(0, "code.py", length=4)
        assert pins.digest(code) == hashlib.sha256(b"code").hexdigest()
        with pytest.raises(OSError):
            (path / "code.py").write_bytes(b"evil")
        with pytest.raises(OSError):
            path.rename(path.with_name("replacement"))
        pins.close()
        retirement = trees.reserve(TreePurpose.RETIRE, sid)
        retirement.begin(host, identity, generation, installed_id)
        code = retirement.add(0, "code.py", ceiling=4)
        assert retirement.info(code) == (False, 4)
        assert retirement.read(code, 0, 4) == b"code"
        retirement.prepare()
        retirement.delete(code)
        retirement.delete(0)
        retirement.finish()
        retirement.close()
        absence = trees.reserve(TreePurpose.ABSENCE, sid)
        assert absence.observe(host, identity, generation)
        with pytest.raises(StorageTreeError):
            absence.observe(host, identity, generation)
        absence.close()
        assert not path.exists()


@pytest.mark.parametrize("action", ["existing", "wrong-parent", "unflushed", "repeat-write", "bound"])
def test_bootstrap_failures_keep_partial_tree_and_refuse_more_effects(native, tmp_path_factory, action):
    with fixture(native, tmp_path_factory) as (_, trees, sid, root, parent, identity):
        owner = trees.reserve(TreePurpose.BOOTSTRAP, sid)
        if action == "existing":
            private_directory(root / "metadata", sid)
        if action in {"existing", "wrong-parent"}:
            expected = replace(identity, file_id=identity.file_id+1) if action == "wrong-parent" else identity
            with pytest.raises(StorageTreeError):
                owner.begin(parent, expected, "metadata")
        else:
            owner.begin(parent, identity, "metadata")
            file = owner.add(0, "intent", ceiling=4)
            if action == "unflushed":
                with pytest.raises(StorageTreeError):
                    owner.finish()
            elif action == "repeat-write":
                owner.write(file, b"abc")
                with pytest.raises(StorageTreeError):
                    owner.write(file, b"def")
                assert (root / "metadata" / "intent").read_bytes() == b"abc"
            else:
                with pytest.raises(StorageTreeError):
                    owner.write(file, b"12345")
                assert (root / "metadata" / "intent").stat().st_size == 0
        with pytest.raises(StorageTreeError):
            owner.begin(parent, identity, "second")
        owner.close()
        assert not (root / "second").exists()
        assert (root / "metadata").exists() == (action != "wrong-parent")


@pytest.mark.parametrize("extra,allowed", [("(A;OICI;GRGX;;;WD)", True), ("(A;OICI;DC;;;WD)", False)])
def test_parent_delete_child_right_is_refused_before_creation(native, tmp_path_factory, extra, allowed):
    with fixture(native, tmp_path_factory, extra) as (_, trees, sid, root, parent, identity):
        owner = trees.reserve(TreePurpose.BOOTSTRAP, sid)
        if allowed:
            owner.begin(parent, identity, "metadata")
            owner.finish()
        else:
            with pytest.raises(StorageTreeError) as caught:
                owner.begin(parent, identity, "metadata")
            assert caught.value.native_error == 5
        owner.close()
        assert (root / "metadata").exists() == allowed


@pytest.mark.parametrize("action", ["unrecorded", "wrong-root", "omitted-member", "child-before-parent"])
def test_retirement_never_adopts_or_walks_an_unproved_tree(native, tmp_path_factory, action):
    with fixture(native, tmp_path_factory) as (_, trees, sid, root, parent, identity):
        generation, root_id = install(trees, sid, parent, identity)
        owner = trees.reserve(TreePurpose.RETIRE, sid)
        if action in {"unrecorded", "wrong-root"}:
            expected = None if action == "unrecorded" else replace(root_id, file_id=root_id.file_id+1)
            with pytest.raises(StorageTreeError):
                owner.begin(parent, identity, generation, expected)
        else:
            owner.begin(parent, identity, generation, root_id)
            if action == "child-before-parent":
                owner.add(0, "code.py", ceiling=4)
            owner.prepare()
            with pytest.raises(StorageTreeError):
                owner.delete(0)
        owner.close()
        assert (root / generation / "code.py").read_bytes() == b"code"


def test_unrecorded_root_can_only_be_observed_absent(native, tmp_path_factory):
    with fixture(native, tmp_path_factory) as (_, trees, sid, _, parent, identity):
        owner = trees.reserve(TreePurpose.RETIRE, sid)
        owner.begin(parent, identity, str(uuid.uuid4()), None)
        child = owner.add(0, "code.py", ceiling=4)
        assert owner.info(child) == (True, 0)
        owner.prepare()
        owner.delete(child)
        owner.delete(0)
        owner.finish()
        owner.close()


def test_native_capacity_python_gc_and_lost_close_result_retain_exact_owner(native, tmp_path_factory):
    with fixture(native, tmp_path_factory) as (_, trees, sid, _, parent, identity):
        owners = [trees.reserve(TreePurpose.PIN, sid) for _ in range(4)]
        with pytest.raises(StorageTreeError):
            trees.reserve(TreePurpose.INSTALL, sid)
        reference = weakref.ref(owners[0])
        owners[0].root(parent, identity)
        del owners[0]
        gc.collect()
        assert reference() is not None and trees.retained_trees == 4
        first = reference()
        assert trees._calls["tree_retire"](first._cookie) == 0
        first.close()
        second = trees.reserve(TreePurpose.PIN, sid)
        second.root(parent, identity)
        assert trees._calls["tree_retire"](first._cookie) == 0
        assert second.identity(0) == identity
        for owner in [*owners, second]:
            owner.close()


@pytest.mark.parametrize("purpose", list(TreePurpose))
def test_native_purpose_is_immutable_before_any_effect(native, tmp_path_factory, purpose):
    with fixture(native, tmp_path_factory) as (_, trees, sid, _, _, _):
        entries = {
            "tree_root": (TreePurpose.PIN, [None, None, 3]),
            "tree_path": (TreePurpose.PIN, [None, None, 3]),
            "install_begin": (TreePurpose.INSTALL, [None, None, 3, None]),
            "install_absent": (TreePurpose.ABSENCE, [None, None, 3, None, None]),
            "bootstrap_begin": (TreePurpose.BOOTSTRAP, [None, None, 3, None]),
            "retirement_begin": (TreePurpose.RETIRE, [None, None, None, None, 0]),
        }
        for name, (expected, args) in entries.items():
            if purpose is expected:
                continue
            owner = trees.reserve(purpose, sid)
            assert trees._calls[name](owner._cookie, *args) == 5023
            status = (C.c_uint64 * 8)()
            assert trees._calls["tree_status"](owner._cookie, status, 8) == 0
            assert status[0] == status[1] == 0 and status[2] == 1
            owner.close()


@pytest.mark.parametrize("sid", ["S-1-5-01", "S-1-5-4294967296", "S-1-5-1\0", "S-1-4-21", True])
def test_host_sid_never_wraps_or_accepts_noncanonical_values(sid):
    with pytest.raises(ValueError):
        _sid_bytes(sid)


@pytest.mark.parametrize("kind,mode", [("pins", None), ("install", None)] +
                         [("bootstrap", mode) for mode in range(1, 11)] +
                         [("retirement", mode) for mode in range(1, 6)])
def test_native_after_effect_failures_keep_ownership(native, tmp_path_factory, kind, mode):
    with fixture(native, tmp_path_factory) as (_, _, sid, root, _, _):
        executable = native[2] / f"storage-tree-{kind}-faults.exe"
        assert executable.is_file(), "Dedicated native fault fixture is required"
        args = [str(executable), str(root)]
        if kind == "retirement":
            generation = str(uuid.uuid4())
            private_directory(root / generation, sid)
            (root / generation / "code.py").write_bytes(b"code")
            args.append(generation)
        if mode is not None:
            args.append(str(mode))
        result = subprocess.run(args, capture_output=True, text=True, timeout=25)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "passed" in result.stdout and "quarantined=" in result.stdout
