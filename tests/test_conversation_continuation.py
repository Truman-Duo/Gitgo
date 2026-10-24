from types import SimpleNamespace
import pytest
from backend.core.loop.conversation_continuation import resolve_conversation_process
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.tool_result_support import _apply_host_task_transitions


def test_cold_terminal_child_restores_history_session_and_parent_without_approval(tmp_path_factory):
    workspace = tmp_path_factory / "project"; workspace.mkdir()
    store = SessionStore(str(workspace), state_home=tmp_path_factory / "state")
    manager = AgentProcessManager()
    root = manager.fork(parent_id=None, role="supervisor", tool_registry=ToolRegistry([]), max_steps=8, ring_level=RingLevel.RING_3,
        workspace_path=str(workspace), actor_kind="supervisor", capability_profile_id="supervisor.control")
    child = manager.fork(parent_id=root.process_id, role="worker", tool_registry=ToolRegistry([]), max_steps=8, ring_level=RingLevel.RING_3,
        workspace_path=str(workspace), actor_kind="worker", capability_profile_id="text.only", task_kind="action")
    child.session.append_user("Explain the previously completed boundary")
    child.session.append_assistant("I verified the result against the recorded contract")
    for process in (root, child):
        process.status = ProcessStatus.COMPLETED; process.lifecycle_finalized = True
        store.save_process_checkpoint(process)
        store.append_event(process.process_id, "agent_complete", {"status": "completed"})
    reopened = SessionStore(str(workspace), storage=store._storage)
    cold = AgentProcessManager()
    restored = resolve_conversation_process(cold, reopened, workspace, child.process_id)
    assert restored.session.session_id == child.session.session_id
    assert restored.session.messages[-1]["content"] == child.session.messages[-1]["content"]
    assert restored.parent_id == root.process_id and cold.get(root.process_id)
    assert restored.capability_lease is None and restored.approval_grants == []
    reopened.close(); store.close()


def test_missing_identity_never_creates_new_conversation():
    manager = AgentProcessManager()
    with pytest.raises(ValueError, match="CONTINUATION_STATE_UNAVAILABLE"):
        resolve_conversation_process(manager, None, ".", "missing")
    assert manager.list_processes() == []


def test_interrupted_conversation_keeps_explicit_recovery_requirement():
    manager = AgentProcessManager()
    process = manager.fork(parent_id=None, role="worker", tool_registry=ToolRegistry([]), max_steps=8, ring_level=RingLevel.RING_3)
    process.status = ProcessStatus.RECOVERY_REVIEW_REQUIRED
    with pytest.raises(ValueError, match="CONTINUATION_RECOVERY_REQUIRED"):
        resolve_conversation_process(manager, None, ".", process.process_id)


def test_direct_worker_promotes_only_after_committed_mutation():
    process = SimpleNamespace(actor_kind="worker", task_kind="answer",
        runtime_preferences={"user_direct_continuation": True})
    read = SimpleNamespace(tool_name="read_file", is_error=False, receipt={"effect": "read"})
    assert not _apply_host_task_transitions(process, [read])
    write = SimpleNamespace(tool_name="write_file", is_error=False, receipt={"effect": "workspace_write"})
    assert _apply_host_task_transitions(process, [write])
    assert process.task_kind == "action"

def test_continuation_scope_never_expands_a_bounded_worker():
    from backend.core.loop.conversation_continuation import bounded_continuation_tools
    assert bounded_continuation_tools(["read_file", "write_file"], issued_tools=["read_file"]) == ["read_file"]
    assert bounded_continuation_tools(["read_file", "write_file"], contract={"allowed_tools": ["read_file"]}) == ["read_file"]
    assert bounded_continuation_tools(["read_file"], issued_tools=["read_file", "old_dynamic_grant"]) == ["read_file"]
    assert bounded_continuation_tools(["read_file"], issued_tools=[]) == []

def test_cold_checkpoint_preserves_the_issued_scope(tmp_path_factory):
    workspace = tmp_path_factory / "scoped"; workspace.mkdir()
    store = SessionStore(str(workspace), state_home=tmp_path_factory / "scoped-state")
    manager = AgentProcessManager()
    process = manager.fork(parent_id=None, role="worker", actor_kind="worker",
        capability_profile_id="development.workspace", tool_registry=ToolRegistry(["read_file"]),
        max_steps=6, ring_level=RingLevel.RING_3, workspace_path=str(workspace))
    process.status=ProcessStatus.COMPLETED; process.lifecycle_finalized=True
    try:
        store.save_process_checkpoint(process)
        store.append_event(process.process_id, "agent_complete", {"status":"completed"})
        cold=AgentProcessManager()
        restored=resolve_conversation_process(cold,store,workspace,process.process_id)
        assert restored.tool_registry.list_all()==["read_file"]
    finally:
        store.close()
