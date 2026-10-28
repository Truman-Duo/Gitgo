"""Exact historical task navigation uses existing public projection, without a writer."""
import pytest

from backend.core.storage import StorageRuntime
from backend.core.storage.runtime import read_existing_task_view


def checkpoint(storage, process, session, task, response, *, parent="", parent_task=""):
    storage.save_agent_checkpoint({"process_id": process, "session_id": session, "task_id": task,
        "parent_id": parent, "parent_task_id": parent_task,
        "actor_kind": "worker" if parent else "supervisor", "role": "worker" if parent else "main",
        "status": "completed", "messages": [
            {"role": "user", "content": "PUBLIC " + task, "message_type": "conversation"},
            {"role": "user", "content": "[HOST PRIVATE CONTROL]", "message_type": "control"},
            {"role": "assistant", "content": "INTERMEDIATE PROVIDER TEXT"}],
        "result": {"status": "completed", "response": response, "process_id": process}})


def test_exact_old_root_and_child_remain_readable_after_a_new_conversation(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory / "workspace"
    ws.mkdir()
    with StorageRuntime(ws) as storage:
        checkpoint(storage, "old-main", "old-session", "old-task", "OLD ROOT RESULT")
        checkpoint(storage, "child", "child-session", "child-task", "CHILD RESULT", parent="old-main", parent_task="old-task")
        checkpoint(storage, "new-main", "new-session", "new-task", "NEW ROOT RESULT")
        before = storage._state.total_changes
        old = read_existing_task_view(ws, "old-main", "old-task")
        assert old["process"]["active_task_id"] == "old-task"
        assert any(row["content"] == "OLD ROOT RESULT" for row in old["messages"])
        text = str(old["messages"])
        assert "NEW ROOT RESULT" not in text and "PRIVATE CONTROL" not in text and "INTERMEDIATE PROVIDER" not in text
        child = read_existing_task_view(ws, "child", "child-task")
        assert child["parent_task_id"] == "old-task"
        assert child["process"]["parent_id"] == "old-main"
        assert storage._state.total_changes == before
        assert read_existing_task_view(ws, "old-main", "new-task") is None
    def forbidden_writer(*args, **kwargs):
        pytest.fail("read-only task navigation constructed a writer")
    monkeypatch.setattr(StorageRuntime, "__init__", forbidden_writer)
    assert read_existing_task_view(ws, "old-main", "old-task")["messages"] == old["messages"]
    empty = tmp_path_factory / "empty-workspace"
    empty.mkdir()
    assert read_existing_task_view(empty, "old-main", "old-task") is None


def test_a_reused_main_process_can_read_its_earlier_task_without_switching_current_task(tmp_path_factory):
    ws = tmp_path_factory / "workspace"
    ws.mkdir()
    with StorageRuntime(ws) as storage:
        checkpoint(storage, "main", "session", "first", "FIRST RESULT")
        checkpoint(storage, "main", "session", "second", "SECOND RESULT")
        result = read_existing_task_view(ws, "main", "first")
        assert result["process"]["active_task_id"] == "first"
        assert all(row["turn_id"] == "first" for row in result["messages"])
        assert "FIRST RESULT" in str(result["messages"])
        assert "SECOND RESULT" not in str(result["messages"])
        assert storage.load_agent_process_state("main")["task"]["task_id"] == "second"
