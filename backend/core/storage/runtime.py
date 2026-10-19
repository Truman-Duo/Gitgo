"""Centralized SQLite writer, quotas, CAS and out-of-band health state."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Literal

from .migrations import OBSERVABILITY_MIGRATIONS, STATE_MIGRATIONS, Migration
from .models import (
    StorageBlocked,
    StorageCorruptionDetected,
    StorageReferenceMissing,
    StorageRuntimeUnsupported,
    StorageHealth,
    StorageHealthLevel,
    StoragePolicy,
)
from .paths import (
    StoragePaths, resolve_existing_storage_paths, resolve_storage_paths,
    validate_storage_location,
    resolve_shared_knowledge_paths,
)
from .redaction import redact_for_persistence
from .maintenance import StorageLease
from .protocol import require_legacy_storage_format
from .cas_inventory import CasReferenceInventory


from .repositories import objects as _objects_repository
from .repositories import custom_tools as _custom_tools_repository
from .repositories import lineage as _lineage_repository
from .repositories import checkpoints as _checkpoints_repository
from .repositories import usage as _usage_repository
from .repositories import sessions as _sessions_repository
from .repositories import presentation as _presentation_repository
from .repositories import recovery as _recovery_repository
from .repositories import worktrees as _worktrees_repository
from .repositories import retirement as _retirement_repository
from .repositories import knowledge as _knowledge_repository
from .repositories import observability as _observability_repository
from .repositories import observation_retention as _observation_retention_repository
from .repositories import cas_maintenance as _cas_maintenance_repository
from .repositories import health as _health_repository
from .repositories import commands as _commands_repository
from .support import _encode_page_cursor, _decode_page_cursor, validate_sqlite_runtime, _utc_now, _sql_literal, _file_family_bytes, _directory_bytes, _estimate_durable_session_tokens, _durable_context_breakdown, _durable_cache_summary, _validate_sqlite_file_shape, _quick_check, _StorageCursor, _StorageConnection, _MinuteBudget


DatabaseName = Literal["state", "observability"]


def _protected_checkpoint_catalogue():
    """Build the fixed trusted catalogue entirely in memory at the facade.

    The selected worker receives immutable schema facts, not a connection or
    an alternate storage path. Stored project schema cannot qualify itself.
    """
    from contextlib import closing
    from .project_schema import GUARD
    from .batch_sql import SCHEMA
    from .batch_inventory import catalogue
    validate_sqlite_runtime()
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        db.execute("PRAGMA temp_store=MEMORY")
        StorageRuntime._apply_migrations(db, STATE_MIGRATIONS)
        for sql in (*GUARD, *SCHEMA):
            db.execute(sql)
        return catalogue(db)


def open_usability_source(project_root: str | Path) -> sqlite3.Connection:
    """Open committed traces read-only without a writer runtime or migrations.

    The caller owns and closes this thread-local connection. Collection must
    not create authoritative databases or acquire a second StorageRuntime.
    """
    validate_sqlite_runtime()
    path = Path(project_root) / "observability.sqlite3"
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)
    try:
        connection.execute("PRAGMA query_only=ON")
    except BaseException:
        connection.close()
        raise
    return connection


def open_usability_statistics(project_root: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open only the separate derived metrics store; never create directories.

    Schema, quotas and retention belong to the collector. SQLite connection
    creation and runtime validation remain inside the storage boundary.
    """
    validate_sqlite_runtime()
    path = Path(project_root) / "usability" / "metrics.sqlite3"
    if read_only:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)
        try:
            connection.execute("PRAGMA query_only=ON")
        except BaseException:
            connection.close()
            raise
        return connection
    return sqlite3.connect(path, timeout=.25)


