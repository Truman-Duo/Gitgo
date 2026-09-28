"""Explicit, non-destructive salvage for a truncated Gitgo state database.

The source is never opened writable and the destination must not exist.  The
tool copies reachable relational rows, rebuilds the CAS object catalogue, and
reconstructs public user turns plus usage rollups from durable task objects.
It is deliberately an operator tool, not an automatic startup rewrite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import uuid
from collections import defaultdict
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from backend.core.storage.migrations import STATE_MIGRATIONS
from backend.core.storage.runtime import StorageRuntime, validate_sqlite_runtime
from backend.core.storage.maintenance import StorageLease
from backend.core.storage.paths import validate_storage_location


COPY_TABLES = (
    "projects", "sessions", "tasks", "tool_calls", "receipts",
    "governance_signals", "test_evidence", "provider_attempts",
    "mailbox_messages", "worktrees", "dependency_nodes",
    "dependency_edges", "object_refs", "storage_kv",
    "session_processes", "session_events", "process_dependencies",
    "history_events", "lessons", "messages", "task_usage", "process_presentation",
    "deleted_processes", "deletion_plans",
)


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _patch_shadow_header(path: Path) -> dict:
    with path.open("rb") as handle:
        data = bytearray(handle.read(100))
    if len(data) < 100 or data[:16] != b"SQLite format 3\x00":
        raise RuntimeError("source does not have a valid SQLite header")
    encoded = int.from_bytes(data[16:18], "big")
    page_size = 65536 if encoded == 1 else encoded
    file_size = path.stat().st_size
    if not page_size or file_size % page_size:
        raise RuntimeError("source size is not aligned to its SQLite page size")
    physical_pages = file_size // page_size
    declared_pages = int.from_bytes(data[28:32], "big")
    if declared_pages > physical_pages:
        data[28:32] = physical_pages.to_bytes(4, "big")
        with path.open("r+b") as handle:
            handle.write(data)
    return {
        "page_size": page_size,
        "declared_pages": declared_pages,
        "physical_pages": physical_pages,
        "shadow_header_patched": declared_pages > physical_pages,
    }


def _cas_path(cas_dir: Path, ref: str) -> Path:
    digest = ref.removeprefix("sha256:")
    return cas_dir / digest[:2] / digest[2:]


def _read_json(cas_dir: Path, ref: str) -> dict | None:
    try:
        value = json.loads(_cas_path(cas_dir, ref).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, dict) else None


def _put_json(cas_dir: Path, value: dict) -> tuple[str, int]:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    target = cas_dir / digest[:2] / digest[2:]
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(encoded)
        temporary.replace(target)
    return "sha256:" + digest, len(encoded)


def _copy_reachable_rows(
    source: sqlite3.Connection, destination: sqlite3.Connection, table: str,
) -> tuple[int, str]:
    columns = [str(row[1]) for row in destination.execute(f"PRAGMA table_info({_quoted(table)})")]
    if not columns:
        return 0, "destination table missing"
    selected = ",".join(_quoted(column) for column in columns)
    count = 0
    error = ""
    placeholders = ",".join("?" for _ in columns)
    insert = f"INSERT OR IGNORE INTO {_quoted(table)} ({selected}) VALUES ({placeholders})"
    try:
        cursor = source.execute(f"SELECT {selected} FROM {_quoted(table)}")
        while True:
            try:
                row = cursor.fetchone()
            except sqlite3.DatabaseError as exc:
                error = str(exc)
                break
            if row is None:
                break
            destination.execute(insert, tuple(row))
            count += 1
    except sqlite3.DatabaseError as exc:
        error = str(exc)
    return count, error


def _rebuild_object_catalog(destination: sqlite3.Connection, cas_dir: Path) -> int:
    inserted = 0
    for path in cas_dir.rglob("*"):
        if not path.is_file() or path.name.endswith(".tmp"):
            continue
        digest = path.parent.name + path.name
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            continue
        destination.execute(
            """INSERT OR IGNORE INTO objects(digest, byte_length, media_type, created_at)
               VALUES(?, ?, 'application/octet-stream', datetime('now'))""",
            (digest, len(data)),
        )
        inserted += 1
    return inserted


def _rebuild_public_messages(
    destination: sqlite3.Connection, cas_dir: Path,
) -> int:
    sequence_by_session: dict[str, int] = defaultdict(int)
    for session_id, sequence in destination.execute(
        "SELECT session_id, MAX(sequence) + 1 FROM messages GROUP BY session_id"
    ):
        sequence_by_session[str(session_id)] = int(sequence)
    inserted = 0
    rows = destination.execute(
        """SELECT task_id, session_id, contract_ref, created_at
             FROM tasks WHERE session_id IS NOT NULL
             ORDER BY created_at, task_id"""
    ).fetchall()
    for task_id, session_id, contract_ref, created_at in rows:
        if destination.execute(
            "SELECT 1 FROM messages WHERE task_id=? AND role='user' LIMIT 1", (task_id,),
        ).fetchone():
            continue
        contract = _read_json(cas_dir, str(contract_ref or ""))
        content = str((contract or {}).get("task_description") or "").strip()
        if not content:
            continue
        message = {"role": "user", "content": content, "message_type": "conversation"}
        content_ref, byte_length = _put_json(cas_dir, message)
        digest = content_ref.removeprefix("sha256:")
        destination.execute(
            """INSERT OR IGNORE INTO objects(digest, byte_length, media_type, created_at)
               VALUES(?, ?, 'application/json', ?)""",
            (digest, byte_length, created_at),
        )
        sequence = sequence_by_session[str(session_id)]
        sequence_by_session[str(session_id)] += 1
        destination.execute(
            """INSERT OR IGNORE INTO messages(
                   message_id, session_id, task_id, sequence, role,
                   content_ref, reasoning_ref, created_at
               ) VALUES(?, ?, ?, ?, 'user', ?, NULL, ?)""",
            (f"recovered-user:{task_id}", session_id, task_id, sequence, content_ref, created_at),
        )
        inserted += 1
    return inserted


def _rebuild_task_usage(destination: sqlite3.Connection, cas_dir: Path) -> int:
    parents = {
        str(row[0]): (str(row[1] or ""), str(row[2] or ""))
        for row in destination.execute(
            "SELECT task_id, process_id, parent_process_id FROM session_processes WHERE task_id IS NOT NULL"
        )
    }
    inserted = 0
    for row in destination.execute(
        "SELECT task_id, outcome_ref, status, created_at, updated_at FROM tasks"
    ).fetchall():
        task_id, outcome_ref, status, created_at, updated_at = row
        if destination.execute("SELECT 1 FROM task_usage WHERE task_id=?", (task_id,)).fetchone():
            continue
        outcome = _read_json(cas_dir, str(outcome_ref or "")) or {}
        metadata = dict(outcome.get("metadata") or {})
        used = dict(dict(metadata.get("task_tree_budget") or {}).get("used") or {})
        process_id, parent_process_id = parents.get(str(task_id), ("", ""))
        destination.execute(
            """INSERT OR REPLACE INTO task_usage(
                   task_id, process_id, parent_process_id, task_kind, status,
                   provider_calls, input_tokens, output_tokens, cache_read_tokens,
                   cache_write_tokens, duration_ms, tool_calls, created_at, updated_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, process_id, parent_process_id,
                str(metadata.get("task_kind") or ""),
                str(outcome.get("status") or status or ""),
                int(used.get("provider_calls", 0) or 0),
                int(used.get("reported_input_tokens", 0) or 0),
                int(used.get("reported_output_tokens", 0) or 0),
                int(used.get("reported_cache_read_tokens", 0) or 0),
                int(used.get("reported_cache_write_tokens", 0) or 0),
                float(outcome.get("duration_ms", 0.0) or 0.0),
                int(outcome.get("tool_calls_executed", 0) or 0),
                created_at, updated_at,
            ),
        )
        inserted += 1
    return inserted


