from contextlib import contextmanager
from dataclasses import replace
import ctypes
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from backend.core.storage import journal_cells as cells, slot_records, runtime_records
from backend.core.storage.native_windows_journal import StorageJournalError, WindowsJournalLibrary
from backend.core.storage.native_windows_scope import WindowsScopeLibrary


@pytest.fixture
def native():
    if sys.platform != "win32":
        pytest.skip("Windows native storage journals")
    selected = os.environ.get("GITGO_STORAGE_NATIVE_TEST_LIBRARY")
    if not selected:
        pytest.skip("Build native tests with scripts/build_storage_native.py")
    path = Path(selected).resolve(strict=True)
    scope = WindowsScopeLibrary(ctypes.CDLL(str(path)))
    journal = WindowsJournalLibrary(ctypes.CDLL(str(path.with_name("gitgo-storage-journal.dll"))))
    yield scope, journal
    retained = scope.retained_scopes + journal.retained_journals
    for owner in list(journal._owners.values()):
        owner.close()
    for owner in list(scope._owners.values()):
        owner.close()
    assert retained == 0


@contextmanager
def fixture(native, root, payload=b"", index=0):
    scope_lib, journal = native
    path = root / f"slot-{index:04d}.json"
    path.write_bytes(payload)
    scope = scope_lib.open_scope()
    try:
        parent = scope.root(str(root))
        parent_id = scope.identity(parent)[0]
        file = scope.open(path.name, parent)
        file_id = scope.identity(file)[0]
        scope.close_handle(file)
        yield journal, parent, parent_id, file_id, path
    finally:
        for owner in list(journal._owners.values()):
            owner.close()
        scope.close()


def reservation():
    body = dict(version=1, registry_id=str(uuid.uuid4()), envelope="a"*64, slot=0,
                project=str(uuid.uuid4()), fingerprint="b"*64)
    return slot_records.WorkerSlotState(cells.canonical(dict(body=body,
        checksum=hashlib.sha256(cells.canonical(body)).hexdigest())))


def event(generation=0):
    return dict(phase="INTENT", launch=str(uuid.uuid4()), pid=0, created=0, parent_pid=42,
                parent_created=123, profile="c"*64, mode="publish", request=str(uuid.uuid4()),
                generation=generation, runtime=dict(generation=str(uuid.uuid4()), manifest="d"*64, epoch=1))


def test_journal_survives_100_reopens_without_growing(native, tmp_path_factory):
    with fixture(native, tmp_path_factory) as (library, parent, parent_id, file_id, path):
        owner = library.reserve(slot_records.FORMAT)
        owner.open(parent, 0, parent_id, file_id, initialize=True)
        view = owner.update(owner.read(), reservation())
        state = view.head.state
        owner.close()
        for generation in range(100):
            owner = library.reserve(slot_records.FORMAT)
            owner.open(parent, 0, parent_id, file_id)
            value = event(generation)
            for phase in ("INTENT", "CREATED", "ACCEPTED", "RETIRED"):
                if phase == "CREATED":
                    value = dict(value, pid=43, created=124)
                value = dict(value, phase=phase)
                owner.update(owner.read(), state.with_event(value))
            assert owner.read().head.sequence == 1 + 4*(generation+1)
            owner.close()
            assert path.stat().st_size == 8192


def test_stale_full_snapshot_cannot_overwrite_newer_intent(native, tmp_path_factory):
    with fixture(native, tmp_path_factory) as (library, parent, parent_id, file_id, path):
        owner = library.reserve(slot_records.FORMAT)
        owner.open(parent, 0, parent_id, file_id, initialize=True)
        prior = owner.update(owner.read(), reservation())
        actual = owner.update(prior, prior.head.state.with_event(event()))
        with pytest.raises(StorageJournalError):
            owner.update(prior, prior.head.state.with_event(event()))
        with pytest.raises(StorageJournalError):
            owner.read()
        owner.close()
        assert path.read_bytes() == actual.raw


