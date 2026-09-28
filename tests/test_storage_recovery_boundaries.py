from pathlib import Path
import json
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

from backend.core.storage import StorageRuntime, StorageRuntimeUnsupported, StorageCorruptionDetected
from backend.core.storage.runtime import validate_sqlite_runtime, _StorageConnection
from backend.core.storage.maintenance import StorageLease, StorageMaintenanceActive
from scripts.recover_state_sqlite import recover


def test_abrupt_process_exit_preserves_committed_checkpoint_and_allows_resume(
    tmp_path_factory: Path,
):
    workspace = tmp_path_factory / "hot-close-workspace"
    workspace.mkdir()
    state_home = tmp_path_factory / "hot-close-state"
    script = f"""
import os
from backend.core.storage import StorageRuntime
runtime = StorageRuntime({str(workspace)!r}, state_home={str(state_home)!r})
runtime.save_agent_checkpoint({{
    'process_id': 'root-hot', 'session_id': 'session-hot',
    'task_id': 'task-hot', 'status': 'running', 'role': 'supervisor',
    'actor_kind': 'supervisor',
    'messages': [{{'role': 'user', 'content': 'survive abrupt close',
                  'message_type': 'conversation'}}],
    'session_metadata': {{'context_memo': {{}}, 'host_ledger': [],
                         'provider_usage': {{'input_tokens': 4}}}},
}})
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr

    with StorageRuntime(workspace, state_home=state_home) as reopened:
        state = reopened.load_agent_process_state("root-hot")
        assert state is not None
        assert state["session"]["messages"][0]["content"] == "survive abrupt close"
        assert state["process"]["status"] == "running"
        reopened.save_agent_checkpoint({
            "process_id": "root-hot", "session_id": "session-hot",
            "task_id": "task-hot", "status": "completed",
            "role": "supervisor", "actor_kind": "supervisor",
            "messages": [{
                "role": "user", "content": "survive abrupt close",
                "message_type": "conversation",
            }],
            "session_metadata": {
                "context_memo": {}, "host_ledger": [],
                "provider_usage": {"input_tokens": 4},
            },
            "result": {"status": "completed", "response": "resumed safely"},
        })

    with StorageRuntime(workspace, state_home=state_home) as final:
        conversation = final.read_latest_conversations()["main_conversation"]
        assert conversation[-1]["content"] == "resumed safely"
        assert not any("reference_missing" in reason for reason in final.check_health().reasons)


@pytest.mark.parametrize("version", [(3, 49, 1), (3, 50, 6), (3, 51, 2), (3, 44, 5)])
def test_rejects_known_wal_reset_engines(version):
    with pytest.raises(StorageRuntimeUnsupported):
        validate_sqlite_runtime(version)


@pytest.mark.parametrize("version", [(3, 44, 6), (3, 50, 7), (3, 51, 3), (3, 53, 4)])
def test_accepts_verified_fix_branches(version):
    validate_sqlite_runtime(version)


def test_maintenance_is_exclusive_but_normal_runtimes_share(tmp_path_factory: Path):
    with StorageLease(tmp_path_factory), StorageLease(tmp_path_factory):
        with pytest.raises(StorageMaintenanceActive):
            StorageLease(tmp_path_factory, exclusive=True)
    with StorageLease(tmp_path_factory, exclusive=True):
        with pytest.raises(StorageMaintenanceActive):
            StorageLease(tmp_path_factory)
    with StorageLease(tmp_path_factory):
        pass


def test_recovery_keeps_committed_wal_and_existing_messages(tmp_path_factory: Path):
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    snapshot = tmp_path_factory / "offline-copy"
    snapshot.mkdir()
    import shutil
    with StorageRuntime(workspace) as runtime:
        runtime.save_agent_checkpoint({
            "process_id": "root", "session_id": "session", "task_id": "task",
            "status": "completed", "role": "supervisor", "actor_kind": "supervisor",
            "messages": [{"role": "user", "content": "Committed only in WAL", "message_type": "conversation"}],
            "result": {"status": "completed", "response": "Done"},
        })
        # No concurrent writer in this test; preserve the uncheckpointed family.
        for suffix in ("", "-wal"):
            shutil.copy2(Path(str(runtime.paths.state_db) + suffix), snapshot / ("state.sqlite3" + suffix))
        source = snapshot / "state.sqlite3"
        before = {p.name: p.read_bytes() for p in snapshot.glob("*.sqlite3*")}
        destination = snapshot / "recovered.sqlite3"
        report = recover(source, destination, runtime.paths.cas_dir)
        assert report["copied_journals"] == ["-wal"]
        assert report["copied"]["messages"]["rows"] >= 1
        assert report["messages_reconstructed"] == 0
        with closing(sqlite3.connect(destination)) as connection:
            assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        for name, data in before.items():
            assert (snapshot / name).read_bytes() == data


def test_query_fault_reports_immediately_and_does_not_classify_constraints(tmp_path_factory: Path):
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    runtime = StorageRuntime(workspace)
    try:
        error = sqlite3.DatabaseError("damaged btree")
        error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
        with pytest.raises(StorageCorruptionDetected):
            runtime._record_database_fault("state", runtime.paths.state_db, error)
        health = runtime.check_health()
        assert health.level.value == "blocked"
        assert "state_integrity_failed" in " ".join(health.reasons)
        with pytest.raises(Exception, match="integrity_failed"):
            runtime.put_state_ref("test", "key", "value")
        other = sqlite3.IntegrityError("duplicate key")
        other.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT
        runtime._record_database_fault("state", runtime.paths.state_db, other)
    finally:
        runtime.close()


def test_cursor_deferred_read_uses_same_fault_boundary():
    connection = sqlite3.connect(":memory:", factory=_StorageConnection)
    observed = []
    connection.fault_callback = observed.append
    try:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("SELECT * FROM missing_table").fetchall()
        assert len(observed) == 1
    finally:
        connection.close()


def test_disposable_reset_preserves_files_identity_and_backup(tmp_path_factory: Path):
    from scripts.reset_disposable_storage import reset_disposable
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text("preserve this artifact", encoding="utf-8")
    with StorageRuntime(workspace) as runtime:
        identity = runtime.paths.project_id
        runtime.put_state_ref("test", "old", "old-ref")
    with pytest.raises(ValueError):
        reset_disposable(workspace, expected_project_id=identity, discard_test_data=False)
    with pytest.raises(ValueError):
        reset_disposable(workspace, expected_project_id="wrong", discard_test_data=True)
    result = reset_disposable(workspace, expected_project_id=identity, discard_test_data=True)
    assert result["health"] == "ok"
    assert (workspace / "index.html").read_text() == "preserve this artifact"
    backup = Path(result["backup"])
    with closing(sqlite3.connect(backup / "state.sqlite3")) as source:
        assert source.execute("SELECT value_ref FROM storage_kv WHERE key='old'").fetchone()[0] == "old-ref"
    with StorageRuntime(workspace) as runtime:
        assert runtime.paths.project_id == identity
        assert runtime.get_state_ref("test", "old") is None


def test_incomplete_recovery_never_silently_opens_a_new_database(tmp_path_factory: Path):
    from backend.core.storage import resolve_storage_paths, StorageBlocked
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    paths = resolve_storage_paths(workspace)
    (paths.project_root / "storage-recovery-in-progress.json").write_text("{}")
    with pytest.raises(StorageBlocked, match="RECOVERY_INCOMPLETE"):
        StorageRuntime(workspace)
    assert not paths.state_db.exists()


def test_split_or_redirected_family_is_rejected(tmp_path_factory: Path, monkeypatch):
    from backend.core.storage.paths import validate_storage_location, StorageLocationConflict
    root = tmp_path_factory / "store"
    root.mkdir()
    member = root / "state.sqlite3"
    member.touch()
    original_resolve = Path.resolve
    def redirected(path, *args, **kwargs):
        if path == member:
            return tmp_path_factory / "private-view" / "state.sqlite3"
        return original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(StorageLocationConflict, match="redirected or split"):
        validate_storage_location(root)


def test_relocation_keeps_old_family_and_publishes_verified_store(tmp_path_factory: Path):
    from scripts.reset_disposable_storage import relocate_disposable
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    old_home, new_home = tmp_path_factory / "old", tmp_path_factory / "new"
    with StorageRuntime(workspace, state_home=old_home) as runtime:
        identity = runtime.paths.project_id
        runtime.put_state_ref("test", "keep-in-backup", "ref")
        source = runtime.paths.state_db
    original = source.read_bytes()
    result = relocate_disposable(workspace, expected_project_id=identity, source_home=old_home,
                                 destination_home=new_home, discard_test_data=True)
    assert result["health"] == "ok"
    assert source.read_bytes() == original
    assert (new_home / "projects" / identity / "storage-location.json").is_file()


def test_preserving_relocation_keeps_state_and_source_family(tmp_path_factory: Path):
    from scripts.relocate_state_storage import relocate_preserving
    workspace = tmp_path_factory / "preserved-workspace"
    workspace.mkdir()
    old_home = tmp_path_factory / "preserved-old"
    new_home = tmp_path_factory / "preserved-new"
    with StorageRuntime(workspace, state_home=old_home) as runtime:
        identity = runtime.paths.project_id
        runtime.put_state_ref("test", "durable", "kept-ref")
        source = runtime.paths.state_db
    original = source.read_bytes()

    result = relocate_preserving(
        workspace,
        expected_project_id=identity,
        source_home=old_home,
        destination_home=new_home,
    )

    assert result["source_retained"] is True
    assert result["published_health"] == "ok"
    assert source.read_bytes() == original
    with StorageRuntime(workspace, state_home=new_home) as relocated:
        assert relocated.get_state_ref("test", "durable") == "kept-ref"
    receipt = new_home / "projects" / identity / "storage-location.json"
    assert json.loads(receipt.read_text(encoding="utf-8"))["source_home"] == str(old_home.absolute())
