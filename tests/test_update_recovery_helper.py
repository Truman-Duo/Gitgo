"""Independent lab recovery, bounded IPC and captured authority (not shipping update)."""

from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from backend.core.update.recovery_helper import (
    MAX_REQUEST_BYTES, ROLE, RecoveryRequest, helper_main, recover_in_helper, recover_request,
)
from backend.core.update.persistence import atomic_control, read_control
from backend.core.update.tree import encode, tree_digest
from backend.core.update.validation import UpdateError
from tests.test_update_transaction import lab, signing, apply, Crash, crash_at


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Explicit POSIX lab helper; no Windows installation adapter")


def test_request_roundtrip_requires_local_captured_authority(lab):
    request = RecoveryRequest.capture(lab[0])
    assert RecoveryRequest.parse(request.wire()) == request
    assert request.installation_id == lab[0].registration["installation_id"]
    assert request.protected_paths == lab[0].protected_paths


@pytest.mark.parametrize("patch", [
    {"schema_version": True}, {"schema_version": 2}, {"nonce": "not-a-nonce"},
    {"installation_id": "wrong"}, {"device": True}, {"inode": 0},
    {"protected_paths": []}, {"protected_paths": [{}]}, {"protected_paths": ["relative"]},
    {"root": "../installation"}, {"root": "/private/tmp/../wrong"},
])
def test_invalid_request_is_rejected_before_directory_access(lab, patch):
    wire = RecoveryRequest.capture(lab[0]).wire()
    wire.update(patch)
    with pytest.raises(UpdateError):
        RecoveryRequest.parse(wire)


@pytest.mark.parametrize("change", ["id", "inode", "device", "protection", "production"])
def test_stale_or_reduced_authority_never_mutates_the_ledger(lab, change):
    store = lab[0]
    request = RecoveryRequest.capture(store)
    if change == "id": request = replace(request, installation_id="f" * 32)
    if change == "inode": request = replace(request, inode=request.inode + 1)
    if change == "device": request = replace(request, device=request.device + 1 if type(request.device) is int
        else "darwin-volume:11111111-2222-3333-4444-555555555555")
    if change == "protection": request = replace(request, protected_paths=request.protected_paths[:1])
    if change == "production":
        path = store.root / "control/registration.json"
        value = read_control(path); value["simulation"] = False; atomic_control(path, value)
    before = (store.root / "control/ledger.json").read_bytes()
    with pytest.raises(UpdateError): recover_request(request)
    assert (store.root / "control/ledger.json").read_bytes() == before
    assert (store.root / "active/program").is_file()


@pytest.mark.parametrize("point", ["prepared", "old_renamed", "new_renamed", "health_passed"])
def test_role_recovers_pending_transactions_and_is_idempotent(lab, point):
    request = RecoveryRequest.capture(lab[0])
    with pytest.raises(Crash): apply(lab, checkpoint=crash_at(point))
    # The role starts from serialized local authority, not the in-memory store.
    first = io.StringIO()
    assert helper_main([], stdin=io.BytesIO(encode(request.wire())), stdout=first) == 0
    result = json.loads(first.getvalue())
    assert result["status"] == "rolled_back"
    assert result["version"] == "1.0.0-alpha.2"
    assert (lab[0].root / "active/program").is_file()
    assert recover_request(request) == result


def test_role_is_busy_without_killing_or_overwriting_a_live_runtime(lab):
    request = RecoveryRequest.capture(lab[0])
    before = (lab[0].root / "control/ledger.json").read_bytes()
    with lab[0].runtime_session():
        assert recover_request(request)["status"] == "busy"
    assert (lab[0].root / "control/ledger.json").read_bytes() == before


@pytest.mark.parametrize("raw,args", [
    (b"not-json-private-path", []), (b"x" * (MAX_REQUEST_BYTES + 1), []),
    (b'{}', []), (b'{"schema_version":1,"schema_version":1}', []),
    (b"{}", ["--root", "private-path-must-not-be-echoed"]),
], ids=["invalid-json", "oversized-input", "empty-request", "duplicate-schema", "unexpected-argument"])
def test_role_failures_are_bounded_and_do_not_echo_private_input(raw, args):
    output = io.StringIO()
    assert helper_main(args, stdin=io.BytesIO(raw), stdout=output) == 2
    assert json.loads(output.getvalue()) == {"schema_version": 1, "error_code": "UPDATE_HELPER_REQUEST_INVALID"}