def recover(source_path: Path, destination_path: Path, cas_dir: Path) -> dict:
    validate_sqlite_runtime()
    source_path = source_path.absolute()
    validate_storage_location(source_path.parent)
    destination_path = destination_path.resolve()
    cas_dir = cas_dir.resolve()
    if destination_path.exists():
        raise FileExistsError(f"destination already exists: {destination_path}")
    if not source_path.is_file() or not cas_dir.is_dir():
        raise FileNotFoundError("source database and CAS directory must exist")

    report: dict = {"source": str(source_path), "destination": str(destination_path)}
    with StorageLease(source_path.parent, exclusive=True), tempfile.TemporaryDirectory(
        prefix="gitgo-sqlite-salvage-"
    ) as temporary_dir:
        shadow = Path(temporary_dir) / "state.sqlite3"
        shutil.copy2(source_path, shadow)
        journals = []
        for suffix in ("-wal", "-journal"):
            member = Path(str(source_path) + suffix)
            if member.exists():
                shutil.copy2(member, Path(str(shadow) + suffix))
                journals.append(suffix)
        report["copied_journals"] = journals
        if not journals:
            report.update(_patch_shadow_header(shadow))
        else:
            report["shadow_header_patched"] = False
        # Only the isolated shadow may be recovered/checkpointed by SQLite.
        # Never use immutable mode: it ignores the copied committed WAL.
        source = sqlite3.connect(
            shadow.as_uri() + "?mode=rw", uri=True,
        )
        source.execute("PRAGMA writable_schema=ON")
        destination = sqlite3.connect(destination_path)
        destination.row_factory = sqlite3.Row
        try:
            StorageRuntime._apply_migrations(destination, STATE_MIGRATIONS)
            destination.execute("PRAGMA foreign_keys=OFF")
            copied: dict[str, dict] = {}
            destination.execute("BEGIN IMMEDIATE")
            for table in COPY_TABLES:
                count, error = _copy_reachable_rows(source, destination, table)
                copied[table] = {"rows": count, "error": error}
            object_count = _rebuild_object_catalog(destination, cas_dir)
            message_count = _rebuild_public_messages(destination, cas_dir)
            usage_count = _rebuild_task_usage(destination, cas_dir)
            destination.execute("COMMIT")
            destination.execute("PRAGMA foreign_keys=ON")
            quick_check = [str(row[0]) for row in destination.execute("PRAGMA integrity_check")]
            foreign_key_errors = [tuple(row) for row in destination.execute("PRAGMA foreign_key_check")]
            if quick_check != ["ok"] or foreign_key_errors:
                raise RuntimeError(
                    f"recovered database failed validation: quick={quick_check}, fk={foreign_key_errors[:5]}"
                )
            destination.execute("PRAGMA journal_mode=WAL")
            destination.execute("PRAGMA synchronous=FULL")
            report.update({
                "copied": copied,
                "objects_registered": object_count,
                "messages_reconstructed": message_count,
                "usage_reconstructed": usage_count,
                "quick_check": quick_check,
                "foreign_key_errors": 0,
            })
        except BaseException:
            try:
                destination.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            destination.close()
            source.close()
            destination_path.unlink(missing_ok=True)
            raise
        destination.close()
        source.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--cas-dir", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(
        recover(args.source, args.destination, args.cas_dir),
        ensure_ascii=False, sort_keys=True, indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
