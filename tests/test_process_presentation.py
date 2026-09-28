from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from backend.core.application.process_presentation import ProcessPresentation
from backend.core.daemon.dispatch import _process_status_payload
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import RingLevel
from backend.core.loop.tools import ToolRegistry
from backend.core.storage import StorageRuntime


def test_names_and_archive_survive_checkpoints_and_restart_without_changing_authority(tmp_path_factory):
    ws = tmp_path_factory / "ws"
    ws.mkdir()
    with StorageRuntime(ws) as storage:
        presentation = ProcessPresentation(storage)
        manager = AgentProcessManager(presentation=presentation)
        store = SessionStore(str(ws), storage=storage)
        root = manager.fork(None, "supervisor", ToolRegistry([]), 10, RingLevel.RING_0,
                            actor_kind="supervisor", task_id="root-task")
        root.pending_decision = {
            "decision_id": "decision-cold-read", "question": "Choose",
            "options": [{"label": "A"}, {"label": "B"}],
        }
        store.save_process_checkpoint(root)
        durable_root = storage.read_latest_root_process()
        assert durable_root["process_id"] == root.process_id
        assert durable_root["actor_kind"] == "supervisor"
        assert durable_root["pending_decision"]["decision_id"] == "decision-cold-read"
        child = manager.fork(root.process_id, "worker", ToolRegistry([]), 10, RingLevel.RING_3,
                             task_id="child-task")
        store.save_process_checkpoint(child)
        original_name = child.session.display_name
        assert storage.read_project_b_processes()[child.process_id]["display_name"] == original_name
        presentation.rename(child.process_id, "HTML 作者")
        presentation.archive(child.process_id)
        before = storage._state.total_changes
        presentation.archive(child.process_id)
        assert storage._state.total_changes == before  # No write churn for an unchanged choice.
        store.save_process_checkpoint(child)
        assert child.session.display_name == original_name  # Execution checkpoint is not the presentation authority.
        assert _process_status_payload(child)["display_name"] == "HTML 作者"
        assert child.parent_id == root.process_id
        assert _process_status_payload(child)["archived"] is True
        with pytest.raises(ValueError, match="PROCESS_NOT_B"):
            presentation.archive(root.process_id)
        with pytest.raises(ValueError, match="PROCESS_NOT_FOUND"):
            presentation.rename("missing", "name")
        with pytest.raises(ValueError, match="control"):
            presentation.rename(child.process_id, "bad\x1b[31m")
    with StorageRuntime(ws) as storage:
        row = storage.read_project_b_processes()[child.process_id]
        assert row["display_name"] == "HTML 作者" and row["archived"]
        assert row["parent_id"] == root.process_id and row["status"] == "waiting"
        result = ProcessPresentation(storage).archive(child.process_id, False)
        assert result["execution_unchanged"] and not result["data_deleted"]
        assert not storage.read_project_b_processes()[child.process_id]["archived"]


def test_concurrent_hosts_update_independent_fields_without_lost_updates(tmp_path_factory):
    ws = tmp_path_factory / "ws"
    ws.mkdir()
    with StorageRuntime(ws) as first, StorageRuntime(ws) as second:
        first.save_agent_checkpoint({"process_id": "b", "session_id": "s", "task_id": "t",
            "actor_kind": "worker", "status": "completed", "messages": []})
        with ThreadPoolExecutor(max_workers=2) as pool:
            rename = pool.submit(ProcessPresentation(first).rename, "b", "worker name")
            archive = pool.submit(ProcessPresentation(second).archive, "b")
            rename.result()
            archive.result()
        assert first.read_process_presentation("b") == {"display_name": "worker name", "archived": True}
