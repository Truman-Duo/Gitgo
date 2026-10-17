"""Real native tests; the dedicated Windows CI job builds this ABI first."""
from dataclasses import replace
import ctypes
import gc
import os
from pathlib import Path
import subprocess
import sys
import threading
import weakref

import pytest

from backend.core.sandbox_read import ObjectIdentity
from backend.core.storage.native_windows_scope import (
    StorageOwnershipError, WindowsScopeLibrary, _identity_words,
)


@pytest.fixture
def native():
    if sys.platform != "win32":
        pytest.skip("Windows native ownership backend")
    selected = os.environ.get("GITGO_STORAGE_NATIVE_TEST_LIBRARY")
    if not selected:
        pytest.skip("Build the native test library with scripts/build_storage_native.py")
    # Test-only explicit path. Production module loading never reads this env.
    library = WindowsScopeLibrary(ctypes.CDLL(str(Path(selected).resolve(strict=True))))
    yield library
    assert library.retained_scopes == 0


def test_native_owner_pins_ancestors_and_reads_exact_objects(native, tmp_path_factory):
    (tmp_path_factory / "file").write_bytes(b"data")
    scope = native.open_scope()
    try:
        root = scope.root(str(tmp_path_factory))
        file = scope.open("file", root)
        identity, directory = scope.identity(file)
        assert identity.platform == "win32" and not directory
        assert scope.read(file) == b"data"
        with pytest.raises(OSError):
            (tmp_path_factory / "file").rename(tmp_path_factory / "moved")
        scope.close_handle(file)
        scope.close_handle(file)
        (tmp_path_factory / "file").rename(tmp_path_factory / "moved")
    finally:
        scope.close()


def test_new_file_handle_cannot_be_closed_by_reused_slot_token(native, tmp_path_factory):
    (tmp_path_factory / "file").write_bytes(b"data")
    scope = native.open_scope()
    try:
        root = scope.root(str(tmp_path_factory))
        old = scope.open("file", root)
        scope.close_handle(old)
        current = scope.open("file", root)
        assert old.token != current.token
        with pytest.raises(StorageOwnershipError):
            scope.close_handle(old)
        with pytest.raises(OSError):
            (tmp_path_factory / "file").unlink()
    finally:
        scope.close()


def test_gate_contention_and_wrong_identity_refuse_admission(native, tmp_path_factory):
    (tmp_path_factory / "gate").touch()
    scopes = [native.open_scope() for _ in range(3)]
    try:
        first, second, third = scopes
        root = first.root(str(tmp_path_factory))
        gate = first.open("gate", root)
        identity, _ = first.identity(gate)
        first.lock(gate, identity)
        imported = second.import_parent(root, first.identity(root)[0])
        with pytest.raises(StorageOwnershipError) as caught:
            second.lock(second.open("gate", imported), identity)
        assert caught.value.native_error == 33
        another = third.root(str(tmp_path_factory))
        with pytest.raises(StorageOwnershipError):
            third.lock(third.open("gate", another), replace(identity, file_id=identity.file_id + 1))
    finally:
        for scope in scopes:
            scope.close()


def test_importing_own_parent_is_still_an_owned_duplicate(native, tmp_path_factory):
    scope = native.open_scope()
    try:
        root = scope.root(str(tmp_path_factory))
        duplicate = scope.import_parent(root, scope.identity(root)[0])
        assert duplicate.token != root.token
        assert scope.identity(duplicate) == scope.identity(root)
    finally:
        scope.close()


def test_failed_return_and_python_gc_cannot_drop_native_ownership(native, tmp_path_factory):
    (tmp_path_factory / "file").write_bytes(b"data")
    scope = native.open_scope()
    reference = weakref.ref(scope)
    scope.open("file", scope.root(str(tmp_path_factory)))
    del scope
    gc.collect()
    assert reference() is not None and native.retained_scopes == 1
    try:
        with pytest.raises(OSError):
            (tmp_path_factory / "file").unlink()
    finally:
        reference().close()


def test_scope_owner_is_bound_to_its_native_thread(native, tmp_path_factory):
    scope = native.open_scope()
    root = scope.root(str(tmp_path_factory))
    errors = []
    def other_thread():
        try:
            scope.close()
        except StorageOwnershipError as error:
            errors.append(error.native_error)
    try:
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive() and errors == [6]
        assert scope.snapshot().handles > 0
    finally:
        scope.close()


def test_lost_retirement_result_cannot_retire_a_reused_scope(native, tmp_path_factory):
    first = native.open_scope()
    first.root(str(tmp_path_factory))
    # Native success occurred, but Python did not receive the completion.
    assert native._calls["retire"](first._cookie) == 0
    second = native.open_scope()
    try:
        root = second.root(str(tmp_path_factory))
        first.close()
        assert second.identity(root)[1]
    finally:
        first.close()
        second.close()


def test_no_terminator_or_device_alias_reaches_the_native_open(native, tmp_path_factory):
    scope = native.open_scope()
    try:
        root = scope.root(str(tmp_path_factory))
        before = scope.snapshot().handles
        for name in ("file\0ignored", "..", "NUL", "file:stream", "file/child"):
            with pytest.raises((ValueError, PermissionError)):
                scope.open(name, root)
        assert scope.snapshot().handles == before
    finally:
        scope.close()


def test_native_account_ignores_environment_and_closes_token_before_return(native, monkeypatch):
    scope = native.open_scope()
    try:
        monkeypatch.setenv("USERPROFILE", r"C:\untrusted")
        monkeypatch.setenv("HOME", r"C:\untrusted")
        sid, profile = scope.account()
        assert sid.startswith("S-1-5-") and profile != r"C:\untrusted"
        assert scope.snapshot().handles == 0
    finally:
        scope.close()


@pytest.mark.parametrize("field,value", [("volume", True), ("volume", -1),
    ("volume", 2**64), ("file_id", 2**128), ("file_id", -1)])
def test_identity_fields_are_never_wrapped_or_coerced(field, value):
    with pytest.raises(ValueError):
        _identity_words(replace(ObjectIdentity("win32", 1, 1), **{field: value}))


@pytest.mark.parametrize("mode", range(1, 10))
def test_native_after_effect_faults_never_retry_unknown_closes(native, tmp_path_factory, mode):
    executable = Path(os.environ["GITGO_STORAGE_NATIVE_TEST_LIBRARY"]).with_name("storage-scope-faults.exe")
    assert executable.is_file(), "The dedicated native fault binary is required"
    (tmp_path_factory / "gate").write_bytes(b"content")
    result = subprocess.run([str(executable), str(tmp_path_factory), str(mode)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"fault mode {mode} passed" in result.stdout
    assert f"quarantined={1 if mode in {4, 7, 9} else 0}" in result.stdout
