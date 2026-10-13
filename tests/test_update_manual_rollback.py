"""Explicit rollback wiring and durable recovery. Never execute placeholders."""
import pytest
from backend.core.update import handoff, health, UpdateError
from backend.core.update.installation import open_managed
from backend.core.update.persistence import read_control, lock_file
from backend.core.update.tree import tree_digest
from tests.test_update_handoff import lab, queue, consume, signing
from tests.test_update_transaction import Crash, inspect_health


def upgraded(lab, monkeypatch):
    with handoff.launch_session(lab.store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce); queue(lab)
        assert consume(lab, nonce).status == "committed"
    return lab.store.rollback_selection()


def test_explicit_offline_rollback_preserves_high_water_and_original_data(lab, monkeypatch):
    selected = upgraded(lab, monkeypatch)
    before = lab.store._ledger()["watermarks"]; calls = list(lab.calls)
    with handoff.launch_session(lab.store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        status = lab.service.status("preview")
        assert status["schema_version"] == 4 and status["can_request_rollback"] is True
        assert status["rollback"]["version"] == "1.0.0-alpha.2"
        response = lab.service.request_rollback(selected["rollback_id"], "d" * 32)
        assert response["exit_code"] == 75 and response["action"] == "rollback"
        assert lab.store._ledger()["current"]["version"] == "1.0.0-alpha.3"
        assert consume(lab, nonce, code=0) is None
        result = consume(lab, nonce)
        assert result.status == "rolled_back" and result.error_code is None
    assert tree_digest(lab.store.root / "active") == lab.before
    assert lab.store._ledger()["watermarks"] == before and lab.calls == calls
    assert open_managed(lab.store.root).recover().status == "rolled_back"
    assert lab.store.rollback_selection() is None  # Cannot toggle/replay arbitrary history.


@pytest.mark.parametrize("point", ["user_rollback_health_passed", "user_rollback_intent",
    "rollback_intent", "failed_renamed", "old_restored", "rollback_written"])
def test_crashed_user_rollback_recovers_from_the_same_ledger(lab, monkeypatch, point):
    selected = upgraded(lab, monkeypatch)
    watermarks = lab.store._ledger()["watermarks"]
    def crash(name):
        if name == point: raise Crash()
    with pytest.raises(Crash):
        lab.store._rollback(selected["rollback_id"], health_check=inspect_health, checkpoint=crash)
    fresh = open_managed(lab.store.root)
    result = fresh.recover()
    assert result.status == ("committed" if point == "user_rollback_health_passed" else "rolled_back")
    assert fresh._ledger()["watermarks"] == watermarks
    assert fresh.recover().status == result.status
    if result.status == "rolled_back": assert tree_digest(fresh.root / "active") == lab.before


def test_failed_predecessor_probe_keeps_running_new_version(lab, monkeypatch):
    selected = upgraded(lab, monkeypatch); before = lab.store._ledger()
    def fail(*_): raise UpdateError("UPDATE_HEALTH_FAILED", "test failure")
    assert lab.store._rollback(selected["rollback_id"], health_check=fail).status == "failed"
    assert lab.store._ledger() == before
    assert lab.store.recover().status == "committed"


def test_changed_retained_program_or_stale_id_never_downgrades(lab, monkeypatch):
    selected = upgraded(lab, monkeypatch)
    with pytest.raises(UpdateError, match="UPDATE_SELECTION_STALE"):
        lab.store._rollback("f" * 64, health_check=inspect_health)
    txn = lab.store._ledger()["transaction"]
    old = lab.store.root / "transactions" / txn["id"] / "old" / "gitgo"
    old.chmod(0o600); old.write_bytes(b"changed")
    with pytest.raises(UpdateError, match="UPDATE_RECOVERY_REQUIRED"):
        lab.store._rollback(selected["rollback_id"], health_check=inspect_health)
    assert lab.store._ledger()["current"]["version"] == "1.0.0-alpha.3"


def test_cancelled_or_busy_rollback_does_not_publish_or_switch(lab, monkeypatch):
    selected = upgraded(lab, monkeypatch)
    with handoff.launch_session(lab.store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        lab.service.cancel("d" * 32)
        with pytest.raises(UpdateError, match="UPDATE_CANCELLED"):
            lab.service.request_rollback(selected["rollback_id"], "d" * 32)
        assert not handoff._path(lab.store, nonce, "request").exists()
        lab.service.request_rollback(selected["rollback_id"], "e" * 32)
        with lab.store.runtime_lease():
            assert consume(lab, nonce).error_code == "UPDATE_BUSY"
        assert lab.store._ledger()["current"]["version"] == "1.0.0-alpha.3"


@pytest.mark.parametrize("after_write", [False, True])
def test_rollback_intent_write_error_uses_durable_fact_not_assumed_success(lab, monkeypatch, after_write):
    selected = upgraded(lab, monkeypatch)
    save = lab.store._save
    once = False
    def fail(ledger, state=None, **kwargs):
        nonlocal once
        if state == "ROLLBACK" and not once:
            once = True
            if after_write: save(ledger, state, **kwargs)
            raise OSError("test fsync failure")
        return save(ledger, state, **kwargs)
    monkeypatch.setattr(lab.store, "_save", fail)
    result = lab.store._rollback(selected["rollback_id"], health_check=inspect_health)
    assert result.status == ("rolled_back" if after_write else "failed")
    assert open_managed(lab.store.root).recover().status == ("rolled_back" if after_write else "committed")


def test_failed_restore_retains_both_programs_for_fresh_recovery(lab, monkeypatch):
    selected = upgraded(lab, monkeypatch)
    move = lab.store._move
    def fail(source, target, expected=None):
        if source.name == "old": raise OSError("test disk error")
        return move(source, target, expected)
    monkeypatch.setattr(lab.store, "_move", fail)
    assert lab.store._rollback(selected["rollback_id"], health_check=inspect_health).status == "recovery_required"
    fresh = open_managed(lab.store.root)
    assert fresh.recover().status == "rolled_back"
    assert tree_digest(fresh.root / "active") == lab.before
