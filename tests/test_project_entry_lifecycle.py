from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import pytest
from backend.core.config import Config, ConfigManager, ProjectConfig
from backend.core.application import ApplicationServices, OperationError
from backend.core.application.project_lifecycle import maintain_unavailable_projects, set_archived
from backend.core.loop.provider_identity import recorded_model_id

@pytest.fixture
def catalog(tmp_path_factory, monkeypatch):
    root = tmp_path_factory / "entry"
    root.mkdir()
    path = root / "config.json"
    monkeypatch.setattr(ConfigManager, "default_path", staticmethod(lambda: path))
    project = ProjectConfig(name="gone")
    project.workspace_path = str(root / "missing")
    ConfigManager.save(Config(projects=[project]))
    return root, ApplicationServices()

def test_entry_failure_is_early_durable_and_does_not_reset_timer(catalog):
    root, service = catalog
    # Listing alone never starts the archive clock.
    service.project_list()
    assert ConfigManager.load().projects[0].unavailable_entry == {}
    for _ in range(2):
        with pytest.raises(OperationError) as result:
            service.invoke("project.enter", {"name": "gone"})
        assert result.value.code == "WORKSPACE_NOT_FOUND"
        assert str(root / "missing") in str(result.value)
        assert result.value.details["archive_at"]
        entry = ConfigManager.load().projects[0].unavailable_entry
        if _ == 0:
            original = entry
        assert entry == original
    assert not (root / "missing").exists()

def test_due_archive_is_idempotent_and_uses_same_transition(catalog):
    _, service = catalog
    with pytest.raises(OperationError):
        service.project_enter("gone")
    cfg = ConfigManager.load()
    start = datetime.fromisoformat(cfg.projects[0].unavailable_entry["first_failed_at"])
    assert maintain_unavailable_projects(cfg, now=start + timedelta(minutes=59)) == []
    assert maintain_unavailable_projects(cfg, now=start + timedelta(minutes=60))[0]["state"] == "archived"
    assert cfg.projects[0].archived
    assert cfg.projects[0].unavailable_entry == {}
    assert maintain_unavailable_projects(cfg, now=start + timedelta(days=1)) == []
    ConfigManager.save(cfg)
    assert service.project_list() == []
    assert service.project_list_archived()[0]["name"] == "gone"
    # The ordinary archive command restores, clearing expired schedules.
    assert service.project_archive("gone")["state"] == "restored"
    assert ConfigManager.load().projects[0].unavailable_entry == {}

@pytest.mark.parametrize("restore_via_entry", [False, True])
def test_restored_workspace_cancels_timer(catalog, restore_via_entry):
    root, service = catalog
    with pytest.raises(OperationError):
        service.project_enter("gone")
    (root / "missing").mkdir()
    if restore_via_entry:
        assert service.project_enter("gone")["ok"]
    else:
        assert service.maintain_project_lifecycle()[0]["state"] == "workspace_restored"
    assert not ConfigManager.load().projects[0].archived
    assert ConfigManager.load().projects[0].unavailable_entry == {}

def test_changed_workspace_and_access_failure_never_authorize_archive(catalog, monkeypatch):
    _, service = catalog
    with pytest.raises(OperationError):
        service.project_enter("gone")
    cfg = ConfigManager.load()
    cfg.projects[0].workspace_path += "-moved"
    assert maintain_unavailable_projects(cfg)[0]["state"] == "schedule_cleared"
    assert not cfg.projects[0].archived
    monkeypatch.setattr("backend.core.application.project_lifecycle.workspace_missing", lambda _: (_ for _ in ()).throw(PermissionError("denied")))
    with pytest.raises(OperationError) as result:
        service.project_enter("gone")
    assert result.value.code == "WORKSPACE_UNAVAILABLE"

def test_config_delay_validation_and_disabled_maintenance(catalog):
    _, service = catalog
    for invalid in [-1, True, "5", 1.2, 43201]:
        with pytest.raises(OperationError):
            service.config_set("safety.unavailable_project_archive_delay_minutes", invalid)
    service.config_set("safety.unavailable_project_archive_delay_minutes", 0)
    with pytest.raises(OperationError) as result:
        service.project_enter("gone")
    assert result.value.details["archive_at"] is None
    cfg = ConfigManager.load()
    assert maintain_unavailable_projects(cfg, now=datetime.now(timezone.utc) + timedelta(days=400)) == []
    assert not cfg.projects[0].archived

def test_archived_and_unknown_entry_rejected(catalog):
    _, service = catalog
    service.project_archive("gone")
    with pytest.raises(OperationError) as result:
        service.project_enter("gone")
    assert result.value.code == "PROJECT_ARCHIVED"
    with pytest.raises(OperationError) as result:
        service.project_enter("other")
    assert result.value.code == "PROJECT_NOT_FOUND"

def test_recorded_model_does_not_use_a_selected_but_unactivated_route():
    ledger = [{"event": "provider_route_activated", "model": "actual-model"},
              {"event": "provider_route_selected", "model": "untried-model"}]
    assert recorded_model_id("", ledger) == "actual-model"
    assert recorded_model_id("new-model", ledger) == "new-model"
    assert recorded_model_id("unknown", []) == ""


def test_model_identity_survives_cold_projection_with_legacy_empty_runtime(tmp_path_factory):
    from backend.core.storage import StorageRuntime
    from backend.core.loop.manager import AgentProcessManager, SessionStore
    from backend.core.loop.models import RingLevel
    from backend.core.loop.tools import ToolRegistry
    from backend.core.daemon.dispatch import _process_status_payload
    ws = tmp_path_factory / "model-projection"
    ws.mkdir()
    with StorageRuntime(ws) as storage:
        manager = AgentProcessManager()
        store = SessionStore(str(ws), storage=storage)
        root = manager.fork(None, "supervisor", ToolRegistry([]), 10, RingLevel.RING_0,
                            actor_kind="supervisor", task_id="root-test")
        root.session.host_ledger.append({"event": "provider_route_activated", "model": "root-recorded"})
        store.save_process_checkpoint(root)
        child = manager.fork(root.process_id, "worker", ToolRegistry([]), 10, RingLevel.RING_3, task_id="child-test")
        child.session.host_ledger.append({"event": "provider_route_activated", "model": "child-recorded"})
        store.save_process_checkpoint(child)
        assert storage.read_latest_root_process()["model_id"] == "root-recorded"
        assert storage.read_project_b_processes()[child.process_id]["model_id"] == "child-recorded"
        assert _process_status_payload(root)["model_id"] == "root-recorded"
