"""Explicit offline reset of disposable test state; never a startup fallback.

Preserves workspace files and identity. Copies the entire old state (including
CAS/WAL) to an external recovery directory before activating clean databases.
A crash during activation leaves a marker that blocks normal runtime startup.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from backend.core.storage import StorageRuntime, resolve_storage_paths
from backend.core.storage.paths import _ensure_project_id
from backend.core.storage.runtime import validate_sqlite_runtime
from backend.core.storage.maintenance import StorageLease
from backend.core.storage.migrations import STATE_MIGRATIONS, OBSERVABILITY_MIGRATIONS


def _durable_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


def reset_disposable(workspace: Path, *, expected_project_id: str, discard_test_data: bool) -> dict:
    if not discard_test_data:
        raise ValueError("Explicit discard_test_data authorization is required")
    validate_sqlite_runtime()
    paths = resolve_storage_paths(workspace)
    if paths.project_id != expected_project_id:
        raise ValueError("Project identity differs from the explicitly authorized target")
    marker = paths.project_root / "storage-recovery-in-progress.json"
    backup = paths.state_home / "recovery" / paths.project_id / uuid.uuid4().hex
    result = {"project_id": paths.project_id, "workspace": str(paths.workspace),
              "backup": str(backup), "discarded": "test runtime history and observability",
              "workspace_files_preserved": True, "sqlite_version": sqlite3.sqlite_version}
    with StorageLease(paths.project_root, exclusive=True):
        if marker.exists():
            raise RuntimeError("An earlier recovery is incomplete; do not overwrite its evidence")
        required = sum(p.stat().st_size for p in paths.project_root.rglob("*") if p.is_file())
        if shutil.disk_usage(paths.project_root).free < required + 1024**3:
            raise RuntimeError("Insufficient space for a complete backup plus safety reserve")
        # Backup is immutable by convention; old journals cannot be paired with
        # the new main files. CAS is backed up too, before future orphan GC.
        shutil.copytree(paths.project_root, backup, ignore=shutil.ignore_patterns("storage-maintenance.lock"))
        staging = backup / "replacement"
        staging.mkdir()
        for name, migrations in (("state.sqlite3", STATE_MIGRATIONS),
                                 ("observability.sqlite3", OBSERVABILITY_MIGRATIONS)):
            with closing(sqlite3.connect(staging / name, isolation_level=None)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA synchronous=FULL")
                StorageRuntime._apply_migrations(connection, migrations)
                if name == "state.sqlite3":
                    timestamp = datetime.now(timezone.utc).isoformat()
                    connection.execute("INSERT INTO projects VALUES(?,?,?,?)",
                                       (paths.project_id, str(paths.workspace), timestamp, timestamp))
                if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
                    raise RuntimeError("Replacement integrity check failed")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("Replacement foreign key check failed")
        _durable_json(backup / "recovery-manifest.json", result)
        displaced = backup / "displaced"
        displaced.mkdir()
        # Resolve and validate every move target before changing any live file.
        names = [base + suffix for base in ("state.sqlite3", "observability.sqlite3")
                 for suffix in ("", "-wal", "-shm", "-journal")]
        names.append("storage-health.json")
        moves = [(paths.project_root / name, displaced / name) for name in names]
        for source, target in moves:
            if not source.resolve().is_relative_to(paths.project_root.resolve()):
                raise RuntimeError("Unexpected storage symlink; refusing replacement")
            if target.exists() or not target.resolve().is_relative_to(backup.resolve()):
                raise RuntimeError("Unexpected backup destination; refusing replacement")
        _durable_json(marker, result)
        for source, target in moves:
            if source.exists():
                source.rename(target)
        for name in ("state.sqlite3", "observability.sqlite3"):
            (staging / name).rename(paths.project_root / name)
        marker.unlink()
    with StorageRuntime(workspace) as runtime:
        result["health"] = runtime.check_health().level.value
        result["counts"] = runtime.session_storage_counts()
    return result


def relocate_disposable(workspace: Path, *, expected_project_id: str,
                        source_home: Path, destination_home: Path, discard_test_data: bool) -> dict:
    """Reset into an unvirtualized location without mutating ambiguous originals."""
    if not discard_test_data:
        raise ValueError("Explicit discard_test_data authorization is required")
    validate_sqlite_runtime()
    workspace = workspace.resolve()
    if _ensure_project_id(workspace) != expected_project_id:
        raise ValueError("Project identity differs from the authorized target")
    source_home, destination_home = source_home.absolute(), destination_home.resolve()
    source = source_home / "projects" / expected_project_id
    target = destination_home / "projects" / expected_project_id
    if not source.is_dir() or target.exists() or target.resolve().is_relative_to(source.resolve()):
        raise ValueError("Source must exist and destination must be a new, separate store")
    backup = destination_home / "recovery" / expected_project_id / uuid.uuid4().hex
    with StorageLease(source, exclusive=True):
        required = sum(p.stat().st_size for p in source.rglob("*") if p.is_file())
        destination_home.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(destination_home).free < required + 1024**3:
            raise RuntimeError("Insufficient backup space")
        shutil.copytree(source, backup, symlinks=True,
                        ignore=shutil.ignore_patterns("storage-maintenance.lock"))
        staging_home = backup / "new-store"
        with StorageRuntime(workspace, state_home=staging_home) as runtime:
            result = {"project_id": expected_project_id, "source_home": str(source_home),
                      "destination_home": str(destination_home), "backup": str(backup),
                      "discarded": "test runtime history and observability",
                      "workspace_files_preserved": True, "sqlite_version": sqlite3.sqlite_version,
                      "health": runtime.check_health().level.value,
                      "counts": runtime.session_storage_counts(),
                      "source_files": {name: str((source / name).resolve()) for name in
                                       ("state.sqlite3", "state.sqlite3-wal", "observability.sqlite3",
                                        "observability.sqlite3-wal")}}
            staged = runtime.paths.project_root
        _durable_json(staged / "storage-location.json", result)
        _durable_json(backup / "relocation-manifest.json", result)
        target.parent.mkdir(parents=True, exist_ok=True)
        staged.rename(target)
    with StorageRuntime(workspace, state_home=destination_home) as runtime:
        result["health"] = runtime.check_health().level.value
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--expected-project-id", required=True)
    parser.add_argument("--discard-test-data", action="store_true")
    parser.add_argument("--source-state-home", type=Path)
    parser.add_argument("--destination-state-home", type=Path)
    args = parser.parse_args()
    if bool(args.source_state_home) != bool(args.destination_state_home):
        parser.error("source and destination state homes must be supplied together")
    common = dict(expected_project_id=args.expected_project_id, discard_test_data=args.discard_test_data)
    result = (relocate_disposable(args.workspace, source_home=args.source_state_home,
                                 destination_home=args.destination_state_home, **common)
              if args.source_state_home else reset_disposable(args.workspace, **common))
    print(json.dumps(result, ensure_ascii=False, indent=2))
