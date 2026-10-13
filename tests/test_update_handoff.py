"""Handoff/state-machine units; placeholder products are NEVER executed.

Platform and health mocks exercise wiring, not Linux/macOS release acceptance.
Actual frozen startup tests live separately and retain the macOS refusal gate.
"""
import json
import os
from types import SimpleNamespace

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import httpx
import pytest

from backend.core.application import ApplicationServices, OperationError
from backend.core.update import UpdateError, installation, health, handoff
from backend.core.update.installation import initialize_managed, open_managed
from backend.core.update.preparation_service import UpdatePreparationService
from backend.core.update.persistence import atomic_control, read_control, lock_file
from backend.core.update.tree import tree_digest
from backend.core.installation_lease import retain_descriptor, runtime_descriptors
from tests.test_update_transaction import build_fixture, refresh_build, archive_fixture, inspect_health, Crash
from tests.test_update_manifest import signing
from tests.test_update_preparation_service import policy_wire, httpx_transport, Stream
from tests.test_update_discovery import NOW

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Managed handoff requires POSIX ownership")


@pytest.fixture
def lab(tmp_path_factory, monkeypatch, signing):
    base = tmp_path_factory.resolve()
    protected = base / "protected"; protected.mkdir(mode=0o700)
    sentinels = {name: ("preserve-" + name).encode() for name in ("project", "config", "db", "db-wal", "db-shm", "secrets")}
    for name, data in sentinels.items(): (protected / name).write_bytes(data)
    seed = base / "seed"; build_fixture(seed, version="1.0.0-alpha.2", platform="linux", architecture="x86_64")
    public = signing[0].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    policy = {**policy_wire(public), "platform": "linux", "architecture": "x86_64"}
    (seed / "release-trust.json").write_text(json.dumps(policy))
    refresh_build(seed, version="1.0.0-alpha.2", platform="linux", architecture="x86_64")
    def probe(host, profile):
        product = json.loads((host.parents[2] / "product.json").read_bytes())
        contract = json.loads((host.parents[2] / "update-data-contract.json").read_bytes())
        return {"runtime": {**{k: product[k] for k in ("product_id", "version", "platform", "architecture")},
            "data_compatibility": contract["data_compatibility"]}, "cleanup": {"complete": False}}
    monkeypatch.setattr(installation, "probe_managed_host", probe)
    root = base / "managed"; root.mkdir(mode=0o700)
    store = initialize_managed(root, seed, protected_paths=(protected,))
    candidate = base / "candidate"; build_fixture(candidate, platform="linux", architecture="x86_64")
    inputs = base / "inputs"; inputs.mkdir(mode=0o700)
    raw, signature, archive = archive_fixture(candidate, inputs, signing, platform="linux", architecture="x86_64")
    calls = []
    def serve(request):
        calls.append(str(request.url))
        return httpx.Response(200, stream=Stream(raw if str(request.url).endswith(".json") else signature
                              if str(request.url).endswith(".sig") else archive.read_bytes()))
    service = UpdatePreparationService(store_loader=lambda: open_managed(root), clock=lambda: NOW,
        transport_factory=lambda hosts, **options: httpx_transport(hosts, serve, **options))
    selected = service.check("preview", "a" * 32)["candidate"]["candidate_id"]
    service.download("preview", selected, "b" * 32)
    monkeypatch.setattr(handoff, "platform_ready", lambda _: True)  # unit wiring only
    monkeypatch.setattr(handoff, "datetime", SimpleNamespace(now=lambda _: NOW))
    monkeypatch.setattr(health, "frozen_health_check", inspect_health)  # static fixtures only
    before = tree_digest(root / "active")
    yield SimpleNamespace(store=store, service=service, selected=selected, calls=calls, before=before)
    assert all((protected / name).read_bytes() == data for name, data in sentinels.items())


def queue(lab): return lab.service.request_apply("preview", lab.selected, "c" * 32)
def consume(lab, nonce, cleanup=None, code=75):
    return handoff._consume_request(lab.store, nonce, code, {"complete": True} if cleanup is None else cleanup)


