"""Real files, SQLite migration and crash boundaries; no installed UI claims."""
from dataclasses import replace
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from backend.core.update.data_upgrade import DataUpgrade, storage_maintenance
from backend.core.update.data_migrate import migrate_shadows
from backend.core.update.manifest import DataCompatibility
from backend.core.update.persistence import atomic_control
from backend.core.update.runtime_contract import runtime_evidence
from backend.core.update.transaction import _compat_wire
from backend.core.update.validation import UpdateError


class Crash(BaseException): pass


def private(path):
    if os.name == "nt":
        from backend.core.update.windows_storage import create_private_directory
        create_private_directory(path)
    else: path.mkdir(mode=0o700)
    return path


@pytest.fixture
def lab(tmp_path_factory):
    root = private(tmp_path_factory / "upgrade")
    directory = private(root / ("a" * 32))
    profile = private(root / "profile")
    resources = {name: profile / (name + ".json") for name in ("config", "providers")}
    for name, path in resources.items():
        path.write_bytes(("original-" + name).encode()); path.chmod(0o600)
    return directory, resources


def prepared(lab):
    directory, resources = lab
    upgrade = DataUpgrade(directory, directory.name, resources)
    shadows = upgrade.prepare()
    for name, path in shadows.items(): path.write_bytes(("converted-" + name).encode())
    upgrade.validate()
    return upgrade


@pytest.mark.parametrize("point", ["data_publish_intent", "data_old_saved:config", "data_new_published:config",
    "data_old_saved:providers", "data_new_published:providers", "data_published"])
def test_crash_at_each_publication_boundary_restores_every_original(lab, point):
    upgrade = prepared(lab)
    def crash(event):
        if event == point: raise Crash()
    with pytest.raises(Crash): upgrade.publish(checkpoint=crash)
    # A new coordinator object consumes only the durable journal.
    DataUpgrade(upgrade.directory, upgrade.transaction_id, upgrade.resources).recover(committed=False)
    for name, path in upgrade.resources.items(): assert path.read_bytes() == ("original-" + name).encode()
    upgrade.recover(committed=False)


@pytest.mark.parametrize("point", ["data_failed_saved:config", "data_old_restored:config",
                                    "data_failed_saved:providers", "data_old_restored:providers"])
def test_interrupted_data_recovery_is_restartable(lab, point):
    upgrade = prepared(lab); upgrade.publish()
    def crash(event):
        if event == point: raise Crash()
    with pytest.raises(Crash): upgrade.recover(committed=False, checkpoint=crash)
    upgrade.recover(committed=False)
    for name, path in upgrade.resources.items(): assert path.read_bytes() == ("original-" + name).encode()


def test_commit_keeps_later_user_work_and_retains_original_snapshot(lab):
    upgrade = prepared(lab); upgrade.publish()
    path = upgrade.resources["config"]
    path.write_bytes(b"new-user-work-after-upgrade")
    upgrade.recover(committed=True)
    assert path.read_bytes() == b"new-user-work-after-upgrade"
    assert upgrade._slots(path)["old"].read_bytes() == b"original-config"


def test_unknown_data_is_not_overwritten_during_recovery(lab):
    upgrade = prepared(lab); upgrade.publish()
    upgrade.resources["config"].write_bytes(b"unexpected-writer")
    with pytest.raises(UpdateError, match="UPDATE_DATA_CHANGED"):
        upgrade.recover(committed=False)
    assert upgrade.resources["config"].read_bytes() == b"unexpected-writer"


def test_changes_during_conversion_do_not_replace_originals(lab):
    directory, resources = lab
    upgrade = DataUpgrade(directory, directory.name, resources)
    upgrade.prepare(); resources["config"].write_bytes(b"concurrent-user-setting")
    with pytest.raises(UpdateError, match="UPDATE_DATA_CHANGED"): upgrade.validate()
    assert resources["config"].read_bytes() == b"concurrent-user-setting"


def test_resource_deletion_is_rejected_and_source_is_untouched(lab):
    directory, resources = lab
    upgrade = DataUpgrade(directory, directory.name, resources)
    shadows = upgrade.prepare(); shadows["config"].unlink()
    with pytest.raises(UpdateError, match="UPDATE_DATA_LOSS"): upgrade.validate()
    assert resources["config"].read_bytes() == b"original-config"