def read_project_list_status(workspace: str | Path) -> dict | None:
    """Read the list projection without constructing a writer runtime.

    This path deliberately performs no migrations, health scans, maintenance,
    directory creation or observability access. SQLite read-only mode still
    observes a valid WAL, while the short busy timeout keeps one project from
    blocking the project table.
    """
    validate_sqlite_runtime()
    paths = resolve_existing_storage_paths(workspace)
    if paths is None or not paths.state_db.is_file():
        return None
    _validate_sqlite_file_shape(paths.state_db)
    uri = paths.state_db.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0.25)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=250")
        row = connection.execute(
            """SELECT process_id, status, updated_at
               FROM session_processes
               WHERE COALESCE(actor_kind, '') = 'supervisor'
                  OR parent_process_id IS NULL
               ORDER BY updated_at DESC, created_at DESC
               LIMIT 1"""
        ).fetchone()
    except sqlite3.OperationalError as exc:
        # A valid pre-runtime database with no process projection is New. Busy,
        # I/O and malformed-schema errors remain visible to the caller.
        if "no such table" in str(exc).lower():
            return None
        raise
    finally:
        connection.close()
    if row is None:
        return None
    return {
        "process_id": str(row["process_id"] or ""),
        "status": str(row["status"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }
































class StorageRuntime:
    """The only supported SQLite entry point in the backend.

    A runtime owns exactly one writer connection for each database and protects
    them with locks.  Large bodies live in CAS; SQLite rows contain references.
    """

    @staticmethod
    def create_protected_project(registry,processes,*,workspace_hint):
        """Create a new bounded checkpoint store in an installed canonical Host.

        Existing projects, workspace paths and environment configuration do
        not switch routes implicitly. Unqualified operations refuse explicitly.
        """
        from .protected_runtime import ProtectedCheckpoints
        return ProtectedCheckpoints.create(registry,processes,workspace_hint)

    @staticmethod
    def open_protected_project(registry,processes,*,project,slot,workspace_hint):
        """Reopen an exact registered protected project without legacy I/O."""
        from .protected_runtime import ProtectedCheckpoints
        return ProtectedCheckpoints.open(registry,processes,project,slot,workspace_hint)

    def __init__(
        self,
        workspace: str | Path,
        *,
        state_home: str | Path | None = None,
        policy: StoragePolicy | None = None,
        health_listener: Callable[[dict], None] | None = None,
        exclusive_maintenance: bool = False,
        shared_knowledge: bool = False,
    ):
        validate_sqlite_runtime()
        if shared_knowledge:
            if state_home is None:
                raise ValueError("shared knowledge requires an explicit state home")
            self.paths = resolve_shared_knowledge_paths(state_home)
        else:
            self.paths = resolve_storage_paths(workspace, state_home=state_home)
        self.policy = policy or StoragePolicy()
        self._health_listener = health_listener
        self._state_lock = threading.RLock()
        self._observability_lock = threading.RLock()
        self._health_lock = threading.RLock()
        self._cas_lock = threading.RLock()
        self._closed = False
        self._close_started = False
        self._closed_databases: set[str] = set()
        self._last_health: StorageHealth | None = None
        self._last_stderr_at = 0.0
        self._cas_bytes_hint: int | None = None
        self._last_cas_scan_at = 0.0
        self._last_metric_sample_at = 0.0
        self._last_integrity_check_at = 0.0
        self._integrity_reasons: dict[str, str] = {}
        self._cas_reference_reasons: dict[str, str] = {}
        self._last_cas_reference_check_at = 0.0
        self._last_cas_gc_at = time.monotonic()
        self._transient_degradations: dict[str, float] = {}
        self._observability_budget = _MinuteBudget(
            self.policy.observability_transactions_per_minute,
            self.policy.observability_logical_bytes_per_minute,
        )
        self._global_sqlite_budget = _MinuteBudget(
            self.policy.sqlite_transactions_per_minute,
            self.policy.sqlite_estimated_bytes_per_minute,
        )
        self._exclusive_maintenance = exclusive_maintenance
        self._maintenance_lease = StorageLease(self.paths.project_root, exclusive=exclusive_maintenance)
        if (self.paths.project_root / "project-deleted.json").exists():
            self._maintenance_lease.close()
            raise StorageBlocked("PROJECT_DELETED: this project store was explicitly retired")
        if (self.paths.project_root / "storage-recovery-in-progress.json").exists():
            self._maintenance_lease.close()
            raise StorageBlocked(
                "STORAGE_RECOVERY_INCOMPLETE: explicit recovery did not finish; "
                "inspect its manifest and backup before reopening this project"
            )
        try:
            self._state = self._open_database(
                self.paths.state_db, STATE_MIGRATIONS, self.policy.state_max_bytes,
                authoritative=True,
            )
            self._observability = self._open_database(
                self.paths.observability_db,
                OBSERVABILITY_MIGRATIONS,
                self.policy.observability_max_bytes,
                authoritative=False,
            )
            validate_storage_location(self.paths.project_root)
            now = _utc_now()
            with self._state_lock:
                self._state.execute(
                    """INSERT INTO projects(project_id, workspace_hint, created_at, updated_at)
                       VALUES(?, ?, ?, ?)
                       ON CONFLICT(project_id) DO UPDATE SET
                           workspace_hint=excluded.workspace_hint,
                           updated_at=excluded.updated_at""",
                    (self.paths.project_id, str(self.paths.workspace), now, now),
                )
            self.check_health(force_publish=True)
            self.maintain_observability()
        except BaseException as exc:
            # Construction failure must not leave yesterday's green health
            # snapshot behind, or leak a successfully opened first database.
            for name in ("_state", "_observability"):
                connection = getattr(self, name, None)
                if connection is not None:
                    try:
                        connection.fault_callback = None
                        connection.close()
                    except sqlite3.Error:
                        pass
            self._closed = True
            self._maintenance_lease.close()
            health = StorageHealth(StorageHealthLevel.BLOCKED, _utc_now(), self.paths.project_id,
                0, 0, 0, 0, 0, ("storage_open_failed:" + str(exc)[:500],))
            try:
                self._write_health_snapshot(health)
                if self._health_listener:
                    self._health_listener(health.to_dict())
            except Exception:
                pass  # Failure reporting cannot replace the original cause.
            self._stderr_health(health)
            raise
    def _open_database(
        self,
        path: Path,
        migrations: Iterable[Migration],
        max_bytes: int,
        *,
        authoritative: bool,
    ) -> sqlite3.Connection:
        migrations = tuple(migrations)
        _validate_sqlite_file_shape(path)
        is_new = not path.exists()
        connection = sqlite3.connect(
            path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
            factory=_StorageConnection,
        )
        connection.row_factory = sqlite3.Row
        try:
            try:
                require_legacy_storage_format(connection, migrations)
            except sqlite3.DatabaseError as error:
                code = getattr(error, "sqlite_errorcode", 0) & 0xFF
                if code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
                    raise StorageCorruptionDetected(str(path), str(error)[:500]) from error
                raise
            if not is_new:
                integrity_error = _quick_check(connection)
                if integrity_error:
                    raise StorageCorruptionDetected(str(path), integrity_error)
            connection.execute("PRAGMA foreign_keys=ON")
            if hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"):
                connection.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            # Authoritative task/session state favors durability. High-volume,
            # reconstructible observability remains NORMAL to avoid recreating
            # the trace-write amplification failure this subsystem was built to
            # prevent.
            connection.execute(
                "PRAGMA synchronous=" + ("FULL" if authoritative else "NORMAL")
            )
            connection.execute("PRAGMA wal_autocheckpoint=1000")
            connection.execute("PRAGMA journal_size_limit=67108864")
            connection.execute("PRAGMA cache_size=-4096")
            connection.execute("PRAGMA mmap_size=0")
            if is_new:
                connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            connection.execute(f"PRAGMA max_page_count={max(1, max_bytes // page_size)}")
            self._apply_migrations(connection, migrations)
            integrity_error = _quick_check(connection)
            if integrity_error:
                raise StorageCorruptionDetected(str(path), integrity_error)
            connection.fault_callback = lambda error: self._record_database_fault(
                "state" if authoritative else "observability", path, error,
            )
            return connection
        except BaseException:
            connection.close()
            raise

    def _record_database_fault(self, name: str, path: Path, error: sqlite3.DatabaseError) -> None:
        # SQLITE_BUSY, constraint errors and cancellation are not corruption.
        code = getattr(error, "sqlite_errorcode", 0) & 0xFF
        if code not in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
            return
        reason = str(error)[:500]
        with self._health_lock:
            changed = self._integrity_reasons.get(name) != reason
            self._integrity_reasons[name] = reason
            health = self._health_from_measurement()
            self._last_health = health
            if changed:
                try:
                    self._write_health_snapshot(health)
                    if self._health_listener:
                        self._health_listener({"event": "storage_health", "storage": health.to_dict()})
                except Exception:
                    pass  # Preserve the actual data error even if its observer fails.
        raise StorageCorruptionDetected(str(path), reason) from error

    @staticmethod
    def _apply_migrations(
        connection: sqlite3.Connection, migrations: Iterable[Migration]
    ) -> None:
        migrations = tuple(migrations)
        require_legacy_storage_format(connection, migrations)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version INTEGER PRIMARY KEY,
                   name TEXT NOT NULL,
                   checksum TEXT NOT NULL,
                   applied_at TEXT NOT NULL
               )"""
        )
        applied = {
            int(row["version"]): str(row["checksum"])
            for row in connection.execute(
                "SELECT version, checksum FROM schema_migrations"
            ).fetchall()
        }
        for migration in migrations:
            existing = applied.get(migration.version)
            if existing is not None:
                if existing != migration.checksum:
                    raise RuntimeError(
                        f"Migration checksum mismatch at version {migration.version}"
                    )
                continue
            script = (
                "BEGIN IMMEDIATE;\n"
                + migration.sql
                + "\nINSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES("
                + str(migration.version)
                + ","
                + _sql_literal(migration.name)
                + ","
                + _sql_literal(migration.checksum)
                + ","
                + _sql_literal(_utc_now())
                + ");\nCOMMIT;"
            )
            try:
                connection.executescript(script)
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("StorageRuntime is closed")
        if self._close_started:
            raise StorageBlocked('STORAGE_RUNTIME_CLOSING')
        self._maintenance_lease.require_active()

    @staticmethod
    def require_legacy_state_file(path: Path) -> None:
        """Offline tools must admit the source before copying or resetting it."""
        validate_sqlite_runtime()
        connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25,
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            require_legacy_storage_format(connection, STATE_MIGRATIONS)
        finally:
            connection.close()

    def set_health_listener(self, listener: Callable[[dict], None] | None) -> None:
        """Replace the event sink after the Daemon queue becomes available."""
        with self._health_lock:
            self._health_listener = listener

    def _ensure_authoritative_capacity(self, logical_bytes: int = 0) -> None:
        health = self.check_health()
        if any(
            reason.startswith("state_integrity_failed:")
            for reason in health.reasons
        ):
            raise StorageBlocked("state_database_integrity_failed")
        projected = health.state_bytes + max(0, logical_bytes)
        if health.free_bytes < self.policy.minimum_free_bytes:
            raise StorageBlocked("insufficient_free_space")
        if projected > self.policy.state_max_bytes:
            raise StorageBlocked("state_database_capacity_exceeded")
        if health.total_bytes + max(0, logical_bytes) > self.policy.total_max_bytes:
            raise StorageBlocked("project_storage_capacity_exceeded")

    def _estimated_sqlite_write(self, logical_bytes: int) -> int:
        # WAL writes occur in pages and may be rewritten by checkpoints.  A
        # conservative reserve makes the rate cap track likely physical writes
        # instead of trusting the much smaller JSON/string payload size.
        return max(4096, logical_bytes * self.policy.sqlite_write_amplification_reserve)

    def _reserve_authoritative_write(self, logical_bytes: int) -> None:
        self._state.require_available()
        self._ensure_authoritative_capacity(self._estimated_sqlite_write(logical_bytes))
        if not self._global_sqlite_budget.reserve(
            self._estimated_sqlite_write(logical_bytes)
        ):
            self._publish_degraded("global_sqlite_write_rate_exceeded")
            raise StorageBlocked("global_sqlite_write_rate_exceeded")

    def put_state_ref(self, namespace: str, key: str, value_ref: str) -> None:
        'Store a small authoritative reference, never a large inline body.'
        return _objects_repository.put_state_ref(self, namespace, key, value_ref)

    def get_state_ref(self, namespace: str, key: str) -> str | None:
        return _objects_repository.get_state_ref(self, namespace, key)

    def commit_state_revision(self, namespace: str, key: str, value_ref: str, *,
                              expected_ref: str | None, revision_key: str) -> bool:
        return _objects_repository.commit_state_revision(
            self, namespace, key, value_ref, expected_ref=expected_ref, revision_key=revision_key)

    def list_state_refs(self, namespace: str, *, prefix: str, after: str = "", limit: int = 50) -> list[dict]:
        return _objects_repository.list_state_refs(self, namespace, prefix=prefix, after=after, limit=limit)

    def _write_blob_file(self, data: bytes, *, media_type: str='application/octet-stream') -> dict:
        'Write one immutable file without opening a SQLite transaction.'
        return _objects_repository._write_blob_file(self, data, media_type=media_type)

    @staticmethod
    def _encoded_object(value: object) -> bytes:
        return _objects_repository._encoded_object(value)

    @staticmethod
    def _register_object(connection: sqlite3.Connection, descriptor: dict) -> None:
        return _objects_repository._register_object(connection, descriptor)

    def put_blob(self, data: bytes, *, media_type: str='application/octet-stream') -> str:
        'Write an immutable plaintext CAS object once and return its SHA-256 ref.'
        return _objects_repository.put_blob(self, data, media_type=media_type)

    def read_blob(self, ref: str, *, repair: bool = True) -> bytes:
        return _objects_repository.read_blob(self, ref, repair=repair)

    def _restore_canonical_blob(self, digest: str) -> bytes | None:
        'Restore only tiny canonical JSON values proven by their digest.'
        return _objects_repository._restore_canonical_blob(self, digest)

    def put_tool_result(self, content: str, *, process_id: str='', task_id: str='', tool_name: str='', media_type: str='application/json') -> dict:
        'Persist one oversized tool result as a content-addressed object.'
        return _objects_repository.put_tool_result(self, content, process_id=process_id, task_id=task_id, tool_name=tool_name, media_type=media_type)

    def read_tool_result(self, locator: str, *, offset: int=0, max_chars: int=24000, json_pointer: str='', query: str='', owner_process_id: str='') -> dict:
        'Read a bounded page from an addressable tool result.'
        return _objects_repository.read_tool_result(self, locator, offset=offset, max_chars=max_chars, json_pointer=json_pointer, query=query, owner_process_id=owner_process_id)

    def save_custom_tool(self, spec: dict, source: str, *, replace: bool=False) -> dict:
        'Persist one project-scoped authored tool as an immutable version.'
        return _custom_tools_repository.save_custom_tool(self, spec, source, replace=replace)

    def list_custom_tools(self, *, include_archived: bool=False) -> dict:
        'Return bounded catalog metadata without exposing stored source.'
        return _custom_tools_repository.list_custom_tools(self, include_archived=include_archived)

    def load_custom_tool(self, name: str, *, version: int | None=None) -> dict:
        'Load a saved version for an explicit task-local mount.'
        return _custom_tools_repository.load_custom_tool(self, name, version=version)

    def set_custom_tool_archived(self, name: str, archived: bool=True) -> dict:
        'Archive or restore an asset; immutable versions remain recoverable.'
        return _custom_tools_repository.set_custom_tool_archived(self, name, archived)

    def create_session_lineage_checkpoint(self, snapshot: dict, *, session_id: str, process_id: str, turn_id: str) -> dict:
        'Append an immutable pre-turn snapshot and advance the undo cursor.'
        return _lineage_repository.create_session_lineage_checkpoint(self, snapshot, session_id=session_id, process_id=process_id, turn_id=turn_id)

    def preview_session_undo(self, session_id: str) -> dict:
        'Describe the next branch rewind without mutating authoritative state.'
        return _lineage_repository.preview_session_undo(self, session_id)

    def commit_session_undo(self, session_id: str, checkpoint_id: str) -> dict:
        'Move the undo cursor to its parent and return the immutable target.'
        return _lineage_repository.commit_session_undo(self, session_id, checkpoint_id)

    def _read_json_blob(self, ref: str, *, repair: bool = True) -> object:
        return json.loads(self.read_blob(ref, repair=repair).decode("utf-8"))

    def save_agent_checkpoint(self, snapshot: dict) -> str:
        'Atomically persist one process/session/task boundary.'
        return _checkpoints_repository.save_agent_checkpoint(self, snapshot)

    def process_revision(self, process_id: str) -> int:
        self._ensure_open()
        with self._state_lock:
            from .lifecycle import check_revision
            return check_revision(self._state, process_id, None)

    def read_task_usage(self, *, limit: int=100, cursor: str='', day_offset_minutes: int=0) -> dict:
        'Return authoritative root-task usage without reading verbose traces.'
        return _usage_repository.read_task_usage(self, limit=limit, cursor=cursor, day_offset_minutes=day_offset_minutes)

    def append_session_event(self, process_id: str, event_type: str, data: dict | None=None, *, occurred_at: str | None=None, sequence: int | None=None, event_id: str | None=None) -> int:
        return _sessions_repository.append_session_event(self, process_id, event_type, data, occurred_at=occurred_at, sequence=sequence, event_id=event_id)

    def load_agent_session_state(self, process_id: str) -> dict | None:
        return _sessions_repository.load_agent_session_state(self, process_id)

    @staticmethod
    def _is_public_user_message(message: dict) -> bool:
        'Return whether a provider-visible user message belongs in the UI chat.'
        return _presentation_repository._is_public_user_message(message)

    @staticmethod
    def _storage_projection_warning(exc: StorageReferenceMissing, *, turn_id: str='') -> dict:
        'Visible placeholder for damaged history without inventing content.'
        return _presentation_repository._storage_projection_warning(exc, turn_id=turn_id)

    def _public_session_messages(self, session_id: str) -> list[dict]:
        'Project one durable provider session into the canonical public chat.'
        return _presentation_repository._public_session_messages(self, session_id)

    def read_latest_conversations(self, *, agent_conversations: dict | None = None) -> dict:
        'Return the latest durable root conversation and its Agent tree.'
        return _presentation_repository.read_latest_conversations(self, agent_conversations=agent_conversations)

    def read_process_presentation(self, process_id: str) -> dict:
        return _presentation_repository.read_process_presentation(self, process_id)

    def read_process_presentations(self) -> dict[str, dict]:
        return _presentation_repository.read_process_presentations(self)

    def update_process_presentation(self, process_id: str, *, display_name: str | None=None, archived: bool | None=None) -> dict:
        'Small independent fields: checkpoints never overwrite user choices.'
        return _presentation_repository.update_process_presentation(self, process_id, display_name=display_name, archived=archived)

    def read_project_b_processes(self) -> dict[str, dict]:
        'Return every durable B process for this project, across all tasks.'
        return _presentation_repository.read_project_b_processes(self)

    def read_latest_root_process(self) -> dict | None:
        'Return the latest durable A control projection without starting a Daemon.'
        return _presentation_repository.read_latest_root_process(self)

    def read_latest_root_status(self) -> dict | None:
        'Cheap project-list projection with no CAS/session materialisation.'
        return _presentation_repository.read_latest_root_status(self)

    def read_project_b_conversations(self) -> dict[str, list[dict]]:
        'Return public durable transcripts for every B in the project.'
        return _presentation_repository.read_project_b_conversations(self)

    def load_agent_process_state(self, process_id: str) -> dict | None:
        'Return task/runtime state alongside the restored session snapshot.'
        return _recovery_repository.load_agent_process_state(self, process_id)

    def load_latest_root_process_state(self) -> dict | None:
        'Load the latest durable A process, including terminal sessions.'
        return _recovery_repository.load_latest_root_process_state(self)

    def save_worktree_state(self, record: dict) -> None:
        'Persist one low-frequency Agent worktree lifecycle transition.'
        return _worktrees_repository.save_worktree_state(self, record)

    def load_worktree_state(self, process_id: str) -> dict | None:
        return _worktrees_repository.load_worktree_state(self, process_id)

    def list_worktree_states(self, *, active_only: bool=False) -> list[dict]:
        return _worktrees_repository.list_worktree_states(self, active_only=active_only)

    def list_incomplete_processes(self) -> list[str]:
        return _retirement_repository.list_incomplete_processes(self)

    def list_agent_process_links(self) -> list[dict]:
        'Return the minimal topology needed to rebuild a recovery tree.'
        return _retirement_repository.list_agent_process_links(self)

    def session_events_since_checkpoint(self, process_id: str) -> int:
        return _retirement_repository.session_events_since_checkpoint(self, process_id)

    def delete_process_session(self, process_id: str) -> None:
        return _retirement_repository.delete_process_session(self, process_id)

    def is_process_deleted(self, process_id: str) -> bool:
        return _retirement_repository.is_process_deleted(self, process_id)

    def read_deleted_process_ids(self) -> set[str]:
        return _retirement_repository.read_deleted_process_ids(self)

    def purge_b_session(self, session_id: str, plan_id: str) -> None:
        'Remove all executions of one B session under exclusive maintenance.'
        return _retirement_repository.purge_b_session(self, session_id, plan_id)

    def create_deletion_plan(self, manifest: dict, *, not_before: float) -> dict:
        return _retirement_repository.create_deletion_plan(self, manifest, not_before=not_before)

    def read_deletion_plan(self, plan_id: str) -> dict:
        return _retirement_repository.read_deletion_plan(self, plan_id)

    def list_deletion_plans(self, *, limit: int=100, due_only: bool=False) -> list[dict]:
        return _retirement_repository.list_deletion_plans(self, limit=limit, due_only=due_only)

    def transition_deletion_plan(self, plan_id: str, expected: str, state: str, *, error: str='', not_before: float | None=None) -> dict:
        return _retirement_repository.transition_deletion_plan(self, plan_id, expected, state, error=error, not_before=not_before)

    def session_storage_counts(self) -> dict[str, int]:
        'Small diagnostic surface used by migration verification and tests.'
        return _retirement_repository.session_storage_counts(self)

    def _prepare_state_json(self, value: object, *, media_type: str) -> dict:
        encoded = self._encoded_object(value)
        return self._write_blob_file(encoded, media_type=media_type)

    def append_history_record(self, record: dict, *, event_id: str | None=None, compact_threshold: int=400, keep_entries: int=200) -> str:
        'Append one authoritative audit event in one bounded transaction.'
        return _knowledge_repository.append_history_record(self, record, event_id=event_id, compact_threshold=compact_threshold, keep_entries=keep_entries)

    def replace_history_records(self, records: list[dict]) -> None:
        'Atomically replace History for compatibility imports and tests.'
        return _knowledge_repository.replace_history_records(self, records)

    def load_history_records(self, *, limit: int | None=None) -> list[dict]:
        return _knowledge_repository.load_history_records(self, limit=limit)

    def history_count(self) -> int:
        return _knowledge_repository.history_count(self)

    def upsert_lesson_record(self, record: dict, *, scope: str, content_hash: str) -> str:
        'Insert or update one lesson; pending exact duplicates are idempotent.'
        return _knowledge_repository.upsert_lesson_record(self, record, scope=scope, content_hash=content_hash)

    def load_lesson_records(self, *, scope: str, project_name: str | None=None, tech_stack: str | None=None) -> list[dict]:
        return _knowledge_repository.load_lesson_records(self, scope=scope, project_name=project_name, tech_stack=tech_stack)

    def get_lesson_record(self, lesson_id: str) -> tuple[str, dict] | None:
        return _knowledge_repository.get_lesson_record(self, lesson_id)

    def configure_lesson_checker(self, change: dict, *, grant_id: str) -> dict:
        return _knowledge_repository.configure_lesson_checker(self,change,grant_id=grant_id)

    def record_lesson_retrieval(self, lesson_id: str, retrieved_at: str) -> bool:
        return _knowledge_repository.record_lesson_retrieval(self, lesson_id, retrieved_at)

    def record_lesson_verification(self, lesson_id: str, project_id: str, project_name: str, *, user_confirmation: bool = False, expected_version: str = "") -> dict:
        return _knowledge_repository.record_lesson_verification(self, lesson_id, project_id, project_name, user_confirmation=user_confirmation, expected_version=expected_version)

    def delete_lesson_record(self, lesson_id: str, *, project_name: str | None=None, scopes: tuple[str, ...]=('pending', 'instance')) -> bool:
        return _knowledge_repository.delete_lesson_record(self, lesson_id, project_name=project_name, scopes=scopes)

    def lesson_count(self, *, scope: str, project_name: str='') -> int:
        return _knowledge_repository.lesson_count(self, scope=scope, project_name=project_name)

    def save_harvest_signal_records(self, records: list[dict], *, create_only: bool = False) -> int:
        'Upsert changed harvest signals into the shared governance bus.'
        return _knowledge_repository.save_harvest_signal_records(self, records, create_only=create_only)

    def load_harvest_signal_records(self, *, with_version: bool = False) -> list[dict]:
        return _knowledge_repository.load_harvest_signal_records(self, with_version=with_version)

    def resolve_harvest_proposal_records(self, project: str, proposal_id: str, *, accepted: bool,
                                         candidates: list[dict] | None, expected_version: str) -> dict:
        return _knowledge_repository.resolve_harvest_proposal_records(self, project, proposal_id,
            accepted=accepted, candidates=candidates, expected_version=expected_version)

    def harvest_signal_count(self) -> int:
        return _knowledge_repository.harvest_signal_count(self)

    def append_event(self, *, event_type: str, summary: str, severity: str='info', task_id: str | None=None, session_id: str | None=None, sequence: int | None=None, payload_ref: str | None=None, occurred_at: str | None=None) -> bool:
        'Append a bounded semantic event.'
        return _observability_repository.append_event(self, event_type=event_type, summary=summary, severity=severity, task_id=task_id, session_id=session_id, sequence=sequence, payload_ref=payload_ref, occurred_at=occurred_at)

    def _reserve_observability_write(self, logical: int, *, publish_degraded: bool=True) -> bool:
        'Reserve one optional observability transaction.'
        return _observability_repository._reserve_observability_write(self, logical, publish_degraded=publish_degraded)

    def trace_last_sequence(self, trace_id: str) -> int:
        return _observability_repository.trace_last_sequence(self, trace_id)

    def append_trace_record(self, trace_id: str, record: dict, *, detail: object | None=None) -> dict | None:
        'Persist one bounded trace record in the observability database.'
        return _observability_repository.append_trace_record(self, trace_id, record, detail=detail)

    def read_trace_activity(self, event_types: Iterable[str], *, limit: int=20, before: tuple[str, str, int] | None=None) -> list[dict]:
        'Bounded read projection of existing events; does not copy them into History.'
        return _observability_repository.read_trace_activity(self, event_types, limit=limit, before=before)

    def read_trace_events(self, trace_id: str, *, after_seq: int=0, limit: int=500, process_id: str='', include_deltas: bool=True) -> dict:
        return _observability_repository.read_trace_events(self, trace_id, after_seq=after_seq, limit=limit, process_id=process_id, include_deltas=include_deltas)

    def read_trace_summary(self, trace_id: str, *, process_id: str='') -> dict:
        'Return exact semantic-event counts without materializing a verbose trace.'
        return _observability_repository.read_trace_summary(self, trace_id, process_id=process_id)

    def import_trace_records(self, trace_id: str, records: list[tuple[dict, object | None]]) -> int | None:
        'Import one legacy trace in bounded, idempotent transactions.'
        return _observability_repository.import_trace_records(self, trace_id, records)

    def list_traces(self, *, limit: int=100) -> list[dict]:
        return _observability_repository.list_traces(self, limit=limit)

    def observability_counts(self) -> dict[str, int]:
        'Bounded diagnostic surface; callers never receive a connection.'
        return _observability_repository.observability_counts(self)

    def read_trace_detail(self, ref: str) -> object:
        return _observability_repository.read_trace_detail(self, ref)

    def record_storage_metric(self, *, force: bool=False) -> bool:
        'Sample physical file-family sizes at a low, monotonic cadence.'
        return _observability_repository.record_storage_metric(self, force=force)

    def maintain_observability(self, *, force: bool=False) -> dict:
        'Run bounded retention at startup or an explicit idle boundary.'
        return _observation_retention_repository.maintain_observability(self, force=force)

    @staticmethod
    def _digest_from_ref(value: object) -> str | None:
        return _cas_maintenance_repository._digest_from_ref(value)

    def _referenced_cas_digests(self) -> set[str]:
        'Mark the transitive CAS closure used by current durable state.'
        return _cas_maintenance_repository._referenced_cas_digests(self)

    @staticmethod
    def _nested_cas_digests(value: object) -> set[str]:
        return _cas_maintenance_repository._nested_cas_digests(value)

    def _cas_reference_inventory(self) -> CasReferenceInventory:
        'Return direct-column, explicit-owner and legacy nested references.'
        return _cas_maintenance_repository._cas_reference_inventory(self)

    def _refresh_cas_reference_status(self, *, force: bool=False) -> None:
        'Audit live references without scanning unrelated CAS objects.'
        return _cas_maintenance_repository._refresh_cas_reference_status(self, force=force)

    def maintain_cas(self, *, force: bool=False) -> dict:
        'Bounded mark-and-sweep with a grace period for crash orphans.'
        return _cas_maintenance_repository.maintain_cas(self, force=force)

    def _measure(self) -> tuple[int, int, int, int]:
        return _health_repository._measure(self)

    def _health_from_measurement(self) -> StorageHealth:
        return _health_repository._health_from_measurement(self)

    def check_health(self, *, force_publish: bool=False) -> StorageHealth:
        return _health_repository.check_health(self, force_publish=force_publish)

    def _refresh_integrity_status(self, *, force: bool=False) -> None:
        'Low-frequency corruption detection; never run on every write.'
        return _health_repository._refresh_integrity_status(self, force=force)

    def _publish_degraded(self, reason: str) -> None:
        return _health_repository._publish_degraded(self, reason)

    def _write_health_snapshot(self, health: StorageHealth) -> None:
        return _health_repository._write_health_snapshot(self, health)

    def _stderr_health(self, health: StorageHealth) -> None:
        return _health_repository._stderr_health(self, health)

    def diagnostics(self) -> dict:
        return _health_repository.diagnostics(self)

    def close(self) -> None:
        # Admission and borrowing share the lease mutex. Reject before any
        # maintenance or connection teardown if an operation is still active.
        with self._maintenance_lease.shutdown() as admitted:
            if not admitted:
                return
            errors = self._close_storage_resources()
        # Diagnostic/checkpoint errors do not retain a lease once both writer
        # handles have definitely closed. Cleanup failures do (and can retry).
        if errors:
            raise errors[0]

    def _close_storage_resources(self) -> list[BaseException]:
        if self._closed:
            return []
        errors: list[BaseException] = []
        # Shutdown is a natural low-frequency boundary.  A short-lived process
        # records nothing; a long-lived daemon contributes at most one cadence
        # sample and one bounded retention batch before the passive checkpoint.
        try:
            if (
                not self._close_started and not self._integrity_reasons
                and not self._state.quarantined and not self._observability.quarantined
            ):
                self.record_storage_metric()
                self.maintain_observability()
                self.maintain_cas()
        except BaseException:
            try:
                self._publish_degraded("observability_maintenance_failed")
            except BaseException:
                pass
        self._close_started = True
        close_errors: list[BaseException] = []
        with self._state_lock, self._observability_lock:
            for name, connection in (("state", self._state), ("observability", self._observability)):
                if name in self._closed_databases:
                    continue
                try:
                    if name not in self._integrity_reasons and not connection.quarantined:
                        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except BaseException as exc:
                    errors.append(exc)
                try:
                    connection.fault_callback = None
                    connection.close()
                    self._closed_databases.add(name)
                except BaseException as exc:
                    close_errors.append(exc)
        if close_errors:
            # shutdown() retains the maintenance barrier and blocks borrowing.
            # Retry only handles whose close has not returned successfully.
            raise close_errors[0]
        self._closed = True
        # One final out-of-band snapshot records post-checkpoint file-family
        # sizes.  This is a single shutdown write, not a per-poll metric log.
        try:
            with self._health_lock:
                health = self._health_from_measurement()
                self._last_health = health
                self._write_health_snapshot(health)
        except BaseException as exc:
            errors.append(exc)
        return errors

    def __enter__(self) -> "StorageRuntime":
        return self

    def claim_command(self, key: str, fingerprint: str, *, process_id: str = "") -> dict:
        return _commands_repository.claim_command(self, key, fingerprint, process_id=process_id)

    def lookup_command(self, key: str, fingerprint: str) -> dict:
        """Read committed identity only; absence never grants execution."""
        return _commands_repository.lookup_command(self, key, fingerprint)

    @contextmanager
    def _committed_state_reader(self):
        """Own a fresh read-only connection; never run migrations or repair CAS."""
        self._ensure_open()
        connection = sqlite3.connect(self.paths.state_db.as_uri() + '?mode=ro',
                                     uri=True, timeout=1, isolation_level=None)
        try:
            connection.execute('PRAGMA query_only=ON')
            yield connection
        finally:
            connection.close()

    def complete_command(self, key: str, fingerprint: str, result: dict) -> None:
        _commands_repository.complete_command(self, key, fingerprint, result)

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()
