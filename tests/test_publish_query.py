"""Publish queries preserve Alpha3 state and never imply unverified effects."""
import base64
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest

from backend.core.application import ApplicationServices, OperationError
from backend.core.application.publish import list_items, PublishQueryError
from backend.core.config import Config, ConfigManager, ProjectConfig
from backend.core.storage import get_storage, close_owned_storage, bind_storage, StorageRuntime
from backend.core.sync_session.state import save_state


def project_at(workspace):
    workspace.mkdir(exist_ok=True)
    project = ProjectConfig(name="publish-test")
    project.workspace_path = str(workspace)
    return project


def records(count=2):
    return {"project": "publish-test", "stage": "IDLE", "formal_commits": [
        {"message": f"中文候选 🚀 {i}", "prefix": "TEST", "number": i, "synced": True, "pushed": True}
        for i in range(count)]}


def legacy(workspace, data):
    path = workspace / ".gitgo" / "session.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_empty_query_creates_no_identity_storage_or_runtime(tmp_path_factory, monkeypatch):
    project = project_at(tmp_path_factory / "workspace")
    def forbidden(*args, **kwargs):
        raise AssertionError("A read opened a writer or executed a sync step")
    monkeypatch.setattr("backend.core.sync_session.state.get_storage", forbidden)
    monkeypatch.setattr("backend.core.sync_session.SyncSession.__init__", forbidden)
    monkeypatch.setattr(ConfigManager, "save", forbidden)
    result = list_items(project)
    assert result["items"] == [] and result["total"] == 0
    assert list(Path(project.workspace_path).iterdir()) == []


def test_legacy_flags_remain_unverified_and_query_preserves_original(tmp_path_factory):
    project = project_at(tmp_path_factory / "workspace")
    path = legacy(Path(project.workspace_path), records())
    before = path.read_bytes()
    result = list_items(project)
    assert len(result["items"]) == 2
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]
    for item in result["items"]:
        assert item["identity_kind"] == "snapshot_observation"
        assert item["status"] == "needs_reconciliation"
        assert item["candidate_oid"] is None and item["target"] is None
        assert item["recorded"]["pushed"] is True
        assert item["allowed_actions"] == ["migrate"]
        assert not any(node["status"] == "completed" for node in item["nodes"])


def test_pagination_binds_snapshot_and_workspace(tmp_path_factory):
    first = project_at(tmp_path_factory / "first")
    legacy(Path(first.workspace_path), records(3))
    page = list_items(first, limit=1)
    second_page = list_items(first, limit=2, cursor=page["next_cursor"])
    assert [item["label"] for item in second_page["items"]] == ["TEST-1", "TEST-2"]
    assert second_page["next_cursor"] is None
    second = project_at(tmp_path_factory / "second")
    legacy(Path(second.workspace_path), records(3))
    with pytest.raises(PublishQueryError, match="SNAPSHOT_CHANGED"):
        list_items(second, cursor=page["next_cursor"])
    legacy(Path(first.workspace_path), records(4))
    with pytest.raises(PublishQueryError, match="SNAPSHOT_CHANGED"):
        list_items(first, cursor=page["next_cursor"])


@pytest.mark.parametrize("arguments", [{"limit": True}, {"limit": 0}, {"limit": 101},
    {"cursor": "!"}, {"cursor": 4}, {"cursor": "x" * 2049},
    {"cursor": base64.urlsafe_b64encode(b'{"snapshot":"x","offset":true}').decode()}])
def test_invalid_queries_are_rejected(tmp_path_factory, arguments):
    with pytest.raises(PublishQueryError):
        list_items(project_at(tmp_path_factory), **arguments)


def test_cold_committed_query_never_constructs_a_writer(tmp_path_factory, monkeypatch):
    project = project_at(tmp_path_factory / "workspace")
    reference, path = save_state(project.workspace_path, records(), expected_ref=None)
    close_owned_storage()
    def forbidden(*args, **kwargs):
        raise AssertionError("Cold query constructed StorageRuntime")
    monkeypatch.setattr(StorageRuntime, "__init__", forbidden)
    before = path.read_bytes()
    result = list_items(project)
    assert result["items"][0]["source"]["state_ref"] == reference
    assert path.read_bytes() == before


