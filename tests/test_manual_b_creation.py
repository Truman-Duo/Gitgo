import io
import threading
from types import SimpleNamespace

import pytest

from backend.core.application import OperationError
from backend.core.native_host import NativeHost
from backend.core.loop.task_contract import publish_contract


def test_manual_requirement_cannot_be_erased_by_llm_contract():
    requirement = {"manual_B_creation": True, "excluded_process_ids": ["old-B"]}
    process = SimpleNamespace(_context_lock=threading.RLock(), context_version=1,
                              context_snapshot={"task_contract": {"host_requirements": requirement}})
    result = publish_contract(process, {"execution_mode": "delegate", "minimum_delegated_outcomes": 0,
                                        "delegation_required": False, "host_requirements": {}})
    assert result["minimum_delegated_outcomes"] == 1
    assert result["delegation_required"]
    assert result["host_requirements"] == requirement
    with pytest.raises(ValueError, match="explicitly requested"):
        publish_contract(process, {"execution_mode": "answer"})


def test_root_busy_reservation_precedes_daemon_ack(monkeypatch):
    host = NativeHost(stdout=io.StringIO())
    entered, release = threading.Event(), threading.Event()
    def run(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        return {"status": "completed"}
    monkeypatch.setattr(host, "_runtime_chat_admitted", run)
    thread = threading.Thread(target=lambda: host._runtime_chat("one", {"project": "p", "message": "hello"}))
    thread.start()
    assert entered.wait(1)
    try:
        with pytest.raises(OperationError) as exc:
            host._runtime_chat("two", {"project": "p", "message": "new B", "manual_delegation": True})
        assert exc.value.code == "SUPERVISOR_BUSY"
        assert exc.value.details["catalog_id"] == "GITGO-E3107"
    finally:
        release.set()
        thread.join(2)
        host.close()
    assert not host._root_admissions


def test_bound_decision_bypasses_busy_root_without_releasing_its_lease(monkeypatch):
    host = NativeHost(stdout=io.StringIO())
    entered, release = threading.Event(), threading.Event()

    def run(_request_id, _arguments, *, action="chat", **_timing):
        if action == "decision":
            return {"status": "resumed"}
        entered.set()
        assert release.wait(2)
        return {"status": "completed"}

    monkeypatch.setattr(host, "_runtime_chat_admitted", run)
    thread = threading.Thread(
        target=lambda: host._runtime_chat(
            "root", {"project": "p", "message": "long task"},
        )
    )
    thread.start()
    assert entered.wait(1)
    try:
        result = host._runtime_chat(
            "decision",
            {
                "project": "p", "message": "Choose option 1: Allow once",
                "process_id": "b-1", "decision_id": "decision-1",
            },
            action="decision",
        )
        assert result == {"status": "resumed"}
        assert "p" in host._root_admissions
    finally:
        release.set()
        thread.join(2)
        host.close()
    assert not host._root_admissions
