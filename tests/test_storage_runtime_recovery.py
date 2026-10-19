"""Real parent crashes, exact process identities and conservative recovery."""
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from backend.core.storage.envelope import FileEnvelope, StorageEnvelope
from backend.core.storage.host_records import HostAccount, HostLayout, metadata_classes
from backend.core.storage.models import StorageBlocked
from backend.core.storage.native_windows_host import WindowsHostRegistry
from backend.core.storage.native_windows_journal import WindowsJournalLibrary
from backend.core.storage.native_windows_process import WindowsProcessLibrary, StorageProcessIdentity
from backend.core.storage.runtime_control import RuntimeControl
from backend.core.storage.runtime_lifecycle import recover
from backend.core.storage.runtime_manifest import with_runtime_copies
from tests.test_storage_runtime_control import reopen
from tests.test_storage_runtime_launch import packaged_input
from tests.test_storage_windows_tree import native


def command(native, temporary, manifest, mode):
    fixture = Path(__file__).parent/"fixtures/storage_runtime_parent.py"
    return [sys.executable, "-B", str(fixture), str(temporary),
        str(Path(manifest.root)/"runtime-manifest.json"), manifest.sha256,
        str(native[2]/"gitgo-storage-scope.dll"), mode]


def dependencies(native, manifest, report):
    scopes, trees, binaries = native
    journals = WindowsJournalLibrary(ctypes.CDLL(str(binaries/"gitgo-storage-journal.dll")))
    processes = WindowsProcessLibrary(ctypes.CDLL(str(binaries/"gitgo-storage-process.dll")))
    envelope = StorageEnvelope(1, 4096, 2, 256*1024*1024, metadata_classes(2), (FileEnvelope("state", 65536, 2),))
    registry = WindowsHostRegistry(scopes, trees, journals,
        HostLayout(with_runtime_copies(envelope, manifest.content), manifest.content))
    return registry, processes, HostAccount(report["sid"], report["profile"])


@pytest.mark.parametrize("mode,expected", [("complete", "READY"), ("partial", "RETIRING")])
def test_recovery_after_real_parent_exit_uses_native_death_and_exact_committed_files(
        native, tmp_path_factory, packaged_input, mode, expected):
    result = subprocess.run(command(native, tmp_path_factory, packaged_input, mode),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 89, result.stderr
    report = json.loads(result.stdout)
    registry, processes, account = dependencies(native, packaged_input, report)
    host = reopen(registry, account)
    try:
        assert recover(RuntimeControl(host), 0, processes) == expected
        entry = host._read_slot(63).head.state.value()["entries"][0]
        assert entry["worker"]["phase"] == "RETIRED" and entry["generation"] == report["generation"]
    finally:
        assert host.close()
    assert not processes.retained_processes and not registry.journals.retained_journals


def test_missing_child_identity_is_not_reconstructed_after_parent_crash(native, tmp_path_factory, packaged_input):
    result = subprocess.run(command(native, tmp_path_factory, packaged_input, "unknown_identity"),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 89, result.stderr
    report = json.loads(result.stdout)
    registry, processes, account = dependencies(native, packaged_input, report)
    host = reopen(registry, account)
    try:
        before = host._read_slot(63).raw
        with pytest.raises(StorageBlocked, match="unknown native creation"):
            recover(RuntimeControl(host), 0, processes)
        assert (Path(account.root)/"slot-0063.json").read_bytes() == before
        assert not list((Path(account.root)/"runtimes").iterdir())
        assert not processes.retained_processes
    finally:
        assert host.close()


@pytest.mark.parametrize("operation", ["ended", "retire"])
def test_unknown_native_recovery_observation_or_close_cannot_commit_retirement(
        native, tmp_path_factory, packaged_input, monkeypatch, operation):
    result = subprocess.run(command(native, tmp_path_factory, packaged_input, "complete"),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 89, result.stderr
    registry, processes, account = dependencies(native, packaged_input, json.loads(result.stdout))
    host = reopen(registry, account)
    before = host._read_slot(63).raw
    original = processes._calls[operation]
    def lost(*args):
        assert original(*args) == 0
        raise RuntimeError("lost native recovery return")
    try:
        with monkeypatch.context() as patch:
            patch.setitem(processes._calls, operation, lost)
            with pytest.raises(RuntimeError, match="lost native recovery"):
                recover(RuntimeControl(host), 0, processes)
        assert not host._ready and processes.retained_processes
        assert (Path(account.root)/"slot-0063.json").read_bytes() == before
    finally:
        assert host.close()
    assert not processes.retained_processes
    host = reopen(registry, account)
    try:
        assert recover(RuntimeControl(host), 0, processes) == "READY"
    finally:
        assert host.close()


def test_dead_worker_and_released_gate_cannot_retire_a_living_parent(native, tmp_path_factory, packaged_input):
    process = subprocess.Popen(command(native, tmp_path_factory, packaged_input, "alive_parent"),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    registry = processes = None
    try:
        report = json.loads(process.stdout.readline())
        registry, processes, account = dependencies(native, packaged_input, report)
        host = reopen(registry, account)
        try:
            before = host._read_slot(63).raw
            with pytest.raises(StorageBlocked, match="process is still alive"):
                recover(RuntimeControl(host), 0, processes)
            assert (Path(account.root)/"slot-0063.json").read_bytes() == before
        finally:
            assert host.close()
        process.stdin.write("R");process.stdin.flush()
        assert process.wait(timeout=20) == 89
        host = reopen(registry, account)
        try:
            assert recover(RuntimeControl(host), 0, processes) == "READY"
        finally:
            assert host.close()
    finally:
        if process.poll() is None:
            process.kill();process.wait(timeout=20)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        if registry is not None:
            for owner in list(registry._owners.values()): assert owner.close()
        if processes is not None:
            for owner in list(processes._owners.values()):
                deadline = time.monotonic()+15
                while not owner.retire():
                    assert time.monotonic() < deadline
                    time.sleep(.01)