def test_boundary_overlap_and_hardlinks_are_not_accepted(lab):
    directory, resources = lab
    with pytest.raises(UpdateError, match="UPDATE_DATA_PATH"):
        DataUpgrade(directory, directory.name, {"config": resources["config"], "all": resources["config"].parent})
    link = resources["config"].with_name("hardlink")
    os.link(resources["config"], link)
    with pytest.raises(UpdateError, match="UPDATE_DATA_PATH"):
        DataUpgrade(directory, directory.name, resources).prepare()


def test_real_config_conversion_preserves_retired_fields_through_later_saves(lab):
    from backend.core.config import ConfigManager
    directory, resources = lab
    raw = {"schema_version": 1, "projects": [], "retired_feature": {"history": [1, 2, 3]}, "language": "zh"}
    resources["config"].write_text(json.dumps(raw), encoding="utf-8")
    resources["providers"].write_text('{"providers":[]}', encoding="utf-8")
    upgrade = DataUpgrade(directory, directory.name, resources)
    shadows = upgrade.prepare()
    source = replace(DataCompatibility.parse(runtime_evidence()[0]), config_migration_sha256="9" * 64)
    migrate_shadows(shadows, source)
    assert resources["config"].read_text(encoding="utf-8") == json.dumps(raw)
    upgrade.validate(); upgrade.publish()
    cfg = ConfigManager.load(resources["config"], strict=True); cfg.language = "en"
    ConfigManager.save(cfg, resources["config"], allow_empty=True)
    saved = json.loads(resources["config"].read_text(encoding="utf-8"))
    assert saved["retired_feature"] == raw["retired_feature"] and saved["language"] == "en"


def test_actual_source_host_role_migrates_only_shadow_files(lab):
    directory, resources = lab
    resources = {"config": resources["config"]}
    resources["config"].write_text('{"projects":[],"obsolete_option":"keep"}', encoding="utf-8")
    upgrade = DataUpgrade(directory, directory.name, resources)
    shadows = upgrade.prepare()
    plan = directory / "plan.json"; nonce = "b" * 32
    atomic_control(plan, {"schema_version": 1, "transaction_id": directory.name, "nonce": nonce,
        "source": runtime_evidence()[0], "resources": {name: str(path) for name, path in shadows.items()}})
    result = subprocess.run([sys.executable, "-B", "-m", "backend.core.native_host_entry", "--gitgo-internal-role",
        "update-data-migrate", "--plan", str(plan)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"schema_version": 1, "nonce": nonce, "migration_verified": True}
    assert json.loads(shadows["config"].read_text(encoding="utf-8"))["obsolete_option"] == "keep"
    assert "keyboard_bindings" not in resources["config"].read_text(encoding="utf-8")


def test_real_sqlite_upgrade_keeps_rows_and_stable_maintenance_inode(lab, monkeypatch):
    from backend.core.storage.migrations import STATE_MIGRATIONS
    from backend.core.storage.runtime import StorageRuntime
    from backend.core.storage.maintenance import StorageLease, StorageMaintenanceActive
    directory, resources = lab
    root = resources["config"].parent
    database = root / "state.sqlite3"
    with closing(sqlite3.connect(database, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        StorageRuntime._apply_migrations(db, STATE_MIGRATIONS[:-1])
        db.execute("INSERT INTO projects VALUES('example-project','business-workspace','old','old')")
    database.chmod(0o600)
    resources = {"db_test_state": database,
                 "db_test_state_wal": root / "state.sqlite3-wal", "db_test_state_shm": root / "state.sqlite3-shm"}
    with StorageLease(root):
        with pytest.raises(StorageMaintenanceActive):
            with storage_maintenance(resources): pass
    lock = root / "storage-maintenance.lock"; inode = lock.stat().st_ino
    source = DataCompatibility.parse(runtime_evidence()[0])
    source = replace(source, state_migrations=source.state_migrations[:-1])
    with storage_maintenance(resources):
        upgrade = DataUpgrade(directory, directory.name, resources)
        shadows = upgrade.prepare(); migrate_shadows(shadows, source)
        upgrade.validate(); upgrade.publish()
    assert lock.stat().st_ino == inode
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT project_id FROM projects").fetchall() == [("example-project",)]
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == len(STATE_MIGRATIONS)
    upgrade.recover(committed=False)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == len(STATE_MIGRATIONS)-1
