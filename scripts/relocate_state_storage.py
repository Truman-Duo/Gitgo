"""Non-destructively relocate one complete Gitgo SQLite state family.

This is an explicit offline operator action.  The source is never replaced or
deleted.  Main databases, journals, CAS, and lifecycle metadata are copied to
an isolated staging home, WAL is replayed there, and the verified project root
is atomically published with a migration receipt.
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

from backend.core.storage import StorageRuntime
from backend.core.storage.maintenance import StorageLease
from backend.core.storage.paths import _ensure_project_id
from backend.core.storage.runtime import validate_sqlite_runtime


def _durable_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())


def _validate_database(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        if integrity != ["ok"] or foreign_keys:
            raise RuntimeError(
                f"Relocated database failed validation: {path}; "
                f"integrity={integrity[:5]}, foreign_keys={foreign_keys[:5]}"
            )
        return {
            "integrity": integrity,
            "foreign_key_errors": len(foreign_keys),
            "page_count": int(connection.execute("PRAGMA page_count").fetchone()[0]),
        }


def relocate_preserving(
    workspace: Path,
    *,
    expected_project_id: str,
    source_home: Path,
    destination_home: Path,
) -> dict:
    """Publish a verified copy while retaining the complete original family."""
    validate_sqlite_runtime()
    workspace = workspace.resolve()
    if _ensure_project_id(workspace) != expected_project_id:
        raise ValueError("Project identity differs from the explicitly authorized target")
    source_home = source_home.absolute()
    destination_home = destination_home.expanduser().resolve()
    source = source_home / "projects" / expected_project_id
    target = destination_home / "projects" / expected_project_id
    if not source.is_dir():
        raise FileNotFoundError(f"Source project store does not exist: {source}")
    if target.exists():
        raise FileExistsError(f"Destination project store already exists: {target}")
    if destination_home == source_home or destination_home.is_relative_to(source):
        raise ValueError("Destination must be a separate stable state home")

    destination_home.mkdir(parents=True, exist_ok=True)
    backup_root = destination_home / "recovery" / expected_project_id / uuid.uuid4().hex
    source_snapshot = backup_root / "source"
    staging_home = backup_root / "staging-home"
    staged = staging_home / "projects" / expected_project_id
    source_files = {
        name: str((source / name).resolve())
        for name in (
            "state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm",
            "observability.sqlite3", "observability.sqlite3-wal",
            "observability.sqlite3-shm",
        )
        if (source / name).exists()
    }
    required = sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
    # Source snapshot + staging copy + bounded safety room.
    if shutil.disk_usage(destination_home).free < required * 2 + 512 * 1024**2:
        raise RuntimeError("Insufficient space for source snapshot, staging copy, and safety reserve")

    with StorageLease(source, exclusive=True):
        shutil.copytree(
            source, source_snapshot,
            ignore=shutil.ignore_patterns("storage-maintenance.lock"),
        )
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_snapshot, staged)
        # SHM is a transient shared-memory index, never durable evidence.  WAL
        # is retained and replayed against its matching copied main database.
        for name in ("state.sqlite3-shm", "observability.sqlite3-shm"):
            (staged / name).unlink(missing_ok=True)
        validation = {
            name: _validate_database(staged / name)
            for name in ("state.sqlite3", "observability.sqlite3")
        }
        with StorageRuntime(workspace, state_home=staging_home) as runtime:
            health = runtime.check_health().level.value
            counts = runtime.session_storage_counts()
        if health != "ok":
            raise RuntimeError(f"Relocated runtime is not healthy: {health}")

        result = {
            "migration": "preserve-complete-family-v1",
            "project_id": expected_project_id,
            "workspace": str(workspace),
            "source_home": str(source_home),
            "destination_home": str(destination_home),
            "backup": str(source_snapshot),
            "source_retained": True,
            "source_files": source_files,
            "sqlite_version": sqlite3.sqlite_version,
            "validation": validation,
            "health": health,
            "counts": counts,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        _durable_json(staged / "storage-location.json", result)
        _durable_json(backup_root / "relocation-manifest.json", result)
        target.parent.mkdir(parents=True, exist_ok=True)
        staged.rename(target)

    with StorageRuntime(workspace, state_home=destination_home) as runtime:
        final_health = runtime.check_health().level.value
        final_counts = runtime.session_storage_counts()
    if final_health != "ok" or final_counts != counts:
        raise RuntimeError(
            f"Published store verification failed: health={final_health}, counts={final_counts}"
        )
    result["published_health"] = final_health
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--expected-project-id", required=True)
    parser.add_argument("--source-state-home", required=True, type=Path)
    parser.add_argument("--destination-state-home", required=True, type=Path)
    args = parser.parse_args()
    result = relocate_preserving(
        args.workspace,
        expected_project_id=args.expected_project_id,
        source_home=args.source_state_home,
        destination_home=args.destination_state_home,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