def test_injected_store_is_the_only_authority_and_empty_does_not_resurrect_legacy(tmp_path_factory):
    project = project_at(tmp_path_factory / "workspace")
    legacy(Path(project.workspace_path), records(4))
    with StorageRuntime(project.workspace_path, state_home=tmp_path_factory / "other-state") as store:
        bind_storage(project.workspace_path, store)
        save_state(project.workspace_path, records(0), expected_ref=None)
        assert list_items(project)["items"] == []


def test_missing_or_corrupt_committed_object_does_not_fall_back(tmp_path_factory):
    project = project_at(tmp_path_factory / "workspace")
    reference, path = save_state(project.workspace_path, records(), expected_ref=None)
    legacy(Path(project.workspace_path), records(4))
    path.write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="DIGEST_MISMATCH"):
        list_items(project)
    assert path.read_bytes() == b"corrupt"
    path.unlink()
    with pytest.raises(RuntimeError, match="GOVERNANCE_STATE_UNAVAILABLE"):
        list_items(project)


def test_application_reads_explicit_legacy_config_without_migrating(tmp_path_factory, monkeypatch):
    project = project_at(tmp_path_factory / "workspace")
    legacy(Path(project.workspace_path), records())
    config = tmp_path_factory / "old-config.json"
    ConfigManager.save(Config(projects=[project]), config)
    before = config.read_bytes()
    monkeypatch.setattr(ConfigManager, "find_config", lambda: config)
    def forbidden(*args, **kwargs):
        raise AssertionError("Query attempted a config migration")
    monkeypatch.setattr(ConfigManager, "save", forbidden)
    result = ApplicationServices().invoke("publish.items", {"project": project.name})
    assert result["total"] == 2 and config.read_bytes() == before
    with pytest.raises(OperationError) as error:
        ApplicationServices().invoke("publish.items", {"project": project.name, "limit": 1000})
    assert error.value.code == "PUBLISH_INVALID_LIMIT"


def test_application_reports_damaged_state_without_internal_paths(tmp_path_factory):
    project = project_at(tmp_path_factory / "workspace")
    ConfigManager.save(Config(projects=[project]))
    path = legacy(Path(project.workspace_path), records())
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(OperationError) as error:
        ApplicationServices().invoke("publish.items", {"project": project.name})
    assert error.value.code == "PUBLISH_STATE_UNAVAILABLE"
    assert str(tmp_path_factory) not in str(error.value)


def test_read_snapshot_never_creates_a_missing_database(tmp_path_factory):
    project = project_at(tmp_path_factory / "workspace")
    save_state(project.workspace_path, records(), expected_ref=None)
    database = get_storage(project.workspace_path).paths.state_db
    close_owned_storage()
    database.unlink()
    with pytest.raises(Exception):
        list_items(project)
    assert not database.exists()


def test_real_native_host_advertises_and_reads_the_same_snapshot(tmp_path_factory):
    project = project_at(tmp_path_factory / "workspace")
    ConfigManager.save(Config(projects=[project]))
    save_state(project.workspace_path, records(), expected_ref=None)
    expected = list_items(project)
    close_owned_storage()
    source = str(Path(__file__).resolve().parents[1])
    bootstrap = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('backend.core.native_host',run_name='__main__')"
    process = subprocess.Popen([sys.executable, "-B", "-u", "-c", bootstrap, source],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
    replies = queue.Queue()
    def collect():
        for line in process.stdout:
            replies.put(json.loads(line))
    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        assert replies.get(timeout=15)["type"] == "host_started"
        for request_id, operation, arguments in [("capabilities", "host.capabilities", {}),
                ("items", "publish.items", {"project": project.name})]:
            process.stdin.write(json.dumps({"type": "request", "protocol_version": 1,
                "request_id": request_id, "operation": operation, "arguments": arguments}) + "\n")
            process.stdin.flush()
            while True:
                reply = replies.get(timeout=15)
                if reply.get("request_id") == request_id:
                    break
            assert not reply.get("error"), reply
            if request_id == "capabilities":
                assert "publish.items" in reply["result"]["operations"]
            else:
                assert reply["result"] == expected
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)
        reader.join(timeout=2)
        process.stdout.close(); process.stderr.close()
    assert process.returncode == 0