@pytest.fixture
def coordinator(lab, monkeypatch):
    from backend.core import process_control, process_output
    store = lab[0]
    parent = store.root.parent
    helper = parent / "stable-helper"; helper.mkdir(mode=0o700)
    host = helper / "gitgo-host"; host.write_bytes(b"mocked frozen helper; never executed"); host.chmod(0o700)
    profile = parent / "helper-profile"; profile.mkdir(mode=0o700)
    request = RecoveryRequest.capture(store)
    captured = {}
    proc = SimpleNamespace(returncode=0, poll=lambda: 0,
        _gitgo_cleanup={"complete": False, "mode": "owned-session-only", "issues": ["not-full-containment"]})
    def spawn(argv, **kwargs): captured.update(argv=argv, **kwargs); return proc
    def output(*args, **kwargs):
        captured["input"] = kwargs["input"]
        result = recover_request(RecoveryRequest.parse(json.loads(kwargs["input"])))
        return json.dumps(result), "", False
    monkeypatch.setattr(process_control, "spawn_managed", spawn)
    monkeypatch.setattr(process_control, "attach_kill_job", lambda _: None)
    monkeypatch.setattr(process_control, "close_job", lambda _: None)
    monkeypatch.setattr(process_control, "terminate_tree", lambda *_: captured.update(terminated=True))
    monkeypatch.setattr(process_output, "collect_output", output)
    return host, tree_digest(helper), request, profile, captured, proc


def test_coordinator_keeps_cleanup_separate_and_drops_inherited_keys(coordinator, monkeypatch):
    host, sha, request, profile, captured, _ = coordinator
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-credential-not-forwarded")
    monkeypatch.setenv("PYTHONPATH", "must-not-forward")
    result = recover_in_helper(host, sha, request, profile)
    assert result["recovery"]["status"] == "idle"
    assert result["cleanup"]["complete"] is False
    assert captured["argv"] == [str(host), "--gitgo-internal-role", ROLE]
    assert "DEEPSEEK_API_KEY" not in captured["env"]
    assert "PYTHONPATH" not in captured["env"]
    assert str(request.root) not in " ".join(captured["argv"])
    assert captured["env"]["GITGO_CONFIG_PATH"] == str(profile / "config.json")


@pytest.mark.parametrize("fault", ["nonce", "id", "status", "version", "schema", "cleanup", "lie", "overflow", "timeout", "oversized_stdout", "changed_helper", "exit"])
def test_coordinator_rejects_untrusted_or_incomplete_reports(coordinator, monkeypatch, fault):
    from backend.core import process_output
    host, sha, request, profile, _, proc = coordinator
    if fault == "exit": proc.returncode = 1
    if fault == "cleanup": proc._gitgo_cleanup = None
    def output(*args, **kwargs):
        result = recover_request(request)
        if fault == "nonce": result["nonce"] = "c" * 32
        if fault == "id": result["installation_id"] = "c" * 32
        if fault == "status": result["status"] = "available"
        if fault == "version": result["version"] = "9.0.0"
        if fault == "schema": result["schema_version"] = True
        if fault == "lie": result["status"] = "committed"  # Same version but wrong ledger state.
        if fault == "overflow": raise ValueError("sensitive-proxy-detail")
        if fault == "timeout": raise subprocess.TimeoutExpired("private-helper-command", 30)
        if fault == "oversized_stdout": return " " * (64 * 1024 + 1), "", False
        if fault == "changed_helper": host.write_bytes(b"changed after launch")
        return json.dumps(result), "", False
    monkeypatch.setattr(process_output, "collect_output", output)
    with pytest.raises(UpdateError, match="UPDATE_HELPER_FAILED") as exc:
        recover_in_helper(host, sha, request, profile)
    assert "sensitive-proxy-detail" not in str(exc.value)


def test_changed_helper_or_nonempty_profile_is_rejected_before_spawn(coordinator):
    host, sha, request, profile, captured, _ = coordinator
    host.write_bytes(b"unexpected executable replacement")
    with pytest.raises(UpdateError, match="UPDATE_HELPER_INVALID"):
        recover_in_helper(host, sha, request, profile)
    assert not captured
    (profile / "existing-user-document").write_bytes(b"keep")
    with pytest.raises(UpdateError): recover_in_helper(host, tree_digest(host.parent), request, profile)
    assert (profile / "existing-user-document").read_bytes() == b"keep"
    assert not captured


