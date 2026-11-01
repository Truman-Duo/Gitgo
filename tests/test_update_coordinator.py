"""Admitted-engine dispatch uses real files/leases, simulated worker results."""
import pytest

from backend.core.update import coordinator as engine
from backend.core.update import windows_storage as win
from backend.core.update.handoff import launch_session
from backend.core.update.persistence import atomic_control, read_control, private_namespace
from backend.core.update.transaction import TransactionResult
from backend.core.update.validation import UpdateError
from tests.test_update_windows_managed import managed
from tests.test_update_manifest import signing

pytestmark = pytest.mark.skipif(not win.supported(), reason="Native Windows admission required")


def test_ordinary_quit_without_prepared_update_does_not_copy_an_engine(managed, monkeypatch):
    store = managed[0]
    seen = []
    def invoke(store, program, args):
        seen.append((program, args)); return TransactionResult("idle")
    monkeypatch.setattr(engine, "_invoke", invoke)
    with launch_session(store) as nonce:
        assert engine.dispatch(store, nonce, 0, {"complete": True}, automatic=True).status == "idle"
    assert seen[0][0].is_relative_to(store.root / "active") and seen[0][1] == ["--propose", "--nonce", nonce]
    assert not (store.root / "control/engines").exists()


def test_worker_is_exact_admitted_copy_and_disposable_after_completion(managed, monkeypatch):
    store = managed[0]
    def invoke(store, program, args):
        value, cache = engine._read(store)
        assert program.is_relative_to(cache) and not args
        assert value["tree_sha256"] == store._ledger()["current"]["tree_sha256"]
        return TransactionResult("blocked", error_code="UPDATE_SELECTION_STALE")
    monkeypatch.setattr(engine, "_invoke", invoke)
    with launch_session(store) as nonce:
        assert engine.dispatch(store, nonce, 75, {"complete": True}).status == "blocked"
    assert not (store.root / "control" / engine.TICKET).exists()
    assert not list((store.root / "control/engines").iterdir())


def test_interrupted_engine_uses_same_retained_generation_for_recovery_only(managed, monkeypatch):
    store = managed[0]; calls = []
    def invoke(store, program, args):
        calls.append((program, args))
        return TransactionResult("busy" if len(calls) == 1 else "rolled_back")
    monkeypatch.setattr(engine, "_invoke", invoke)
    with launch_session(store) as nonce:
        assert engine.dispatch(store, nonce, 75, {"complete": True}).status == "busy"
    assert (store.root / "control" / engine.TICKET).exists()
    assert engine.recover_if_pending(store).status == "rolled_back"
    assert calls[0][0] == calls[1][0] and calls[1][1] == ["--recover"]
    assert not (store.root / "control" / engine.TICKET).exists()


@pytest.mark.parametrize("fault", ["unadmitted", "changed-code"])
def test_retained_engine_cannot_substitute_unadmitted_code(managed, monkeypatch, fault):
    store = managed[0]
    monkeypatch.setattr(engine, "_invoke", lambda *_: TransactionResult("busy"))
    with launch_session(store) as nonce:
        engine.dispatch(store, nonce, 75, {"complete": True})
    path = store.root / "control" / engine.TICKET; value = read_control(path)
    if fault == "unadmitted":
        value.update(tree_sha256="9" * 64, engine="engines/" + "9" * 64)
        atomic_control(path, value)
    else:
        (store.root / "control" / value["engine"] / "gitgo.exe").write_bytes(b"changed")
    with pytest.raises(UpdateError, match="UPDATE_ENGINE_INVALID"): engine.recover_if_pending(store)
    assert path.exists()


def test_incomplete_cleanup_never_starts_immediate_engine(managed, monkeypatch):
    store = managed[0]
    monkeypatch.setattr(engine, "_invoke", lambda *_: pytest.fail("Worker must not run"))
    with pytest.raises(UpdateError, match="UPDATE_SCOPE_UNAVAILABLE"):
        engine.dispatch(store, "a" * 32, 75, {"complete": False})


def test_mac_deferred_route_uses_current_engine_and_preserves_cleanup_fact(managed, monkeypatch):
    store = managed[0]
    from backend.core.update import handoff, deferred
    monkeypatch.setattr(handoff, "strategy", lambda _: "after_reboot")
    monkeypatch.setattr(deferred, "pending_status", lambda _: {"version": "1.0.0-alpha.3"})
    def invoke(store, program, args):
        value, _ = engine._read(store)
        assert value["deferred"] and value["cleanup_complete"] is False
        return TransactionResult("restart_required")
    monkeypatch.setattr(engine, "_invoke", invoke)
    assert engine.dispatch(store, "a" * 32, 0, {"complete": False}, deferred=True).status == "restart_required"

