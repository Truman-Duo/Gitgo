from __future__ import annotations

import json
import queue
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.recovery import (
    inspect_post_checkpoint_invocations,
    restore_incomplete_processes,
)
from backend.core.loop.session import AgentSession
from backend.core.loop.tools import ToolRegistry
from backend.core.daemon.dispatch import _cmd_task, _handle_command
from backend.core.loop.provider_adapters import (
    AnthropicMessagesAdapter,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
)
from backend.core.loop.provider_protocol import ProviderRequest
from backend.core.loop.outcome import OutcomeStatus, TaskOutcome


def _workspace(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir()
    return workspace


def _running_supervisor(workspace: Path):
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None,
        role="supervisor",
        tool_registry=ToolRegistry(
            CapabilityProfiles.resolve_tools("supervisor.control")
        ),
        max_steps=12,
        ring_level=RingLevel.RING_0,
        task_description="continue the durable task",
        task_id="task-recovery",
        workspace_path=str(workspace),
        actor_kind="supervisor",
        capability_profile_id="supervisor.control",
        task_kind="supervisor",
    )
    process.status = ProcessStatus.RUNNING
    process.session.append_user("inspect and safely continue")
    process.capability_lease = object()
    return manager, process


def test_safe_checkpoint_restores_only_as_explicit_candidate(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"
    _, original = _running_supervisor(workspace)
    original.session.append_assistant("durable partial answer")
    store = SessionStore(str(workspace), state_home=state_home)
    store.save_process_checkpoint(original)
    process_id = original.process_id
    session_id = original.session.session_id
    store.close()

    reopened = SessionStore(str(workspace), state_home=state_home)
    restored_manager = AgentProcessManager()
    candidates = restore_incomplete_processes(reopened, restored_manager, workspace)
    restored = restored_manager.get(process_id)

    assert [item["process_id"] for item in candidates] == [process_id]
    assert restored is not None
    assert restored.status is ProcessStatus.RESUME_AVAILABLE
    assert restored.capability_lease is None
    assert restored.session.session_id == session_id
    assert restored.session.messages[-1]["content"] == "durable partial answer"
    assert restored.recovery["self_execute_lease_revoked"] is True
    reopened.close()


def test_event_only_orphan_is_visible_but_cannot_be_resumed(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"
    store = SessionStore(str(workspace), state_home=state_home)
    store.append_event("event-only", "message_append", {
        "message": {"role": "user", "content": "durable orphan"},
    })
    store.close()

    reopened = SessionStore(str(workspace), state_home=state_home)
    manager = AgentProcessManager()
    candidates = restore_incomplete_processes(reopened, manager, workspace)
    process = manager.get("event-only")

    assert candidates[0]["process_id"] == "event-only"
    assert candidates[0]["resume_forbidden"] is True
    assert candidates[0]["reasons"][0]["code"] == "PROCESS_CHECKPOINT_MISSING"
    assert process.session.messages[-1]["content"] == "durable orphan"
    emitted = []
    _cmd_task(
        {"action": "resume", "process_id": "event-only"},
        None, None, {"apm": manager, "evq": queue.Queue()}, emitted.append,
    )
    assert emitted[-1]["error"] == (
        "RECOVERY_RESUME_FORBIDDEN:PROCESS_CHECKPOINT_MISSING"
    )
    reopened.close()


def test_post_checkpoint_effect_requires_manual_verification(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"
    _, original = _running_supervisor(workspace)
    store = SessionStore(str(workspace), state_home=state_home)
    store.save_process_checkpoint(original)
    process_id = original.process_id
    checkpoint_at = store.load_process_state(process_id)["session_checkpoint"]["checkpoint_at"]
    store.close()

    journal_dir = workspace / ".gitgo" / "tool_invocations"
    journal_dir.mkdir(parents=True)
    (journal_dir / "effect.json").write_text(json.dumps({
        "execution_id": "effect-after-checkpoint",
        "process_id": process_id,
        "task_id": "task-recovery",
        "tool_name": "file_edit",
        "effect": "workspace",
        "state": "committed",
        "effect_state": "committed",
        "updated_at": datetime.fromisoformat(checkpoint_at).timestamp() + 1,
    }), encoding="utf-8")
    assert inspect_post_checkpoint_invocations(
        workspace, process_id, checkpoint_at,
    )

    reopened = SessionStore(str(workspace), state_home=state_home)
    restored_manager = AgentProcessManager()
    candidates = restore_incomplete_processes(reopened, restored_manager, workspace)
    restored = restored_manager.get(process_id)

    assert candidates[0]["requires_manual_verification"] is True
    assert restored.status is ProcessStatus.RECOVERY_REVIEW_REQUIRED
    assert restored.recovery["reasons"][0]["code"] == (
        "EFFECT_COMMITTED_AFTER_CHECKPOINT"
    )
    emitted = []
    daemon_ctx = {
        "apm": restored_manager,
        "evq": queue.Queue(),
        "session_store": reopened,
    }
    _cmd_task(
        {"action": "resume", "process_id": process_id},
        None, None, daemon_ctx, emitted.append,
    )
    assert emitted[-1]["error"] == "RECOVERY_MANUAL_VERIFICATION_REQUIRED"
    assert restored.status is ProcessStatus.RECOVERY_REVIEW_REQUIRED

    emitted.clear()
    _cmd_task(
        {"action": "discard", "process_id": process_id, "reason": "verified obsolete"},
        None, None, daemon_ctx, emitted.append,
    )
    assert emitted[-1]["result"]["status"] == "discarded"
    assert restored.status is ProcessStatus.CANCELLED
    assert process_id not in reopened.list_incomplete()
    reopened.close()


def test_pending_user_decision_is_restored_without_model_replay(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"
    _, original = _running_supervisor(workspace)
    original.pending_decision = {
        "decision_id": "decision-recovery",
        "question": "Which long-term direction?",
        "options": [{"id": "a", "label": "A"}],
        "decision_sequence": 3,
        "context_epoch": 2,
    }
    original.session.context_epoch = 2
    original.session.host_ledger.append({
        "event": "user_decision_requested", **original.pending_decision,
        "task_id": original.active_task_id, "process_id": original.process_id,
        "created_at": "2026-01-01T00:00:00Z", "message_sequence": 1,
    })
    original.status = ProcessStatus.AWAITING_USER
    store = SessionStore(str(workspace), state_home=state_home)
    store.save_process_checkpoint(original)
    process_id = original.process_id
    store.close()

    reopened = SessionStore(str(workspace), state_home=state_home)
    restored_manager = AgentProcessManager()
    candidates = restore_incomplete_processes(reopened, restored_manager, workspace)
    restored = restored_manager.get(process_id)

    assert candidates[0]["status"] == "awaiting_user"
    assert restored.status is ProcessStatus.AWAITING_USER
    assert restored.pending_decision["decision_id"] == "decision-recovery"
    assert restored.session.context_epoch == 2
    from backend.core.loop.decision_timeline import decision_timeline
    assert decision_timeline(restored.session.host_ledger)[0]["status"] == "awaiting_user"
    reopened.close()


def test_command_handler_failure_returns_structured_error_without_killing_daemon(
    monkeypatch,
):
    from backend.core.daemon import dispatch

    emitted = []

    def explode(_cmd, _session, _project, _daemon_ctx, _emit):
        raise RuntimeError("one damaged projection")

    monkeypatch.setitem(dispatch.COMMAND_HANDLERS, "explode", explode)
    monkeypatch.setattr(dispatch, "_emit_global", emitted.append)

    _handle_command(
        {"cmd": "explode", "request_id": "request-1"},
        None, None, {},
    )

    assert emitted[-1]["event"] == "command_result"
    assert emitted[-1]["request_id"] == "request-1"
    assert emitted[-1]["error"] == "DAEMON_COMMAND_FAILED"
    assert emitted[-1]["error_info"]["catalog_id"] == "GITGO-E7201"


def test_missing_incomplete_checkpoint_object_becomes_discard_only_candidate(
    tmp_path_factory: Path,
):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"
    _, original = _running_supervisor(workspace)
    original.session.host_ledger.append({
        "event": "user_decision_requested",
        "decision_id": "lost-ledger-detail",
        "question": "A durable question",
    })
    store = SessionStore(str(workspace), state_home=state_home)
    store.save_process_checkpoint(original)
    with store._storage._state_lock:
        metadata_ref = str(store._storage._state.execute(
            "SELECT metadata_ref FROM sessions WHERE session_id=?",
            (original.session.session_id,),
        ).fetchone()[0])
    metadata = store._storage._read_json_blob(metadata_ref)
    ledger_ref = str(metadata["host_ledger_ref"])
    digest = ledger_ref.removeprefix("sha256:")
    (store.storage_paths.cas_dir / digest[:2] / digest[2:]).unlink()
    process_id = original.process_id
    store.close()

    reopened = SessionStore(str(workspace), state_home=state_home)
    manager = AgentProcessManager()
    candidates = restore_incomplete_processes(reopened, manager, workspace)
    restored = manager.get(process_id)

    assert restored is not None
    assert restored.status is ProcessStatus.RECOVERY_REVIEW_REQUIRED
    assert restored.recovery["resume_forbidden"] is True
    assert restored.recovery["reasons"][0]["code"] == (
        "STORAGE_CAS_REFERENCE_MISSING"
    )
    assert candidates[0]["process_id"] == process_id
    reopened.close()


def test_recovery_rehydrates_governance_signal_wire_records(tmp_path_factory: Path):
    from backend.core.loop.signals import (
        GovernanceSignal, SignalCategory, SignalSeverity,
    )

    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"
    _, original = _running_supervisor(workspace)
    original.context_snapshot = {
        "signals": [GovernanceSignal(
            signal_id="durable-signal",
            source="lesson_trigger",
            severity=SignalSeverity.HIGH,
            category=SignalCategory.WARN,
            rule="Keep the durable boundary typed.",
        )],
    }
    original.status = ProcessStatus.AWAITING_USER
    original.pending_decision = {
        "decision_id": "typed-resume",
        "question": "Continue?",
        "options": [{"action": "continue", "label": "Continue"}],
    }
    store = SessionStore(str(workspace), state_home=state_home)
    store.save_process_checkpoint(original)
    process_id = original.process_id
    store.close()

    reopened = SessionStore(str(workspace), state_home=state_home)
    restored_manager = AgentProcessManager()
    restore_incomplete_processes(reopened, restored_manager, workspace)
    restored = restored_manager.get(process_id)

    signal = restored.context_snapshot["signals"][0]
    assert isinstance(signal, GovernanceSignal)
    assert signal.source == "lesson_trigger"
    assert signal.category is SignalCategory.WARN
    reopened.close()


def test_terminal_checkpoint_repairs_only_missing_completion_barrier(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"
    _, original = _running_supervisor(workspace)
    original.status = ProcessStatus.COMPLETED
    store = SessionStore(str(workspace), state_home=state_home)
    store.save_process_checkpoint(original)
    process_id = original.process_id
    assert process_id in store.list_incomplete()
    store.close()

    reopened = SessionStore(str(workspace), state_home=state_home)
    candidates = restore_incomplete_processes(
        reopened, AgentProcessManager(), workspace,
    )
    assert candidates == []
    assert process_id not in reopened.list_incomplete()
    reopened.close()


def test_restore_rebuilds_connected_supervisor_worker_tree(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"
    manager, parent = _running_supervisor(workspace)
    child = manager.fork(
        parent_id=parent.process_id,
        role="worker",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("development.workspace")),
        max_steps=8,
        ring_level=RingLevel.RING_3,
        task_description="implement bounded child work",
        task_id="task-recovery:1",
        workspace_path=str(workspace),
        actor_kind="worker",
        capability_profile_id="development.workspace",
        task_kind="action",
    )
    # Simulate a crash after the child row was admitted but before the parent
    # checkpoint captured its child_ids/delegated_contracts edge.
    child.status = ProcessStatus.COMPLETED
    child.result = {
        "task_id": child.active_task_id,
        "process_id": child.process_id,
        "status": "completed",
        "process_status": "completed",
        "response": "child done",
    }
    store = SessionStore(str(workspace), state_home=state_home)
    store.save_process_checkpoint(parent)
    store.save_process_checkpoint(child)
    store.append_event(child.process_id, "agent_complete", {"status": "completed"})
    parent_id = parent.process_id
    child_id = child.process_id
    store.close()

    reopened = SessionStore(str(workspace), state_home=state_home)
    restored_manager = AgentProcessManager()
    candidates = restore_incomplete_processes(reopened, restored_manager, workspace)
    restored_parent = restored_manager.get(parent_id)
    restored_child = restored_manager.get(child_id)

    assert [item["process_id"] for item in candidates] == [parent_id]
    assert restored_parent.status is ProcessStatus.RESUME_AVAILABLE
    assert restored_child.status is ProcessStatus.COMPLETED
    assert child_id in restored_parent.child_ids
    assert restored_parent.delegated_contracts[child_id]["task_id"] == (
        "task-recovery:1"
    )
    assert restored_child.parent_id == parent_id
    assert restored_parent.task_budget is restored_child.task_budget
    assert restored_child.result["response"] == "child done"
    reopened.close()


def test_dangling_provider_tool_call_is_sealed_once_as_unknown():
    session = AgentSession()
    session.append_assistant_provider("", tool_calls=[{
        "id": "call-recovery",
        "type": "function",
        "function": {"name": "file_edit", "arguments": "{}"},
    }])

    assert session.seal_dangling_tool_calls(
        recovery_code="RECOVERY_RESULT_UNAVAILABLE",
    ) == ["call-recovery"]
    assert session.seal_dangling_tool_calls(
        recovery_code="RECOVERY_RESULT_UNAVAILABLE",
    ) == []
    result = session.messages[-1]
    assert result["role"] == "tool"
    assert result["tool_call_id"] == "call-recovery"
    assert result["data"]["execution_state"] == "unknown"
    assert result["receipt"]["effect_state"] == "ambiguous"


def test_recovery_seals_dangling_calls_for_all_native_provider_protocols():
    cases = [
        (
            OpenAIChatAdapter(),
            {"tool_calls": [{
                "id": "chat-call", "type": "function",
                "function": {"name": "scan", "arguments": "{}"},
            }]},
            "chat-call",
        ),
        (
            OpenAIResponsesAdapter(),
            {"response_output_items": [{
                "type": "function_call", "call_id": "responses-call",
                "name": "scan", "arguments": "{}",
            }]},
            "responses-call",
        ),
        (
            AnthropicMessagesAdapter(),
            {"anthropic_content_blocks": [{
                "type": "tool_use", "id": "anthropic-call",
                "name": "scan", "input": {},
            }]},
            "anthropic-call",
        ),
    ]
    for adapter, continuation, call_id in cases:
        session = AgentSession()
        session.append_assistant_provider("", continuation_state=continuation)
        assert session.seal_dangling_tool_calls(
            recovery_code="RECOVERY_RESULT_UNAVAILABLE",
        ) == [call_id]
        request = ProviderRequest(messages=tuple(session.to_provider_messages()))
        body = adapter.build_body(request, "test-model")
        encoded = json.dumps(body, ensure_ascii=False)
        assert call_id in encoded
        assert "Execution state is unknown" in encoded


def test_explicit_safe_resume_continues_same_process_and_writes_terminal_barrier(
    tmp_path_factory: Path, monkeypatch,
):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"
    _, original = _running_supervisor(workspace)
    store = SessionStore(str(workspace), state_home=state_home)
    store.save_process_checkpoint(original)
    process_id = original.process_id
    store.close()

    reopened = SessionStore(str(workspace), state_home=state_home)
    restored_manager = AgentProcessManager()
    restore_incomplete_processes(reopened, restored_manager, workspace)
    restored = restored_manager.get(process_id)
    restored.task_budget = None
    events = queue.Queue()
    emitted = []

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    def complete_recovered(process, *_args, **_kwargs):
        process.status = ProcessStatus.COMPLETED
        outcome = TaskOutcome(
            task_id=process.active_task_id,
            process_id=process.process_id,
            status=OutcomeStatus.COMPLETED,
            process_status="completed",
            response="continued safely",
            steps_used=1,
            llm_used=True,
        ).to_dict()
        process.result = outcome
        return outcome

    monkeypatch.setattr("backend.core.daemon.dispatch.threading.Thread", ImmediateThread)
    monkeypatch.setattr("backend.core.loop.executor.agent_step", complete_recovered)
    _cmd_task(
        {
            "action": "resume",
            "process_id": process_id,
            "task_id": restored.active_task_id,
            "context_snapshot": {},
        },
        SimpleNamespace(workspace_path=workspace),
        SimpleNamespace(name="test-project"),
        {
            "apm": restored_manager,
            "dispatcher": SimpleNamespace(_executors={}),
            "evq": events,
            "llm": object(),
            "session_store": reopened,
            "recovery_available": [process_id],
            "recovery_candidates": [{"process_id": process_id}],
        },
        emitted.append,
    )

    assert emitted[-1]["result"]["process_id"] == process_id
    assert restored.status is ProcessStatus.COMPLETED
    assert restored.result["response"] == "continued safely"
    assert process_id not in reopened.list_incomplete()
    assert any(
        message.get("steering_type") == "daemon_recovery"
        for message in restored.session.messages
    )
    reopened.close()


def test_real_daemon_process_restart_keeps_candidate_explicit(
    tmp_path_factory: Path, monkeypatch,
):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    monkeypatch.setattr(
        "backend.core.history.HistoryManager.add_operation",
        lambda *_args, **_kwargs: None,
    )
    _, original = _running_supervisor(workspace)
    store = SessionStore(str(workspace), state_home=state_home)
    store.save_process_checkpoint(original)
    process_id = original.process_id
    store.close()

    repo_root = Path(__file__).resolve().parents[1]
    bootstrap = (
        "from backend.core.config import Config, ProjectConfig; "
        "from backend.core.daemon import run_daemon; "
        f"p=ProjectConfig(name='recovery-process-e2e'); p.workspace_path={str(workspace)!r}; "
        "run_daemon(Config(projects=[p]), p, trial_interval=9999, debounce_sec=0.1)"
    )
    env = {
        **os.environ,
        "GITGO_STATE_HOME": str(state_home),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
    }

    def run_instance(commands: list[dict]) -> list[dict]:
        completed = subprocess.run(
            [sys.executable, "-c", bootstrap],
            cwd=repo_root,
            env=env,
            input="".join(json.dumps(command) + "\n" for command in commands),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        return [
            json.loads(line) for line in completed.stdout.splitlines()
            if line.strip().startswith("{")
        ]

    first = run_instance([
        {"cmd": "loop_status", "request_id": "status-first"},
        {"cmd": "shutdown", "request_id": "shutdown-first"},
    ])
    first_status = next(
        event["result"] for event in first
        if event.get("event") == "command_result"
        and event.get("request_id") == "status-first"
    )
    assert first_status["recovery_available"] == [process_id]

    second = run_instance([
        {"cmd": "loop_status", "request_id": "status-second"},
        {
            "cmd": "task", "action": "discard", "process_id": process_id,
            "reason": "cross-process acceptance complete", "request_id": "discard-second",
        },
        {"cmd": "shutdown", "request_id": "shutdown-second"},
    ])
    second_status = next(
        event["result"] for event in second
        if event.get("event") == "command_result"
        and event.get("request_id") == "status-second"
    )
    assert second_status["recovery_available"] == [process_id]
    assert any(
        event.get("event") == "command_result"
        and event.get("request_id") == "discard-second"
        and event.get("result", {}).get("status") == "discarded"
        for event in second
    )

    final_store = SessionStore(str(workspace), state_home=state_home)
    assert process_id not in final_store.list_incomplete()
    final_store.close()