def test_queue_is_confirmed_exact_selection_not_apply_or_network(lab, monkeypatch):
    with handoff.launch_session(lab.store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        assert lab.service.status("preview")["can_request_apply"] is True
        before = lab.store._ledger(); calls = list(lab.calls)
        assert queue(lab) == {"schema_version": 1, "status": "exit_required", "candidate_id": lab.selected,
                              "channel": "preview", "exit_code": 75}
        assert queue(lab)["status"] == "exit_required"
        assert lab.calls == calls and lab.store._ledger() == before
        assert tree_digest(lab.store.root / "active") == lab.before
        assert consume(lab, nonce, code=0) is None  # Ordinary quit never applies.
    assert not lab.service.status("preview")["can_request_apply"]


def test_direct_or_forged_environment_has_no_live_launcher_ticket(lab, monkeypatch):
    for nonce in ("", "f" * 32, "../../elsewhere"):
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        assert not lab.service.status("preview")["can_request_apply"]
        with pytest.raises((UpdateError, OSError)): queue(lab)
    assert not list((lab.store.root / "control/preparation").glob("request-*"))
    with pytest.raises(UpdateError, match="UPDATE_BOOTSTRAP_ROLE"):
        handoff.consume_request(lab.store, "f" * 32, 75, {"complete": True})


def test_platform_refusal_is_not_user_override(lab, monkeypatch):
    monkeypatch.setattr(handoff, "platform_ready", lambda _: False)
    with handoff.launch_session(lab.store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        assert not lab.service.status("preview")["can_request_apply"]
        with pytest.raises(UpdateError, match="UPDATE_SCOPE_UNAVAILABLE"): queue(lab)
        assert consume(lab, nonce).status == "blocked"
    assert tree_digest(lab.store.root / "active") == lab.before


@pytest.mark.parametrize("cleanup", [None, {}, {"complete": False}, {"complete": 1}, {"complete": "true"}])
def test_incomplete_or_malformed_cleanup_never_switches(lab, monkeypatch, cleanup):
    with handoff.launch_session(lab.store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce); queue(lab)
        result = handoff._consume_request(lab.store, nonce, 75, cleanup)
        assert result.error_code == "UPDATE_SCOPE_UNAVAILABLE"
        assert tree_digest(lab.store.root / "active") == lab.before
        assert lab.store._ledger()["transaction"] is None


def test_shared_launcher_descriptor_must_be_released_and_other_instances_block(lab, monkeypatch):
    with handoff.launch_session(lab.store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce); queue(lab)
        with lab.store.runtime_lease() as (_, fd):
            with retain_descriptor(fd): assert consume(lab, nonce).error_code == "UPDATE_SCOPE_UNAVAILABLE"
            assert runtime_descriptors() == ()
            assert consume(lab, nonce).error_code == "UPDATE_BUSY"
        assert tree_digest(lab.store.root / "active") == lab.before
        assert consume(lab, nonce).status == "committed"  # static unit health only
        assert consume(lab, nonce).status == "blocked"  # Cannot replay old current identity.


def test_failed_candidate_health_restores_old_and_keeps_high_water(lab, monkeypatch):
    def failed(*_): raise UpdateError("UPDATE_HEALTH_FAILED", "unit injected startup failure")
    monkeypatch.setattr(health, "frozen_health_check", failed)
    high_water = lab.store._ledger()["watermarks"]
    with handoff.launch_session(lab.store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce); queue(lab)
        result = consume(lab, nonce)
        assert result.status == "rolled_back" and result.error_code == "UPDATE_HEALTH_FAILED"
        assert tree_digest(lab.store.root / "active") == lab.before
        assert lab.store._ledger()["watermarks"] == high_water
        assert open_managed(lab.store.root).recover().status == "rolled_back"


@pytest.mark.parametrize("fault", ["request", "check", "archive", "payload", "expired"])
def test_reverification_after_shutdown_rejects_changed_evidence(lab, monkeypatch, fault):
    with handoff.launch_session(lab.store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce); queue(lab)
        parent = lab.store.root / "control/preparation"
        if fault == "request":
            path = handoff._path(lab.store, nonce, "request")
            value = read_control(path); value["current"]["version"] = "1.0.0-alpha.1"; atomic_control(path, value)
        if fault == "check":
            path = parent / "check-preview.json"
            value = read_control(path); value["candidate_id"] = "f" * 64; atomic_control(path, value)
        if fault in ("archive", "payload"):
            receipt = read_control(parent / "prepared-preview.json"); work = parent / receipt["directory"]
            path = work / "archive.verified" if fault == "archive" else work / receipt["bundle"] / "gitgo"
            path.chmod(0o600); path.write_bytes(b"changed")
        if fault == "expired":
            from datetime import timedelta
            monkeypatch.setattr(handoff, "datetime", SimpleNamespace(now=lambda _: NOW + timedelta(days=4)))
        assert consume(lab, nonce).status == "blocked"
        assert tree_digest(lab.store.root / "active") == lab.before
        assert lab.store._ledger()["transaction"] is None


def test_interrupted_switch_is_recovered_by_fresh_store_not_replayed_ticket(lab, monkeypatch):
    save = lab.store._save
    def crash(ledger, state=None, *, failure=None):
        result = save(ledger, state, failure=failure)
        if state == "CHECK_NEW": raise Crash()
        return result
    monkeypatch.setattr(lab.store, "_save", crash)
    with handoff.launch_session(lab.store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce); queue(lab)
        with pytest.raises(Crash): consume(lab, nonce)
        fresh = open_managed(lab.store.root)
        assert fresh.recover().status == "rolled_back"
        assert tree_digest(fresh.root / "active") == lab.before


def test_rpc_accepts_no_path_key_or_health_override_and_cancelled_request_never_queues(lab, monkeypatch):
    services = ApplicationServices(); services.updates = lab.service
    with handoff.launch_session(lab.store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        args = {"channel": "preview", "candidate_id": lab.selected, "operation_id": "d" * 32}
        for key in ("root", "health_check", "enabled", "public_key", "url", "session"):
            with pytest.raises(OperationError) as error: services.invoke("update.request_apply", {**args, key: "override"})
            assert error.value.code == "INVALID_ARGUMENTS"
        lab.service.cancel("d" * 32)
        with pytest.raises(OperationError, match="UPDATE_CANCELLED"): services.invoke("update.request_apply", args)
        assert not handoff._path(lab.store, nonce, "request").exists()


def test_busy_preparation_does_not_block_ordinary_launch_or_quit(lab):
    path = lab.store.root / "control/preparation.lock"
    with lock_file(path):
        with handoff.launch_session(lab.store) as nonce:
            assert nonce is None
            with lab.store.runtime_lease(): pass
    session = handoff.launch_session(lab.store)
    nonce = session.__enter__()
    with lock_file(path): session.__exit__(None, None, None)
    # Our owned ticket may be retained for bounded maintenance, never adopted.
    assert handoff._path(lab.store, nonce, "session").exists()


@pytest.mark.parametrize("complete", [True, False])
def test_launcher_releases_leases_before_apply_and_restarts_with_no_task_replay(lab, monkeypatch, complete):
    import sys
    from backend.core import process_control
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(lab.store.root / "bootstrap/gitgo-host/gitgo-host"))
    calls = []
    def spawn(argv, *, cwd, env):
        assert runtime_descriptors()  # Runtime is protected until the child stops.
        calls.append(argv)
        if len(calls) == 1:
            monkeypatch.setenv(handoff.SESSION_ENV, env[handoff.SESSION_ENV])
            queue(lab)
            return SimpleNamespace(wait=lambda: None, returncode=75, _gitgo_cleanup={"complete": complete})
        assert argv == [str(lab.store.root / "active/gitgo")]
        assert lab.store._ledger()["current"]["version"] == ("1.0.0-alpha.3" if complete else "1.0.0-alpha.2")
        return SimpleNamespace(wait=lambda: None, returncode=0, _gitgo_cleanup={"complete": complete})
    monkeypatch.setattr(process_control, "spawn_managed", spawn)
    monkeypatch.setattr(process_control, "finish_scope", lambda _: None)
    assert installation.bootstrap_main("managed-launch", ["--smoke-task-b64", "do-not-replay"]) == 0
    assert len(calls) == 2 and runtime_descriptors() == ()
    assert not list((lab.store.root / "control/preparation").glob("session-*"))
    assert not list((lab.store.root / "control/preparation").glob("request-*"))