def test_missing_helper_and_root_errors_never_expose_paths(coordinator):
    host, sha, request, profile, captured, _ = coordinator
    missing = host.parent / "private-missing-host"
    with pytest.raises(UpdateError) as exc:
        recover_in_helper(missing, sha, request, profile)
    assert str(missing) not in str(exc.value)
    with pytest.raises(UpdateError) as exc:
        recover_request(replace(request, root=request.root.parent / "private-missing-installation"))
    assert "private-missing-installation" not in str(exc.value)
    assert not captured


@pytest.mark.parametrize("point", ["old_renamed", "new_renamed", "commit_written"])
def test_actual_exit_then_fresh_helper_process_recovers_without_app_initialization(lab, point):
    store, _, (raw, detached, archive), keys, _ = lab
    request = RecoveryRequest.capture(store)
    public = keys[0].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    script = '''
import os, sys
from pathlib import Path
from backend.core.update import ManifestVerifier
from backend.core.update.transaction import SimulationStore
from tests.test_update_discovery import NOW, POLICY
from tests.test_update_transaction import inspect_health
root, archive, raw, detached, public, point, *protected = sys.argv[1:]
store = SimulationStore(Path(root), protected_paths=tuple(Path(p) for p in protected))
def checkpoint(event):
    if event == point: os._exit(73)
store.apply(POLICY, ManifestVerifier({"ephemeral-test": bytes.fromhex(public)}), bytes.fromhex(raw),
    bytes.fromhex(detached), Path(archive), health_check=inspect_health, clock=lambda: NOW, checkpoint=checkpoint)
'''
    crash = subprocess.run([sys.executable, "-B", "-c", script, str(store.root), str(archive), raw.hex(),
        detached.hex(), public, point, *(str(p) for p in store.protected_paths)], capture_output=True, timeout=20)
    assert crash.returncode == 73, crash.stderr.decode(errors="replace")
    profile = store.root.parent / "child-profile"; profile.mkdir(mode=0o700)
    from backend.core.update.health import isolated_environment
    child = subprocess.run([sys.executable, "-B", "-m", "backend.core.native_host_entry",
        "--gitgo-internal-role", ROLE], input=encode(request.wire()), capture_output=True,
        env=isolated_environment(profile), cwd=Path(__file__).resolve().parents[1], timeout=20)
    assert child.returncode == 0, child.stderr.decode(errors="replace")
    result = json.loads(child.stdout)
    assert result["nonce"] == request.nonce
    assert result["status"] == ("committed" if point == "commit_written" else "rolled_back")
    assert not list(profile.iterdir())  # Role never starts ordinary Host/credentials/storage.
    with store.runtime_session(): pass


@pytest.mark.skipif(not os.environ.get("GITGO_UPDATE_TEST_HELPER"), reason="Requires an explicitly supplied built frozen Host outside the lab installation")
@pytest.mark.parametrize("point", ["old_renamed", "new_renamed", "commit_written"])
def test_explicit_frozen_helper_preserves_recorded_identity_and_protected_data(lab, point):
    # Optional real-artifact evidence, not a mocked executable. The caller
    # supplies a private, separately built stable Host; do not change its files.
    host = Path(os.environ["GITGO_UPDATE_TEST_HELPER"])
    pinned = tree_digest(host.parent)
    store = lab[0]
    request = RecoveryRequest.capture(store)
    with pytest.raises(Crash): apply(lab, checkpoint=crash_at(point))
    profile = store.root.parent / "frozen-helper-profile"; profile.mkdir(mode=0o700)
    result = recover_in_helper(host, pinned, request, profile, timeout=60)
    assert result["recovery"]["status"] == ("committed" if point == "commit_written" else "rolled_back")
    assert result["recovery"]["installation_id"] == request.installation_id
    assert type(result["cleanup"]["complete"]) is bool
    if sys.platform == "darwin":
        assert result["cleanup"]["complete"] is False  # Never fake full macOS coverage.
    assert not list(profile.iterdir())
    with store.runtime_session(): pass