@pytest.mark.parametrize("payload", [b"legacy\n", bytes(8191), bytes(8193)], ids=["legacy", "short", "oversize"])
def test_wrong_size_refuses_without_truncation(native, tmp_path_factory, payload):
    with fixture(native, tmp_path_factory, payload) as (library, parent, parent_id, file_id, path):
        owner = library.reserve(slot_records.FORMAT)
        with pytest.raises(StorageJournalError):
            owner.open(parent, 0, parent_id, file_id)
        owner.close()
        assert path.read_bytes() == payload


def test_exact_native_file_denies_replacement_and_competing_writer(native, tmp_path_factory):
    with fixture(native, tmp_path_factory) as (library, parent, parent_id, file_id, path):
        owner = library.reserve(slot_records.FORMAT)
        owner.open(parent, 0, parent_id, file_id, initialize=True)
        with pytest.raises(PermissionError):
            path.rename(path.with_suffix(".moved"))
        with pytest.raises(PermissionError):
            path.write_bytes(b"changed")
        with pytest.raises(PermissionError):
            path.unlink()
        other = library.reserve(slot_records.FORMAT)
        with pytest.raises(StorageJournalError):
            other.open(parent, 0, parent_id, file_id)
        other.close()
        assert not any(owner.read().raw)


@pytest.mark.parametrize("kind", ["parent", "file", "links"])
def test_native_identity_and_link_count_refuse(native, tmp_path_factory, kind):
    with fixture(native, tmp_path_factory) as (library, parent, parent_id, file_id, path):
        if kind == "parent":
            parent_id = replace(parent_id, file_id=parent_id.file_id ^ 1)
        if kind == "file":
            file_id = replace(file_id, file_id=file_id.file_id ^ 1)
        if kind == "links":
            os.link(path, path.with_suffix(".alias"))
        owner = library.reserve(slot_records.FORMAT)
        with pytest.raises(StorageJournalError):
            owner.open(parent, 0, parent_id, file_id, initialize=True)
        owner.close()
        assert path.read_bytes() == b""


def test_corrupt_nonempty_peer_retains_unknown_without_repair(native, tmp_path_factory):
    payload = bytearray(8192)
    payload[4100] = 1
    with fixture(native, tmp_path_factory, bytes(payload)) as (library, parent, parent_id, file_id, path):
        owner = library.reserve(slot_records.FORMAT)
        owner.open(parent, 0, parent_id, file_id)
        with pytest.raises(cells.JournalUnknown):
            owner.read()
        with pytest.raises(StorageJournalError):
            owner.read()
        owner.close()
        assert path.read_bytes() == payload


def test_runtime_records_use_only_the_prepaid_runtime_slot(native, tmp_path_factory):
    with fixture(native, tmp_path_factory, index=63) as (library, parent, parent_id, file_id, path):
        owner = library.reserve(runtime_records.FORMAT)
        with pytest.raises(ValueError):
            owner.open(parent, 0, parent_id, file_id, initialize=True)
        assert path.read_bytes() == b""
        owner.open(parent, 63, parent_id, file_id, initialize=True)
        state = runtime_records.RuntimeRegistryState(cells.canonical(dict(binding="a"*64,
            epoch=0, active=None, entries=[None, None])))
        view = owner.update(owner.read(), state)
        assert view.head.state == state


def test_lost_retirement_cannot_close_new_journal(native, tmp_path_factory):
    with fixture(native, tmp_path_factory, bytes(8192)) as (library, parent, parent_id, file_id, path):
        first = library.reserve(slot_records.FORMAT)
        first.open(parent, 0, parent_id, file_id)
        assert library._calls["retire"](first._cookie) == 0
        second = library.reserve(slot_records.FORMAT)
        second.open(parent, 0, parent_id, file_id)
        first.close()
        assert second.read().raw == bytes(8192)
        with pytest.raises(PermissionError):
            path.unlink()


def test_native_after_effect_faults_preserve_unknown_debt(native, tmp_path_factory):
    executable = Path(os.environ["GITGO_STORAGE_NATIVE_TEST_LIBRARY"]).with_name("storage-journal-faults.exe")
    result = subprocess.run([str(executable), str(tmp_path_factory)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "slot native faults passed" in result.stdout and "quarantined=1" in result.stdout
