from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.application import ApplicationServices, OperationError
from backend.core.config import Config, ConfigManager, ProjectConfig
from backend.models import FileAccess, RepoNode
from backend.core.storage import get_storage, StorageRuntime
from backend.core.storage.bindings import release_storage
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import RingLevel, ProcessStatus
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.recovery import restore_incomplete_processes


def setup_project(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory / "workspace"
    ws.mkdir()
    (ws / "source.txt").write_text("keep")
    config = tmp_path_factory / "config.json"
    monkeypatch.setattr(ConfigManager, "default_path", staticmethod(lambda: config))
    cfg = Config(projects=[ProjectConfig(name="test", workspace=RepoNode(
        file_access=FileAccess(path=str(ws))))], safety={"delete_delay_minutes": 0, "process_delete_delay_minutes": 0})
    ConfigManager.save(cfg)
    return ws, ApplicationServices()


def test_legacy_due_delete_is_read_only_and_old_write_requires_confirmation(tmp_path_factory, monkeypatch):
    ws, app = setup_project(tmp_path_factory, monkeypatch)
    cfg = ConfigManager.load()
    cfg.projects[0].pending_hard_delete_at = "2000-01-01T00:00:00"
    cfg.projects[0].archived = True
    ConfigManager.save(cfg)
    before = ConfigManager.default_path().read_bytes()
    assert app.project_list() == []
    assert app.project_list_archived()[0]["name"] == "test"
    assert ws.exists() and ConfigManager.default_path().read_bytes() == before
    with pytest.raises(OperationError, match="preview"):
        app.project_delete("test", "hard")


def test_project_create_attaches_arbitrary_existing_or_creates_empty_workspace(
    tmp_path_factory, monkeypatch,
):
    config = tmp_path_factory / "config.json"
    monkeypatch.setattr(ConfigManager, "default_path", staticmethod(lambda: config))
    ConfigManager.save(Config())
    app = ApplicationServices()
    existing = tmp_path_factory / "existing project"
    existing.mkdir()
    attached = app.project_create(
        "attached", str(existing), workspace_mode="attach_existing",
    )
    assert attached["workspace"] == str(existing.resolve())
    created_path = tmp_path_factory / "outside-default" / "new project"
    created = app.project_create(
        "new", str(created_path), workspace_mode="create_new",
    )
    assert created["workspace_mode"] == "create_new"
    assert created_path.is_dir()
    with pytest.raises(OperationError, match="already registered"):
        app.project_create("duplicate-path", str(existing), workspace_mode="attach_existing")
    with pytest.raises(OperationError, match="filesystem root"):
        app.project_create("unsafe", Path(existing.anchor), workspace_mode="attach_existing")


@pytest.mark.parametrize("mode", ["soft", "hard"])
def test_confirmed_project_delete_keeps_safety_tombstone_and_removes_last_config(tmp_path_factory, monkeypatch, mode):
    ws, app = setup_project(tmp_path_factory, monkeypatch)
    plan = app.deletion.preview("test", mode=mode)
    root = Path(plan["manifest"]["state_root"])
    app.deletion.confirm(plan["plan_id"])
    result = app.deletion.run(plan["plan_id"])
    assert result["state"] == "completed", result
    assert not ConfigManager.load().projects
    assert (ws / "source.txt").exists() == (mode == "soft")
    assert (root / "project-deleted.json").exists()
    assert not (root / "state.sqlite3").exists()


def test_cancel_and_identity_drift_never_delete(tmp_path_factory, monkeypatch):
    ws, app = setup_project(tmp_path_factory, monkeypatch)
    plan = app.deletion.preview("test", mode="hard")
    app.deletion.confirm(plan["plan_id"])
    app.deletion.cancel(plan["plan_id"])
    with pytest.raises(OperationError):
        app.deletion.run(plan["plan_id"])
    assert ws.exists()
    next_plan = app.deletion.preview("test", mode="hard")
    cfg = ConfigManager.load()
    cfg.projects[0].archived = True
    ConfigManager.save(cfg)
    with pytest.raises(OperationError, match="changed"):
        app.deletion.confirm(next_plan["plan_id"])


def test_live_storage_lease_blocks_without_deleting(tmp_path_factory, monkeypatch):
    ws, app = setup_project(tmp_path_factory, monkeypatch)
    plan = app.deletion.preview("test", mode="hard")
    app.deletion.confirm(plan["plan_id"])
    with StorageRuntime(ws):
        result = app.deletion.run(plan["plan_id"])
        assert result["state"] == "blocked", result
    assert ws.exists()
    app.deletion.retry(plan["plan_id"])
    assert app.deletion.run(plan["plan_id"])["state"] == "completed"


def test_delete_b_preserves_A_other_B_and_recovery(tmp_path_factory, monkeypatch):
    ws, app = setup_project(tmp_path_factory, monkeypatch)
    runtime = get_storage(ws)
    store = SessionStore(str(ws), storage=runtime)
    manager = AgentProcessManager()
    root = manager.fork(None, "supervisor", ToolRegistry([]), 10, RingLevel.RING_0,
                        actor_kind="supervisor", capability_profile_id="supervisor.control", task_id="a")
    first = manager.fork(root.process_id, "worker", ToolRegistry([]), 10, RingLevel.RING_3,
                         capability_profile_id="text.only", task_id="b")
    other = manager.fork(root.process_id, "worker", ToolRegistry([]), 10, RingLevel.RING_3,
                         capability_profile_id="text.only", task_id="c")
    for process in [root, first, other]:
        process.status = ProcessStatus.COMPLETED
        store.save_process_checkpoint(process)
        store.append_event(process.process_id, "agent_complete", {"status": "completed"})
    plan = app.deletion.preview("test", scope="process", process_id=first.process_id)
    app.deletion.confirm(plan["plan_id"])
    result = app.deletion.run(plan["plan_id"])
    assert result["state"] == "completed", result
    runtime = get_storage(ws)
    assert runtime.load_agent_process_state(first.process_id) is None
    assert runtime.is_process_deleted(first.process_id)
    assert runtime.load_agent_process_state(other.process_id)
    assert runtime.load_agent_process_state(root.process_id)
    assert (ws / "source.txt").read_text() == "keep"
    restored = AgentProcessManager()
    restore_incomplete_processes(SessionStore(str(ws), storage=runtime), restored, ws,
                                 include_process_ids=[root.process_id])
    assert restored.get(root.process_id) and restored.get(other.process_id)
    assert restored.get(first.process_id) is None
    with pytest.raises(ValueError, match="PROCESS_DELETED"):
        SessionStore(str(ws), storage=runtime).save_process_checkpoint(first)
    assert runtime._state.execute("PRAGMA foreign_key_check").fetchall() == []


def test_unfinished_task_is_blocking_and_delay_validation(tmp_path_factory, monkeypatch):
    ws, app = setup_project(tmp_path_factory, monkeypatch)
    get_storage(ws).save_agent_checkpoint({"process_id": "a", "session_id": "s", "status": "running"})
    plan = app.deletion.preview("test", mode="hard")
    app.deletion.confirm(plan["plan_id"])
    assert app.deletion.run(plan["plan_id"])["state"] == "blocked"
    for value in [-1, True, "10", 43201]:
        with pytest.raises(OperationError):
            app.config_set("safety.process_delete_delay_minutes", value)
    assert ws.exists()
