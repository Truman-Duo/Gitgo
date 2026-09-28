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
)
from .redaction import redact_for_persistence
from .maintenance import StorageLease


DatabaseName = Literal["state", "observability"]


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


def _encode_page_cursor(updated_at: str, task_id: str) -> str:
    payload = json.dumps([str(updated_at), str(task_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_page_cursor(cursor: str) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding).decode())
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        return str(value[0]), str(value[1])
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid task usage cursor") from exc


def validate_sqlite_runtime(version: tuple[int, ...] | None = None) -> None:
    """Reject known unsafe WAL engines, including in alternate entry points.

    SQLite's WAL-reset fix is in 3.51.3+ and the 3.50.7/3.44.6 backports.
    https://www.sqlite.org/wal.html#walreset
    This guard deliberately has no environment-variable bypass.
    """
    actual = tuple(version if version is not None else sqlite3.sqlite_version_info)
    safe = actual >= (3, 51, 3) or (
        actual[:2] == (3, 50) and actual >= (3, 50, 7)
    ) or (actual[:2] == (3, 44) and actual >= (3, 44, 6))
    if not safe:
        raise StorageRuntimeUnsupported(".".join(map(str, actual)))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _file_family_bytes(path: Path) -> int:
    total = 0
    for member in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            total += member.stat().st_size
        except FileNotFoundError:
            pass
    return total


def _directory_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for root, _directories, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except (FileNotFoundError, PermissionError):
                continue
    return total


def _estimate_durable_session_tokens(session: dict) -> int:
    messages = list(session.get("messages") or [])
    provider_state = dict(session.get("provider_state") or {})
    total = sum(len(str(item.get("content") or "")) for item in messages if isinstance(item, dict))
    total += sum(len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
                 for value in provider_state.values())
    return max(1, total // 4)


def _durable_context_breakdown(session: dict) -> dict:
    """Reconstruct the same ownership view as AgentSession after a restart."""
    sections: dict[str, int] = {}

    def add(name: str, value: int) -> None:
        amount = max(0, int(value))
        if amount:
            sections[name] = sections.get(name, 0) + amount

    for message in list(session.get("messages") or []):
        if not isinstance(message, dict):
            continue
        content_tokens = max(0, len(str(message.get("content") or "")) // 4)
        prompt_sections = list(message.get("prompt_sections") or [])
        if prompt_sections:
            measured = 0
            for item in prompt_sections:
                if not isinstance(item, dict):
                    continue
                value = max(0, int(item.get("estimated_tokens", 0) or 0))
                measured += value
                add(str(item.get("name") or "Prompt"), value)
            if content_tokens > measured:
                add("Prompt framing", content_tokens - measured)
            continue
        message_type = str(message.get("message_type") or "")
        role = str(message.get("role") or "")
        if message_type == "tool_result" or role == "tool":
            area = "Tool results"
        elif message_type in {"context_checkpoint", "context_summary"}:
            area = "Context checkpoints"
        elif message_type.startswith("host_") or message_type.endswith("steering"):
            area = "Host steering"
        elif role == "user":
            area = "User conversation"
        elif role == "assistant":
            area = "Assistant conversation"
        else:
            area = "Other history"
        add(area, content_tokens)
    provider_tokens = sum(
        len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
        for value in dict(session.get("provider_state") or {}).values()
    ) // 4
    add("Provider continuation", provider_tokens)
    metadata = dict(session.get("session_metadata") or session)
    inventory = dict(metadata.get("context_inventory") or {})
    add("Tool schemas", int(inventory.get("tool_schema_tokens", 0) or 0))
    total = max(1, sum(sections.values()))
    return {
        "estimated_tokens": total,
        "sections": [
            {"name": name, "estimated_tokens": value,
             "ratio": round(value / total, 6)}
            for name, value in sorted(
                sections.items(), key=lambda item: (-item[1], item[0])
            )
        ],
    }


def _durable_cache_summary(session: dict) -> dict:
    telemetry = list(session.get("cache_telemetry") or [])
    total_input = sum(int(item.get("input_tokens", 0) or 0) for item in telemetry)
    total_read = sum(int(item.get("cache_read_tokens", 0) or 0) for item in telemetry)
    eligible = [item for item in telemetry if item.get("eligible_for_reuse")]
    eligible_input = sum(int(item.get("input_tokens", 0) or 0) for item in eligible)
    eligible_read = sum(int(item.get("cache_read_tokens", 0) or 0) for item in eligible)
    return {
        "input_tokens": total_input,
        "cache_read_tokens": total_read,
        "raw_hit_ratio": round(total_read / total_input, 4) if total_input else 0.0,
        "eligible_input_tokens": eligible_input,
        "eligible_cache_read_tokens": eligible_read,
        "eligible_hit_ratio": round(eligible_read / eligible_input, 4) if eligible_input else 0.0,
        "miss_reasons": [
            str(item.get("miss_reason") or "") for item in telemetry
            if item.get("miss_reason")
        ],
    }


def _validate_sqlite_file_shape(path: Path) -> None:
    """Reject an impossible main-file shape before SQLite can mutate it.

    A WAL may legitimately contain pages beyond the current main file. Without
    a WAL, however, a header page count larger than the physical file is proof
    of truncation. The check is constant-time and leaves recovery to an explicit
    operator path; startup must never silently replace authoritative state.
    """
    if not path.exists() or path.stat().st_size == 0:
        return
    size = path.stat().st_size
    if size < 100:
        raise StorageCorruptionDetected(str(path), f"file is only {size} bytes")
    with path.open("rb") as handle:
        header = handle.read(100)
    if header[:16] != b"SQLite format 3\x00":
        raise StorageCorruptionDetected(str(path), "invalid SQLite header")
    encoded_page_size = int.from_bytes(header[16:18], "big")
    page_size = 65536 if encoded_page_size == 1 else encoded_page_size
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise StorageCorruptionDetected(str(path), f"invalid page size {page_size}")
    if size % page_size:
        raise StorageCorruptionDetected(
            str(path), f"file size {size} is not page-aligned ({page_size})",
        )
    declared_pages = int.from_bytes(header[28:32], "big")
    physical_pages = size // page_size
    wal_path = Path(str(path) + "-wal")
    wal_present = wal_path.exists() and wal_path.stat().st_size > 32
    if declared_pages > physical_pages and not wal_present:
        raise StorageCorruptionDetected(
            str(path),
            f"header declares {declared_pages} pages but only {physical_pages} exist and no WAL can recover them",
        )


def _quick_check(connection: sqlite3.Connection) -> str:
    """Return an empty string for a healthy database, else a bounded reason."""
    try:
        rows = connection.execute("PRAGMA quick_check(1)").fetchall()
    except (sqlite3.DatabaseError, StorageCorruptionDetected) as exc:
        return f"{type(exc).__name__}: {exc}"[:500]
    values = [str(row[0]) for row in rows]
    if values == ["ok"]:
        return ""
    return "; ".join(values)[:500] or "quick_check_returned_no_result"


class _StorageCursor(sqlite3.Cursor):
    """Report corruption on execute AND deferred row reads through one boundary."""

    def _checked(self, method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except sqlite3.DatabaseError as exc:
            callback = getattr(self.connection, "fault_callback", None)
            if callback is not None:
                callback(exc)
            raise

    def execute(self, *args, **kwargs):
        return self._checked(super().execute, *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._checked(super().executemany, *args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self._checked(super().executescript, *args, **kwargs)

    def fetchone(self):
        return self._checked(super().fetchone)

    def fetchmany(self, *args):
        return self._checked(super().fetchmany, *args)

    def fetchall(self):
        return self._checked(super().fetchall)

    def __next__(self):
        return self._checked(super().__next__)


class _StorageConnection(sqlite3.Connection):
    fault_callback = None

    def cursor(self, factory=_StorageCursor):
        return super().cursor(factory)

    def execute(self, *args, **kwargs):
        return self.cursor().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self.cursor().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self.cursor().executescript(*args, **kwargs)


class _MinuteBudget:
    def __init__(self, transactions: int, logical_bytes: int):
        self.transactions = transactions
        self.logical_bytes = logical_bytes
        self._entries: deque[tuple[float, int]] = deque()
        self._bytes = 0
        self._lock = threading.Lock()

    def reserve(self, size: int) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._entries and now - self._entries[0][0] >= 60:
                _timestamp, expired = self._entries.popleft()
                self._bytes -= expired
            if len(self._entries) >= self.transactions:
                return False
            if self._bytes + size > self.logical_bytes:
                return False
            self._entries.append((now, size))
            self._bytes += size
            return True


class StorageRuntime:
    """The only supported SQLite entry point in the backend.

    A runtime owns exactly one writer connection for each database and protects
    them with locks.  Large bodies live in CAS; SQLite rows contain references.
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        state_home: str | Path | None = None,
        policy: StoragePolicy | None = None,
        health_listener: Callable[[dict], None] | None = None,
        exclusive_maintenance: bool = False,
    ):
        validate_sqlite_runtime()
        self.paths = resolve_storage_paths(workspace, state_home=state_home)
        self.policy = policy or StoragePolicy()
        self._health_listener = health_listener
        self._state_lock = threading.RLock()
        self._observability_lock = threading.RLock()
        self._health_lock = threading.RLock()
        self._cas_lock = threading.RLock()
        self._closed = False
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
        self._ensure_authoritative_capacity(self._estimated_sqlite_write(logical_bytes))
        if not self._global_sqlite_budget.reserve(
            self._estimated_sqlite_write(logical_bytes)
        ):
            self._publish_degraded("global_sqlite_write_rate_exceeded")
            raise StorageBlocked("global_sqlite_write_rate_exceeded")

    def put_state_ref(self, namespace: str, key: str, value_ref: str) -> None:
        """Store a small authoritative reference, never a large inline body."""
        self._ensure_open()
        if not namespace or not key or not value_ref:
            raise ValueError("namespace, key and value_ref are required")
        logical = len(namespace) + len(key) + len(value_ref)
        self._reserve_authoritative_write(logical)
        with self._state_lock:
            self._state.execute(
                """INSERT INTO storage_kv(namespace, key, value_ref, updated_at)
                   VALUES(?, ?, ?, ?)
                   ON CONFLICT(namespace, key) DO UPDATE SET
                       value_ref=excluded.value_ref,
                       updated_at=excluded.updated_at""",
                (namespace, key, value_ref, _utc_now()),
            )

    def get_state_ref(self, namespace: str, key: str) -> str | None:
        self._ensure_open()
        with self._state_lock:
            row = self._state.execute(
                "SELECT value_ref FROM storage_kv WHERE namespace=? AND key=?",
                (namespace, key),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def _write_blob_file(
        self, data: bytes, *, media_type: str = "application/octet-stream"
    ) -> dict:
        """Write one immutable file without opening a SQLite transaction."""
        self._ensure_open()
        if len(data) > self.policy.max_cas_object_bytes:
            raise StorageBlocked("cas_object_too_large")
        digest = hashlib.sha256(data).hexdigest()
        target = self.paths.cas_dir / digest[:2] / digest[2:]
        with self._cas_lock:
            if not target.exists():
                health = self.check_health()
                if health.cas_bytes + len(data) > self.policy.cas_max_bytes:
                    raise StorageBlocked("cas_capacity_exceeded")
                if health.total_bytes + len(data) > self.policy.total_max_bytes:
                    raise StorageBlocked("project_storage_capacity_exceeded")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
                try:
                    with temporary.open("xb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, target)
                    with self._health_lock:
                        if self._cas_bytes_hint is not None:
                            self._cas_bytes_hint += len(data)
                finally:
                    temporary.unlink(missing_ok=True)
        return {
            "digest": digest,
            "ref": f"sha256:{digest}",
            "byte_length": len(data),
            "media_type": media_type,
            "created_at": _utc_now(),
        }

    @staticmethod
    def _encoded_object(value: object) -> bytes:
        return json.dumps(
            redact_for_persistence(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _register_object(connection: sqlite3.Connection, descriptor: dict) -> None:
        connection.execute(
            """INSERT INTO objects(digest, byte_length, media_type, created_at)
               VALUES(?, ?, ?, ?) ON CONFLICT(digest) DO NOTHING""",
            (
                descriptor["digest"], descriptor["byte_length"],
                descriptor["media_type"], descriptor["created_at"],
            ),
        )

    def put_blob(self, data: bytes, *, media_type: str = "application/octet-stream") -> str:
        """Write an immutable plaintext CAS object once and return its SHA-256 ref."""
        descriptor = self._write_blob_file(data, media_type=media_type)
        with self._state_lock:
            known = self._state.execute(
                "SELECT 1 FROM objects WHERE digest=?", (descriptor["digest"],)
            ).fetchone()
            if known is None:
                self._reserve_authoritative_write(256)
                self._register_object(self._state, descriptor)
        return str(descriptor["ref"])

    def read_blob(self, ref: str) -> bytes:
        self._ensure_open()
        if not ref.startswith("sha256:"):
            raise ValueError("unsupported object reference")
        digest = ref.removeprefix("sha256:")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid SHA-256 object reference")
        path = self.paths.cas_dir / digest[:2] / digest[2:]
        try:
            return path.read_bytes()
        except FileNotFoundError:
            restored = self._restore_canonical_blob(digest)
            if restored is not None:
                return restored
            reason = f"missing:{digest}"
            with self._health_lock:
                self._cas_reference_reasons[digest] = reason
            raise StorageReferenceMissing(ref, str(path)) from None

    def _restore_canonical_blob(self, digest: str) -> bytes | None:
        """Restore only tiny canonical JSON values proven by their digest.

        Empty split checkpoint components were historically shared by digest
        and could be collected by the old shallow GC.  Their bytes are fully
        determined by the hash, so restoring them is lossless.  Unknown content
        is never guessed.
        """
        candidates = (
            self._encoded_object({}), self._encoded_object([]),
            self._encoded_object(None), self._encoded_object(""),
            self._encoded_object(False), self._encoded_object(True),
        )
        data = next(
            (candidate for candidate in candidates
             if hashlib.sha256(candidate).hexdigest() == digest),
            None,
        )
        if data is None:
            return None
        target = self.paths.cas_dir / digest[:2] / digest[2:]
        with self._cas_lock:
            if target.exists():
                return target.read_bytes()
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.repair.tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                with self._health_lock:
                    if self._cas_bytes_hint is not None:
                        self._cas_bytes_hint += len(data)
                    self._cas_reference_reasons.pop(digest, None)
            finally:
                temporary.unlink(missing_ok=True)
        return data

    def put_tool_result(
        self,
        content: str,
        *,
        process_id: str = "",
        task_id: str = "",
        tool_name: str = "",
        media_type: str = "application/json",
    ) -> dict:
        """Persist one oversized tool result as a content-addressed object.

        The locator is stable across retries and duplicate results. Ownership is
        recorded separately so deduplication never erases task/process lineage.
        """
        self._ensure_open()
        encoded = str(content).encode("utf-8")
        descriptor = self._write_blob_file(encoded, media_type=media_type)
        locator = f"tool-result:{descriptor['ref']}"
        now = _utc_now()
        self._reserve_authoritative_write(1024)
        with self._state_lock:
            self._state.execute("BEGIN IMMEDIATE")
            try:
                self._register_object(self._state, descriptor)
                self._state.execute(
                    """INSERT INTO tool_result_objects(
                           locator, content_ref, media_type, byte_length,
                           char_length, created_at
                       ) VALUES(?, ?, ?, ?, ?, ?)
                       ON CONFLICT(locator) DO NOTHING""",
                    (
                        locator, descriptor["ref"], media_type,
                        len(encoded), len(str(content)), now,
                    ),
                )
                self._state.execute(
                    """INSERT INTO tool_result_owners(
                           locator, process_id, task_id, tool_name, created_at
                       ) VALUES(?, ?, ?, ?, ?)
                       ON CONFLICT(locator, process_id, task_id, tool_name)
                       DO NOTHING""",
                    (locator, process_id, task_id, tool_name, now),
                )
                self._state.execute("COMMIT")
            except BaseException:
                self._state.execute("ROLLBACK")
                raise
        return {
            "locator": locator,
            "digest": descriptor["digest"],
            "media_type": media_type,
            "byte_length": len(encoded),
            "total_chars": len(str(content)),
        }

    def read_tool_result(
        self,
        locator: str,
        *,
        offset: int = 0,
        max_chars: int = 24_000,
        json_pointer: str = "",
        query: str = "",
    ) -> dict:
        """Read a bounded page from an addressable tool result.

        Paging is calculated against the final JSON transport size, preventing
        a page from recursively entering the generic spill path.
        """
        self._ensure_open()
        if not str(locator).startswith("tool-result:sha256:"):
            raise ValueError("locator must be tool-result:sha256:<digest>")
        with self._state_lock:
            row = self._state.execute(
                """SELECT content_ref, media_type, byte_length, char_length
                   FROM tool_result_objects WHERE locator=?""",
                (str(locator),),
            ).fetchone()
        if row is None:
            raise KeyError(f"tool result not found: {locator}")
        content = self.read_blob(str(row["content_ref"])).decode("utf-8")
        selected_pointer = str(json_pointer or "").strip()
        if selected_pointer:
            value: object = json.loads(content)
            if selected_pointer != "/":
                if not selected_pointer.startswith("/"):
                    raise ValueError("json_pointer must be empty, '/', or start with '/'")
                for raw_part in selected_pointer[1:].split("/"):
                    part = raw_part.replace("~1", "/").replace("~0", "~")
                    if isinstance(value, list):
                        value = value[int(part)]
                    elif isinstance(value, dict):
                        value = value[part]
                    else:
                        raise ValueError("json_pointer traverses a scalar value")
            content = json.dumps(
                value, ensure_ascii=False, sort_keys=True, indent=2,
            )
        needle = str(query or "")
        query_match = None
        if needle:
            found = content.casefold().find(needle.casefold())
            if found < 0:
                return {
                    "locator": locator,
                    "media_type": str(row["media_type"]),
                    "json_pointer": selected_pointer,
                    "query": needle,
                    "match": None,
                    "content": "",
                    "offset": 0,
                    "next_offset": None,
                    "total_chars": len(content),
                    "truncated": False,
                }
            query_match = found
            offset = max(0, found - 500)
        start = max(0, int(offset or 0))
        if start > len(content):
            raise ValueError("offset is beyond the tool result")
        requested = max(1_000, min(int(max_chars or 24_000), 100_000))
        end = min(len(content), start + requested)
        while True:
            result = {
                "locator": locator,
                "media_type": str(row["media_type"]),
                "json_pointer": selected_pointer,
                "query": needle,
                "match": query_match,
                "content": content[start:end],
                "offset": start,
                "next_offset": end if end < len(content) else None,
                "total_chars": len(content),
                "truncated": end < len(content),
            }
            if len(json.dumps(result, ensure_ascii=False)) <= 28_000 or end - start <= 1_000:
                result["transport_page_limited"] = end < min(
                    len(content), start + requested,
                )
                return result
            end = start + max(1_000, int((end - start) * 0.75))

    def save_custom_tool(
        self, spec: dict, source: str, *, replace: bool = False,
    ) -> dict:
        """Persist one project-scoped authored tool as an immutable version.

        Persistence and authority are intentionally separate: this catalog
        records an asset, but a process must still explicitly mount it through
        ``author_tool`` and pass its capability gate before it can execute it.
        """
        self._ensure_open()
        name = str(spec.get("name") or "").strip()
        digest = str(spec.get("digest") or "").strip()
        source_digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()
        if (not name or len(digest) != 64 or spec.get("execution_mode") not in {
            "authored_pure_python", "authored_privileged_python",
        }):
            raise ValueError("invalid authored tool specification")
        if source_digest != str(spec.get("source_sha256") or ""):
            raise ValueError("authored tool source digest does not match its specification")

        # A reusable project asset can outlive the turn that created it.  Apply
        # the same content scanner used at the Git publication boundary before
        # immutable CAS storage, and never return matched material in errors.
        from backend.core.authorship import scan_privacy
        alerts = [
            item for item in scan_privacy(
                f".gitgo/custom-tools/{name}.py", str(source), level=2,
            )
            if str(item.get("level") or "") == "error"
        ]
        if alerts:
            rules = sorted({str(item.get("rule") or "privacy") for item in alerts})
            raise StorageBlocked(
                "CUSTOM_TOOL_PRIVACY_BLOCKED:" + ",".join(rules)
            )

        persistent_spec = {
            key: value for key, value in dict(spec).items()
            if key not in {"_source", "source"}
        }
        source_object = self._write_blob_file(
            str(source).encode("utf-8"), media_type="text/x-python",
        )
        spec_object = self._write_blob_file(
            self._encoded_object(persistent_spec), media_type="application/json",
        )
        now = _utc_now()
        self._reserve_authoritative_write(4096)
        with self._state_lock:
            self._state.execute("BEGIN IMMEDIATE")
            try:
                row = self._state.execute(
                    "SELECT tool_id, state, current_version FROM custom_tools WHERE name=?",
                    (name,),
                ).fetchone()
                if row is not None and not replace:
                    raise ValueError("CUSTOM_TOOL_EXISTS: use replace to create a new version")
                if row is None and replace:
                    raise ValueError("CUSTOM_TOOL_NOT_FOUND: use register to create it")
                for descriptor in (source_object, spec_object):
                    self._register_object(self._state, descriptor)
                if row is None:
                    tool_id = str(uuid.uuid4())
                    version = 1
                    self._state.execute(
                        """INSERT INTO custom_tools(
                               tool_id, name, state, current_version, created_at, updated_at
                           ) VALUES(?, ?, 'active', ?, ?, ?)""",
                        (tool_id, name, version, now, now),
                    )
                else:
                    tool_id = str(row["tool_id"])
                    existing = self._state.execute(
                        """SELECT version FROM custom_tool_versions
                            WHERE tool_id=? AND digest=?""",
                        (tool_id, digest),
                    ).fetchone()
                    if existing is not None:
                        version = int(existing["version"])
                    else:
                        version = int(row["current_version"] or 0) + 1
                    self._state.execute(
                        """UPDATE custom_tools SET state='active', current_version=?, updated_at=?
                            WHERE tool_id=?""",
                        (version, now, tool_id),
                    )
                self._state.execute(
                    """INSERT INTO custom_tool_versions(
                           tool_id, version, spec_ref, source_ref, digest, created_at
                       ) VALUES(?, ?, ?, ?, ?, ?)
                       ON CONFLICT(tool_id, digest) DO NOTHING""",
                    (
                        tool_id, version, spec_object["ref"], source_object["ref"],
                        digest, now,
                    ),
                )
                self._state.execute("COMMIT")
            except BaseException:
                self._state.execute("ROLLBACK")
                raise
        return {
            "saved": True, "tool_id": tool_id, "name": name,
            "version": version, "digest": digest, "state": "active",
        }

    def list_custom_tools(self, *, include_archived: bool = False) -> dict:
        """Return bounded catalog metadata without exposing stored source."""
        self._ensure_open()
        where = "" if include_archived else "WHERE t.state='active'"
        with self._state_lock:
            rows = self._state.execute(
                f"""SELECT t.tool_id, t.name, t.state, t.current_version,
                            t.created_at, t.updated_at, v.spec_ref, v.digest,
                            (SELECT COUNT(*) FROM custom_tool_versions x
                              WHERE x.tool_id=t.tool_id) AS version_count
                       FROM custom_tools t
                       JOIN custom_tool_versions v ON v.tool_id=t.tool_id
                            AND v.version=t.current_version
                       {where}
                      ORDER BY t.name COLLATE NOCASE
                      LIMIT 500"""
            ).fetchall()
        tools = []
        for row in rows:
            spec = self._read_json_blob(str(row["spec_ref"]))
            spec = spec if isinstance(spec, dict) else {}
            tools.append({
                "tool_id": str(row["tool_id"]), "name": str(row["name"]),
                "description": str(spec.get("description") or ""),
                "state": str(row["state"]),
                "version": int(row["current_version"]),
                "version_count": int(row["version_count"]),
                "digest": str(row["digest"]),
                "updated_at": str(row["updated_at"]),
                "authority": (
                    "user_approved_privileged"
                    if spec.get("execution_mode") == "authored_privileged_python"
                    else "pure_transform_only"
                ),
                "effect": str(spec.get("effect") or "read"),
                "resources": list(spec.get("resources") or []),
            })
        return {"tools": tools, "count": len(tools)}

    def load_custom_tool(self, name: str, *, version: int | None = None) -> dict:
        """Load a saved version for an explicit task-local mount."""
        self._ensure_open()
        with self._state_lock:
            tool = self._state.execute(
                "SELECT tool_id, state, current_version FROM custom_tools WHERE name=?",
                (str(name),),
            ).fetchone()
            if tool is None:
                raise KeyError(f"CUSTOM_TOOL_NOT_FOUND:{name}")
            if str(tool["state"]) != "active":
                raise ValueError(f"CUSTOM_TOOL_ARCHIVED:{name}")
            selected = int(version or tool["current_version"])
            row = self._state.execute(
                """SELECT spec_ref, source_ref, digest FROM custom_tool_versions
                    WHERE tool_id=? AND version=?""",
                (str(tool["tool_id"]), selected),
            ).fetchone()
        if row is None:
            raise KeyError(f"CUSTOM_TOOL_VERSION_NOT_FOUND:{name}@{selected}")
        spec = self._read_json_blob(str(row["spec_ref"]))
        if not isinstance(spec, dict):
            raise RuntimeError("CUSTOM_TOOL_SPEC_INVALID")
        source = self.read_blob(str(row["source_ref"])).decode("utf-8")
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != spec.get("source_sha256"):
            raise RuntimeError("CUSTOM_TOOL_SOURCE_INTEGRITY_FAILED")
        return {
            "tool_id": str(tool["tool_id"]), "name": str(name),
            "version": selected, "digest": str(row["digest"]),
            "source_ref": str(row["source_ref"]), "spec": spec,
            "source": source,
        }

    def set_custom_tool_archived(self, name: str, archived: bool = True) -> dict:
        """Archive or restore an asset; immutable versions remain recoverable."""
        self._ensure_open()
        state = "archived" if archived else "active"
        now = _utc_now()
        self._reserve_authoritative_write(512)
        with self._state_lock:
            cursor = self._state.execute(
                "UPDATE custom_tools SET state=?, updated_at=? WHERE name=?",
                (state, now, str(name)),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"CUSTOM_TOOL_NOT_FOUND:{name}")
        return {"name": str(name), "state": state, "archived": bool(archived)}

    def create_session_lineage_checkpoint(
        self,
        snapshot: dict,
        *,
        session_id: str,
        process_id: str,
        turn_id: str,
    ) -> dict:
        """Append an immutable pre-turn snapshot and advance the undo cursor.

        The CAS object is the history.  SQLite only owns the small, atomic
        lineage pointer, so a rewind never edits or deletes the old branch.
        """
        self._ensure_open()
        if not session_id or not process_id or not turn_id:
            raise ValueError("session_id, process_id and turn_id are required")
        descriptor = self._write_blob_file(
            self._encoded_object(snapshot), media_type="application/json",
        )
        checkpoint_id = str(uuid.uuid4())
        now = _utc_now()
        session = dict(snapshot.get("session") or {})
        message_count = len(session.get("messages") or [])
        context_epoch = int(session.get("context_epoch", 0) or 0)
        self._reserve_authoritative_write(2048)
        with self._state_lock:
            self._state.execute("BEGIN IMMEDIATE")
            try:
                self._register_object(self._state, descriptor)
                parent_row = self._state.execute(
                    "SELECT checkpoint_id FROM session_lineage_heads WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                parent_id = str(parent_row[0]) if parent_row and parent_row[0] else None
                existing = self._state.execute(
                    "SELECT checkpoint_id FROM session_lineage WHERE session_id=? AND turn_id=?",
                    (session_id, turn_id),
                ).fetchone()
                if existing is not None:
                    checkpoint_id = str(existing[0])
                else:
                    self._state.execute(
                        """INSERT INTO session_lineage(
                               checkpoint_id, session_id, process_id, turn_id,
                               parent_checkpoint_id, snapshot_ref, message_count,
                               context_epoch, state, created_at
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'retained', ?)""",
                        (
                            checkpoint_id, session_id, process_id, turn_id,
                            parent_id, descriptor["ref"], message_count,
                            context_epoch, now,
                        ),
                    )
                self._state.execute(
                    """INSERT INTO session_lineage_heads(session_id, checkpoint_id, updated_at)
                       VALUES(?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET
                           checkpoint_id=excluded.checkpoint_id,
                           updated_at=excluded.updated_at""",
                    (session_id, checkpoint_id, now),
                )
                self._state.execute("COMMIT")
            except BaseException:
                self._state.execute("ROLLBACK")
                raise
        return {
            "checkpoint_id": checkpoint_id,
            "parent_checkpoint_id": parent_id,
            "session_id": session_id,
            "process_id": process_id,
            "turn_id": turn_id,
            "message_count": message_count,
            "context_epoch": context_epoch,
        }

    def preview_session_undo(self, session_id: str) -> dict:
        """Describe the next branch rewind without mutating authoritative state."""
        self._ensure_open()
        with self._state_lock:
            row = self._state.execute(
                """SELECT l.checkpoint_id, l.parent_checkpoint_id, l.process_id,
                          l.turn_id, l.snapshot_ref, l.message_count,
                          l.context_epoch, l.created_at
                     FROM session_lineage_heads h
                     JOIN session_lineage l ON l.checkpoint_id=h.checkpoint_id
                    WHERE h.session_id=?""",
                (session_id,),
            ).fetchone()
        if row is None:
            raise ValueError("SESSION_UNDO_UNAVAILABLE:no retained user-turn checkpoint")
        snapshot = self._read_json_blob(str(row["snapshot_ref"]))
        if not isinstance(snapshot, dict):
            raise RuntimeError("SESSION_UNDO_SNAPSHOT_INVALID")
        current_process_id = str(snapshot.get("current_process_id") or row["process_id"])
        current = self.load_agent_session_state(current_process_id) or {}
        target_session = dict(snapshot.get("session") or {})
        target_messages = list(target_session.get("messages") or [])
        current_messages = list(current.get("messages") or [])
        effects = dict(snapshot.get("effects_before_turn") or {})
        return {
            "checkpoint_id": str(row["checkpoint_id"]),
            "parent_checkpoint_id": (
                str(row["parent_checkpoint_id"]) if row["parent_checkpoint_id"] else None
            ),
            "session_id": session_id,
            "process_id": str(row["process_id"]),
            "turn_id": str(row["turn_id"]),
            "created_at": str(row["created_at"]),
            "target_message_count": len(target_messages),
            "current_message_count": len(current_messages),
            "messages_removed": max(0, len(current_messages) - len(target_messages)),
            "context_epoch": int(row["context_epoch"] or 0),
            "turn_preview": str(snapshot.get("turn_preview") or ""),
            "scope": "session_only",
            "workspace_reverted": False,
            "external_effects_reverted": False,
            "effects_before_turn": effects,
            "warning": (
                "Conversation context will rewind. Files, commits, provider calls, "
                "and other external effects are retained and remain auditable."
            ),
        }

    def commit_session_undo(self, session_id: str, checkpoint_id: str) -> dict:
        """Move the undo cursor to its parent and return the immutable target."""
        preview = self.preview_session_undo(session_id)
        if str(preview["checkpoint_id"]) != str(checkpoint_id):
            raise ValueError("SESSION_UNDO_STALE_PREVIEW")
        with self._state_lock:
            row = self._state.execute(
                "SELECT snapshot_ref, parent_checkpoint_id FROM session_lineage WHERE checkpoint_id=? AND session_id=?",
                (checkpoint_id, session_id),
            ).fetchone()
            if row is None:
                raise ValueError("SESSION_UNDO_CHECKPOINT_NOT_FOUND")
            snapshot = self._read_json_blob(str(row["snapshot_ref"]))
            now = _utc_now()
            self._reserve_authoritative_write(1024)
            self._state.execute("BEGIN IMMEDIATE")
            try:
                self._state.execute(
                    "UPDATE session_lineage SET state='rewound' WHERE checkpoint_id=?",
                    (checkpoint_id,),
                )
                self._state.execute(
                    "UPDATE session_lineage_heads SET checkpoint_id=?, updated_at=? WHERE session_id=?",
                    (row["parent_checkpoint_id"], now, session_id),
                )
                self._state.execute("COMMIT")
            except BaseException:
                self._state.execute("ROLLBACK")
                raise
        if not isinstance(snapshot, dict):
            raise RuntimeError("SESSION_UNDO_SNAPSHOT_INVALID")
        return {**preview, "snapshot": dict(snapshot)}

    def _read_json_blob(self, ref: str) -> object:
        return json.loads(self.read_blob(ref).decode("utf-8"))

    def save_agent_checkpoint(self, snapshot: dict) -> str:
        """Atomically persist one process/session/task boundary.

        CAS files are immutable and may safely precede the SQLite commit.  A
        failed transaction can only leave unreferenced objects for later GC;
        it can never expose a partial authoritative checkpoint.
        """
        self._ensure_open()
        process_id = str(snapshot.get("process_id", "")).strip()
        session_id = str(snapshot.get("session_id", "")).strip()
        if not process_id or not session_id:
            raise ValueError("process_id and session_id are required")
        if self.is_process_deleted(process_id):
            raise ValueError("PROCESS_DELETED: a deleted process cannot be checkpointed again")
        messages = list(snapshot.get("messages") or [])
        provider_state = dict(snapshot.get("provider_state") or {})
        session_metadata = dict(snapshot.get("session_metadata") or {})

        descriptors: dict[str, dict] = {}

        def prepare(value: object, media_type: str = "application/json") -> dict:
            descriptor = self._write_blob_file(
                self._encoded_object(value), media_type=media_type
            )
            descriptors[str(descriptor["digest"])] = descriptor
            return descriptor

        provider_objects = {
            str(state_id): prepare(state)
            for state_id, state in provider_state.items()
        }
        session_metadata["provider_state_refs"] = {
            state_id: descriptor["ref"]
            for state_id, descriptor in provider_objects.items()
        }
        for field_name in (
            "context_memo", "host_ledger", "provider_usage",
            "cache_telemetry", "epoch_archive",
        ):
            if field_name in session_metadata:
                descriptor = prepare(session_metadata.pop(field_name))
                session_metadata[f"{field_name}_ref"] = descriptor["ref"]
        metadata_object = prepare(session_metadata)
        runtime_object = prepare(dict(snapshot.get("runtime_state") or {}))
        message_objects: list[tuple[int, dict, dict, dict | None]] = []
        for sequence, raw_message in enumerate(messages):
            message = dict(raw_message or {})
            message_object = prepare(message)
            state_id = str(message.get("provider_state_id", ""))
            reasoning_object = provider_objects.get(state_id)
            message_objects.append((sequence, message, message_object, reasoning_object))

        task_id = str(snapshot.get("task_id", "")).strip()
        contract_object = None
        outcome_object = None
        if task_id:
            contract_object = prepare({
                "task_description": snapshot.get("task_description", ""),
                "task_kind": snapshot.get("task_kind", ""),
                "required_test_ids": list(snapshot.get("required_test_ids") or []),
                "capability_profile_id": snapshot.get("capability_profile_id", ""),
            })
            if snapshot.get("result") is not None:
                outcome_object = prepare(snapshot.get("result"))

        usage_values = None
        raw_outcome = snapshot.get("result")
        if task_id and isinstance(raw_outcome, dict):
            metadata = dict(raw_outcome.get("metadata") or {})
            budget = dict(metadata.get("task_tree_budget") or {})
            used = dict(budget.get("used") or {})
            usage_values = {
                "task_kind": str(metadata.get("task_kind") or snapshot.get("task_kind") or ""),
                "status": str(raw_outcome.get("status") or snapshot.get("status") or ""),
                "provider_calls": int(used.get("provider_calls", 0) or 0),
                "input_tokens": int(used.get("reported_input_tokens", 0) or 0),
                "output_tokens": int(used.get("reported_output_tokens", 0) or 0),
                "cache_read_tokens": int(used.get("reported_cache_read_tokens", 0) or 0),
                "cache_write_tokens": int(used.get("reported_cache_write_tokens", 0) or 0),
                "duration_ms": float(raw_outcome.get("duration_ms", 0.0) or 0.0),
                "tool_calls": int(raw_outcome.get("tool_calls_executed", 0) or 0),
            }

        receipt_objects: list[tuple[dict, dict]] = []
        for raw_receipt in list(snapshot.get("tool_receipts") or []):
            receipt = dict(raw_receipt or {})
            if task_id and receipt.get("receipt_id"):
                receipt_objects.append((receipt, prepare(receipt)))

        logical_estimate = 2048 + 512 * (
            len(message_objects) + len(receipt_objects) + len(descriptors)
        )
        self._reserve_authoritative_write(logical_estimate)
        now = _utc_now()
        checkpoint_at = str(snapshot.get("checkpoint_at") or now)
        status = str(snapshot.get("status") or "running")
        dependencies = list(dict.fromkeys(
            str(item) for item in (snapshot.get("depends_on") or []) if str(item)
        ))
        with self._state_lock:
            self._state.execute("BEGIN IMMEDIATE")
            try:
                for descriptor in descriptors.values():
                    self._register_object(self._state, descriptor)
                # A checkpoint is one ownership unit.  Record the complete CAS
                # closure in the same transaction as the authoritative rows so
                # GC can never mistake split metadata/provider objects for
                # crash orphans.  Replacing the owner set also releases objects
                # no longer used by a later checkpoint after the grace period.
                self._state.execute(
                    "DELETE FROM object_refs WHERE owner_type=? AND owner_id=?",
                    ("session_checkpoint", session_id),
                )
                self._state.executemany(
                    """INSERT INTO object_refs(
                           owner_type, owner_id, field_name, digest, created_at
                       ) VALUES(?, ?, ?, ?, ?)""",
                    [
                        (
                            "session_checkpoint", session_id,
                            f"object:{digest}", digest, now,
                        )
                        for digest in sorted(descriptors)
                    ],
                )
                event_row = self._state.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM session_events WHERE process_id=?",
                    (process_id,),
                ).fetchone()
                checkpoint_sequence = int(event_row[0] if event_row else 0)
                self._state.execute(
                    """INSERT INTO sessions(
                           session_id, project_id, status, context_epoch,
                           created_at, updated_at, metadata_ref, checkpoint_at,
                           checkpoint_sequence
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(session_id) DO UPDATE SET
                           status=excluded.status,
                           context_epoch=excluded.context_epoch,
                           updated_at=excluded.updated_at,
                           metadata_ref=excluded.metadata_ref,
                           checkpoint_at=excluded.checkpoint_at,
                           checkpoint_sequence=excluded.checkpoint_sequence""",
                    (
                        session_id, self.paths.project_id, status,
                        int(session_metadata.get("context_epoch", 0) or 0),
                        str(snapshot.get("session_created_at") or now), now,
                        metadata_object["ref"], checkpoint_at, checkpoint_sequence,
                    ),
                )
                self._state.execute(
                    "DELETE FROM process_dependencies WHERE process_id=?",
                    (process_id,),
                )
                for upstream_id in dependencies:
                    if upstream_id == process_id:
                        raise ValueError("process cannot depend on itself")
                    self._state.execute(
                        """INSERT INTO process_dependencies(
                               process_id, depends_on_process_id, state,
                               created_at, updated_at
                           ) VALUES(?, ?, 'declared', ?, ?)""",
                        (process_id, upstream_id, now, now),
                    )
                if task_id:
                    parent_process_id = str(snapshot.get("parent_id") or "")
                    parent_task_id = None
                    if parent_process_id:
                        parent_task_row = self._state.execute(
                            "SELECT task_id FROM session_processes WHERE process_id=?",
                            (parent_process_id,),
                        ).fetchone()
                        if parent_task_row is not None and parent_task_row["task_id"]:
                            parent_task_id = str(parent_task_row["task_id"])
                    self._state.execute(
                        """INSERT INTO tasks(
                               task_id, session_id, parent_task_id, role, status,
                               contract_ref, outcome_ref, created_at, updated_at
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(task_id) DO UPDATE SET
                                session_id=excluded.session_id,
                                parent_task_id=excluded.parent_task_id,
                                role=excluded.role,
                               status=excluded.status,
                               contract_ref=excluded.contract_ref,
                               outcome_ref=excluded.outcome_ref,
                               updated_at=excluded.updated_at""",
                        (
                            task_id, session_id, parent_task_id,
                            str(snapshot.get("role") or ""),
                            status, contract_object["ref"] if contract_object else None,
                            outcome_object["ref"] if outcome_object else None,
                            str(snapshot.get("created_at") or now), now,
                        ),
                    )
                    if usage_values is not None:
                        self._state.execute(
                            """INSERT INTO task_usage(
                                   task_id, process_id, parent_process_id, task_kind,
                                   status, provider_calls, input_tokens, output_tokens,
                                   cache_read_tokens, cache_write_tokens, duration_ms,
                                   tool_calls, created_at, updated_at
                               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(task_id) DO UPDATE SET
                                   process_id=excluded.process_id,
                                   parent_process_id=excluded.parent_process_id,
                                   task_kind=excluded.task_kind,
                                   status=excluded.status,
                                   provider_calls=excluded.provider_calls,
                                   input_tokens=excluded.input_tokens,
                                   output_tokens=excluded.output_tokens,
                                   cache_read_tokens=excluded.cache_read_tokens,
                                   cache_write_tokens=excluded.cache_write_tokens,
                                   duration_ms=excluded.duration_ms,
                                   tool_calls=excluded.tool_calls,
                                   updated_at=excluded.updated_at""",
                            (
                                task_id, process_id,
                                str(snapshot.get("parent_id") or ""),
                                usage_values["task_kind"], usage_values["status"],
                                usage_values["provider_calls"], usage_values["input_tokens"],
                                usage_values["output_tokens"], usage_values["cache_read_tokens"],
                                usage_values["cache_write_tokens"], usage_values["duration_ms"],
                                usage_values["tool_calls"],
                                str(snapshot.get("created_at") or now), now,
                            ),
                        )
                self._state.execute(
                    """INSERT INTO session_processes(
                           process_id, session_id, task_id, parent_process_id,
                           role, actor_kind, capability_profile_id, status,
                           steps_used, max_steps, checkpoint_event_sequence,
                           created_at, updated_at, runtime_state_ref
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(process_id) DO UPDATE SET
                           session_id=excluded.session_id,
                           task_id=excluded.task_id,
                           parent_process_id=excluded.parent_process_id,
                           role=excluded.role,
                           actor_kind=excluded.actor_kind,
                           capability_profile_id=excluded.capability_profile_id,
                           status=excluded.status,
                           steps_used=excluded.steps_used,
                           max_steps=excluded.max_steps,
                           checkpoint_event_sequence=excluded.checkpoint_event_sequence,
                           runtime_state_ref=excluded.runtime_state_ref,
                           updated_at=excluded.updated_at""",
                    (
                        process_id, session_id, task_id or None,
                        str(snapshot.get("parent_id") or "") or None,
                        str(snapshot.get("role") or ""),
                        str(snapshot.get("actor_kind") or ""),
                        str(snapshot.get("capability_profile_id") or ""),
                        status, int(snapshot.get("steps_used", 0) or 0),
                        int(snapshot.get("max_steps", 0) or 0), checkpoint_sequence,
                        str(snapshot.get("created_at") or now), now,
                        runtime_object["ref"],
                    ),
                )
                for sequence, message, message_object, reasoning_object in message_objects:
                    message_id = hashlib.sha256(
                        f"{session_id}:{sequence}".encode("utf-8")
                    ).hexdigest()
                    self._state.execute(
                        """INSERT INTO messages(
                               message_id, session_id, task_id, sequence, role,
                               content_ref, reasoning_ref, created_at
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(session_id, sequence) DO UPDATE SET
                               task_id=CASE
                                   WHEN messages.content_ref=excluded.content_ref
                                   THEN messages.task_id ELSE excluded.task_id END,
                               role=excluded.role,
                               content_ref=excluded.content_ref,
                               reasoning_ref=excluded.reasoning_ref
                           WHERE messages.role IS NOT excluded.role
                              OR messages.content_ref IS NOT excluded.content_ref
                              OR messages.reasoning_ref IS NOT excluded.reasoning_ref""",
                        (
                            message_id, session_id, task_id or None, sequence,
                            str(message.get("role") or ""), message_object["ref"],
                            reasoning_object["ref"] if reasoning_object else None, now,
                        ),
                    )
                self._state.execute(
                    "DELETE FROM messages WHERE session_id=? AND sequence>=?",
                    (session_id, len(message_objects)),
                )
                for receipt, receipt_object in receipt_objects:
                    self._state.execute(
                        """INSERT INTO receipts(
                               receipt_id, task_id, receipt_type, evidence_ref, created_at
                           ) VALUES(?, ?, ?, ?, ?)
                           ON CONFLICT(receipt_id) DO UPDATE SET
                               task_id=excluded.task_id,
                               receipt_type=excluded.receipt_type,
                               evidence_ref=excluded.evidence_ref""",
                        (
                            str(receipt["receipt_id"]), task_id,
                            str(receipt.get("tool_name") or receipt.get("effect") or "tool"),
                            receipt_object["ref"],
                            str(receipt.get("timestamp") or now),
                        ),
                    )
                self._state.execute("COMMIT")
            except BaseException:
                self._state.execute("ROLLBACK")
                raise
        return f"sqlite:session/{process_id}/{checkpoint_sequence}"

    def read_task_usage(self, *, limit: int = 100, cursor: str = "") -> dict:
        """Return authoritative root-task usage without reading verbose traces."""
        self._ensure_open()
        bounded = max(1, min(int(limit), 1000))
        page_after = _decode_page_cursor(str(cursor or ""))
        with self._state_lock:
            totals = self._state.execute(
                """SELECT COUNT(*), COALESCE(SUM(provider_calls), 0),
                          COALESCE(SUM(input_tokens), 0),
                          COALESCE(SUM(output_tokens), 0),
                          COALESCE(SUM(cache_read_tokens), 0),
                          COALESCE(SUM(cache_write_tokens), 0),
                          COALESCE(SUM(duration_ms), 0),
                          COALESCE(SUM(tool_calls), 0)
                   FROM task_usage WHERE parent_process_id=''"""
            ).fetchone()
            rows = self._state.execute(
                """SELECT task_id, process_id, task_kind, status, provider_calls,
                          input_tokens, output_tokens, cache_read_tokens,
                          cache_write_tokens, duration_ms, tool_calls,
                          created_at, updated_at
                   FROM task_usage WHERE parent_process_id=''
                   ORDER BY updated_at DESC, task_id DESC""",
            ).fetchall()
            legacy_rows = self._state.execute(
                """SELECT DISTINCT t.task_id, sp.process_id, t.status,
                          t.outcome_ref, t.created_at, t.updated_at
                   FROM tasks t
                   JOIN session_processes sp ON sp.task_id=t.task_id
                   LEFT JOIN task_usage u ON u.task_id=t.task_id
                   WHERE sp.parent_process_id IS NULL
                     AND t.outcome_ref IS NOT NULL AND u.task_id IS NULL
                   ORDER BY t.updated_at DESC, t.task_id DESC""",
            ).fetchall()
        keys = (
            "task_count", "provider_calls", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens", "duration_ms", "tool_calls",
        )
        summary = {
            key: (float(totals[index]) if key == "duration_ms" else int(totals[index]))
            for index, key in enumerate(keys)
        }
        legacy_tasks: list[dict] = []
        for row in legacy_rows:
            outcome = self._read_json_blob(str(row["outcome_ref"]))
            if not isinstance(outcome, dict):
                continue
            metadata = dict(outcome.get("metadata") or {})
            budget = dict(metadata.get("task_tree_budget") or {})
            used = dict(budget.get("used") or {})
            item = {
                "task_id": str(row["task_id"]),
                "process_id": str(row["process_id"]),
                "task_kind": str(metadata.get("task_kind") or ""),
                "status": str(outcome.get("status") or row["status"] or ""),
                "provider_calls": int(used.get("provider_calls", 0) or 0),
                "input_tokens": int(used.get("reported_input_tokens", 0) or 0),
                "output_tokens": int(used.get("reported_output_tokens", 0) or 0),
                "cache_read_tokens": int(used.get("reported_cache_read_tokens", 0) or 0),
                "cache_write_tokens": int(used.get("reported_cache_write_tokens", 0) or 0),
                "duration_ms": float(outcome.get("duration_ms", 0.0) or 0.0),
                "tool_calls": int(outcome.get("tool_calls_executed", 0) or 0),
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"] or ""),
            }
            legacy_tasks.append(item)
            summary["task_count"] += 1
            for key in keys[1:]:
                summary[key] += item[key]
        input_tokens = summary["input_tokens"]
        summary["cache_hit_ratio"] = (
            round(summary["cache_read_tokens"] / input_tokens, 4)
            if input_tokens else 0.0
        )
        task_rows = [{key: row[key] for key in row.keys()} for row in rows]
        task_rows.extend(legacy_tasks)
        task_rows.sort(key=lambda item: (
            str(item.get("updated_at") or ""), str(item.get("task_id") or ""),
        ), reverse=True)
        if page_after is not None:
            task_rows = [item for item in task_rows if (
                str(item.get("updated_at") or ""), str(item.get("task_id") or ""),
            ) < page_after]
        page = task_rows[:bounded]
        has_more = len(task_rows) > bounded
        next_cursor = (
            _encode_page_cursor(
                str(page[-1].get("updated_at") or ""),
                str(page[-1].get("task_id") or ""),
            ) if has_more and page else None
        )
        return {
            "summary": summary,
            "tasks": page,
            "page": {
                "limit": bounded, "cursor": str(cursor or ""),
                "next_cursor": next_cursor, "has_more": has_more,
                "scope": "root_tasks_ordered_by_updated_at",
                "summary_scope": "all_authoritative_root_tasks",
            },
        }

    def append_session_event(
        self, process_id: str, event_type: str, data: dict | None = None,
        *, occurred_at: str | None = None, sequence: int | None = None,
        event_id: str | None = None,
    ) -> int:
        self._ensure_open()
        if not process_id or not event_type:
            raise ValueError("process_id and event_type are required")
        descriptor = self._write_blob_file(
            self._encoded_object(data or {}), media_type="application/json"
        )
        self._reserve_authoritative_write(1024)
        with self._state_lock:
            self._state.execute("BEGIN IMMEDIATE")
            try:
                self._register_object(self._state, descriptor)
                if sequence is None:
                    row = self._state.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM session_events WHERE process_id=?",
                        (process_id,),
                    ).fetchone()
                    resolved_sequence = int(row[0] if row else 1)
                else:
                    resolved_sequence = max(1, int(sequence))
                existing = self._state.execute(
                    """SELECT event_id, event_type, payload_ref FROM session_events
                       WHERE process_id=? AND sequence=?""",
                    (process_id, resolved_sequence),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["event_type"]) != event_type
                        or str(existing["payload_ref"]) != str(descriptor["ref"])
                    ):
                        raise RuntimeError(
                            "session event sequence collision with different content"
                        )
                    self._state.execute("COMMIT")
                    return resolved_sequence
                self._state.execute(
                    """INSERT INTO session_events(
                           event_id, process_id, sequence, event_type, payload_ref, occurred_at
                       ) VALUES(?, ?, ?, ?, ?, ?)""",
                    (
                        event_id or str(uuid.uuid4()), process_id, resolved_sequence, event_type,
                        descriptor["ref"], occurred_at or _utc_now(),
                    ),
                )
                self._state.execute("COMMIT")
            except BaseException:
                self._state.execute("ROLLBACK")
                raise
        return resolved_sequence

    def load_agent_session_state(self, process_id: str) -> dict | None:
        self._ensure_open()
        with self._state_lock:
            process_row = self._state.execute(
                """SELECT session_id, checkpoint_event_sequence, runtime_state_ref
                   FROM session_processes WHERE process_id=?""",
                (process_id,),
            ).fetchone()
            session_row = None
            message_rows = []
            checkpoint_sequence = 0
            if process_row is not None and process_row["session_id"]:
                checkpoint_sequence = int(process_row["checkpoint_event_sequence"] or 0)
                session_row = self._state.execute(
                    """SELECT session_id, metadata_ref, context_epoch
                       FROM sessions WHERE session_id=?""",
                    (process_row["session_id"],),
                ).fetchone()
                message_rows = self._state.execute(
                    """SELECT content_ref FROM messages
                       WHERE session_id=? ORDER BY sequence""",
                    (process_row["session_id"],),
                ).fetchall()
            event_rows = self._state.execute(
                """SELECT sequence, event_type, payload_ref FROM session_events
                   WHERE process_id=? AND sequence>? ORDER BY sequence""",
                (process_id, checkpoint_sequence),
            ).fetchall()

        messages = [
            dict(self._read_json_blob(str(row["content_ref"])))
            for row in message_rows
        ]
        provider_state: dict = {}
        metadata: dict = {}
        session_id = ""
        if session_row is not None:
            session_id = str(session_row["session_id"])
            if session_row["metadata_ref"]:
                loaded = self._read_json_blob(str(session_row["metadata_ref"]))
                if isinstance(loaded, dict):
                    metadata = dict(loaded)
                    provider_state = dict(metadata.pop("provider_state", {}) or {})
                    provider_refs = dict(metadata.pop("provider_state_refs", {}) or {})
                    for state_id, ref in provider_refs.items():
                        state = self._read_json_blob(str(ref))
                        if isinstance(state, dict):
                            provider_state[str(state_id)] = dict(state)
                    for field_name in (
                        "context_memo", "host_ledger", "provider_usage",
                        "cache_telemetry", "epoch_archive",
                    ):
                        ref = metadata.pop(f"{field_name}_ref", None)
                        if ref:
                            metadata[field_name] = self._read_json_blob(str(ref))
        for row in event_rows:
            payload = self._read_json_blob(str(row["payload_ref"]))
            data = dict(payload) if isinstance(payload, dict) else {}
            event_type = str(row["event_type"])
            if event_type == "message_append" and isinstance(data.get("message"), dict):
                messages.append(dict(data["message"]))
            elif event_type == "message_pop":
                count = max(0, int(data.get("count", 1) or 1))
                if count:
                    del messages[max(0, len(messages) - count):]
        if not messages and not provider_state and session_row is None:
            return None
        return {
            "session_id": session_id,
            "messages": messages,
            "provider_state": provider_state,
            **metadata,
        }

    @staticmethod
    def _is_public_user_message(message: dict) -> bool:
        """Return whether a provider-visible user message belongs in the UI chat.

        AgentSession is the provider transcript, not the product transcript.  It
        also contains governance nudges, recovery continuations and host steering.
        Keeping that distinction here prevents the Dashboard from accidentally
        rendering internal control traffic as if the user had typed it.
        """
        if str(message.get("role") or "") != "user":
            return False
        message_type = str(message.get("message_type") or "")
        if message_type and message_type != "conversation":
            return False
        content = str(message.get("content") or "")
        internal_prefixes = (
            "[HOST ", "[USER DECISION ", "[GOVERNANCE ", "[\u6cbb\u7406\u5efa\u8bae]",
            "[\u7cfb\u7edf\u63d0\u793a]", "[\u7cfb\u7edf]",
        )
        return bool(content.strip()) and not content.lstrip().startswith(internal_prefixes)

    @staticmethod
    def _storage_projection_warning(
        exc: StorageReferenceMissing, *, turn_id: str = "",
    ) -> dict:
        """Visible placeholder for damaged history without inventing content."""
        return {
            "message_id": f"storage-warning:{exc.ref}",
            "turn_id": turn_id,
            "role": "assistant",
            "content": (
                "[Error: [STORAGE_CAS_REFERENCE_MISSING] Part of this historical "
                "record is unavailable. Current project data was not replaced; "
                "restore a verified backup to recover the missing detail.]"
            ),
            "timestamp": "",
            "final": True,
            "kind": "error",
            "visibility": "public",
            "status": "degraded",
            "storage_ref": exc.ref,
        }

    def _public_session_messages(self, session_id: str) -> list[dict]:
        """Project one durable provider session into the canonical public chat.

        User turns come from the message journal.  Assistant turns come from the
        terminal TaskOutcome, which is the sole completion authority.  Intermediate
        provider prose remains available in traces/verbose mode but is deliberately
        excluded here, avoiding the former duplicate-final-answer race.
        """
        with self._state_lock:
            message_rows = self._state.execute(
                """SELECT message_id, task_id, sequence, content_ref, created_at
                   FROM messages WHERE session_id=? ORDER BY sequence""",
                (session_id,),
            ).fetchall()
            task_rows = self._state.execute(
                """SELECT task_id, outcome_ref, created_at, updated_at
                   FROM tasks WHERE session_id=? ORDER BY created_at, task_id""",
                (session_id,),
            ).fetchall()
            session_row = self._state.execute(
                "SELECT metadata_ref FROM sessions WHERE session_id=?", (session_id,),
            ).fetchone()

        messages_by_task: dict[str, list[dict]] = {}
        unscoped: list[dict] = []
        for row in message_rows:
            try:
                loaded = self._read_json_blob(str(row["content_ref"]))
            except StorageReferenceMissing as exc:
                warning = self._storage_projection_warning(
                    exc, turn_id=str(row["task_id"] or ""),
                )
                task_id = str(row["task_id"] or "")
                if task_id:
                    messages_by_task.setdefault(task_id, []).append(warning)
                else:
                    unscoped.append(warning)
                continue
            if not isinstance(loaded, dict) or not self._is_public_user_message(loaded):
                continue
            projected = {
                "message_id": str(row["message_id"]),
                "turn_id": str(row["task_id"] or ""),
                "role": "user",
                "content": str(loaded.get("content") or ""),
                "timestamp": str(loaded.get("timestamp") or row["created_at"] or ""),
                "final": True,
                "kind": "conversation",
                "visibility": "public",
                "sequence": int(row["sequence"]),
            }
            task_id = str(row["task_id"] or "")
            if task_id:
                messages_by_task.setdefault(task_id, []).append(projected)
            else:
                unscoped.append(projected)

        if session_row is not None and session_row["metadata_ref"]:
            try:
                metadata = self._read_json_blob(str(session_row["metadata_ref"]))
                if isinstance(metadata, dict):
                    ledger_ref = metadata.get("host_ledger_ref")
                    try:
                        ledger = (
                            self._read_json_blob(str(ledger_ref))
                            if ledger_ref else metadata.get("host_ledger", [])
                        )
                    except StorageReferenceMissing as exc:
                        unscoped.append(self._storage_projection_warning(exc))
                        ledger = []
                    from backend.core.loop.decision_timeline import decision_timeline
                    for card in decision_timeline(ledger if isinstance(ledger, list) else []):
                        messages_by_task.setdefault(card["turn_id"], []).append(card)
            except StorageReferenceMissing as exc:
                unscoped.append(self._storage_projection_warning(exc))
        public = list(unscoped)
        known_tasks: set[str] = set()
        for row in task_rows:
            task_id = str(row["task_id"])
            known_tasks.add(task_id)
            public.extend(sorted(messages_by_task.get(task_id, []), key=lambda item: item.get("sequence", 0)))
            outcome_ref = str(row["outcome_ref"] or "")
            if not outcome_ref:
                continue
            try:
                loaded = self._read_json_blob(outcome_ref)
            except StorageReferenceMissing as exc:
                public.append(self._storage_projection_warning(exc, turn_id=task_id))
                continue
            if not isinstance(loaded, dict):
                continue
            response = str(loaded.get("response") or "").strip()
            status = str(loaded.get("status") or "")
            if status == "awaiting_user":
                # The durable question card is the public projection; the
                # formatted control response would duplicate all its options.
                if any(item.get("kind") == "decision" for item in messages_by_task.get(task_id, [])):
                    continue
            failed = status in {"failed", "cancelled", "timed_out", "degraded"}
            if failed:
                error = loaded.get("error") or {}
                code = str(error.get("code") or "AGENT_TASK_FAILED")
                detail = str(error.get("message") or f"Agent task ended as {status}")
                report = f"[Error: [{code}] {detail}]"
                response = f"{report}\n\n{response}" if status == "degraded" and response else report
            if not response:
                continue
            public.append({
                "message_id": f"outcome:{task_id}",
                "turn_id": task_id,
                "role": "assistant",
                "content": response,
                "timestamp": str(row["updated_at"] or row["created_at"] or ""),
                "final": True,
                "kind": "error" if failed else "outcome",
                "visibility": "public",
                "status": str(loaded.get("status") or ""),
                "duration_ms": float(loaded.get("duration_ms", 0.0) or 0.0),
                "trace_id": str((loaded.get("metadata") or {}).get("trace_id") or task_id),
                "process_id": str(loaded.get("process_id") or ""),
            })
        for task_id, items in messages_by_task.items():
            if task_id not in known_tasks:
                public.extend(items)
        return public

    def read_latest_conversations(self) -> dict:
        """Return the latest durable root conversation and its Agent tree.

        This is a read model: completed processes are not resurrected in APM and
        the daemon does not need to be started merely to display chat history.
        """
        self._ensure_open()
        with self._state_lock:
            root = self._state.execute(
                """SELECT process_id, session_id FROM session_processes
                   WHERE parent_process_id IS NULL
                   ORDER BY updated_at DESC, created_at DESC LIMIT 1"""
            ).fetchone()
            if root is None:
                return {"main_conversation": [], "agent_conversations": {},
                        "conversation_process_id": "", "session_id": ""}
            root_id = str(root["process_id"])
            rows = self._state.execute(
                """WITH RECURSIVE tree(process_id, session_id) AS (
                       SELECT process_id, session_id FROM session_processes
                        WHERE process_id=?
                       UNION ALL
                       SELECT child.process_id, child.session_id
                         FROM session_processes child
                         JOIN tree parent ON child.parent_process_id=parent.process_id
                   ) SELECT process_id, session_id FROM tree""",
                (root_id,),
            ).fetchall()

        conversations = {
            str(row["process_id"]): self._public_session_messages(str(row["session_id"]))
            for row in rows
        }
        session_id = str(root["session_id"])
        main_conversation = self._public_session_messages(session_id)
        # B questions remain authoritative in the B session ledger.  A gets a
        # compact read projection so it can supervise without duplicating the
        # full option payload or becoming a manual message relay.
        question_summaries = []
        for process_id, messages in conversations.items():
            if process_id == root_id:
                continue
            for message in messages:
                decision = message.get("decision")
                if not isinstance(decision, dict):
                    continue
                source = str(decision.get("source_display_name") or "Subprocess")
                question = str(decision.get("question") or message.get("content") or "")
                answer = str(message.get("decision_answer") or "")
                content = f"{source} asked: {question}"
                if answer:
                    content += f" · answered: {answer}"
                question_summaries.append({
                    "message_id": f"child-question:{decision.get('decision_id')}",
                    "turn_id": str(message.get("turn_id") or ""),
                    "role": "assistant",
                    "content": content,
                    "timestamp": str(message.get("timestamp") or ""),
                    "final": True,
                    "kind": "agent_question",
                    "visibility": "public",
                    "status": str(message.get("status") or "awaiting_user"),
                    "process_id": process_id,
                })
        main_conversation.extend(question_summaries)
        return {
            "main_conversation": main_conversation,
            "agent_conversations": conversations,
            "conversation_process_id": root_id,
            "session_id": session_id,
        }

    def read_process_presentation(self, process_id: str) -> dict:
        self._ensure_open()
        with self._state_lock:
            row = self._state.execute(
                "SELECT display_name, archived FROM process_presentation WHERE process_id=?",
                (process_id,),
            ).fetchone()
        if row is None:
            return {}
        return {**({"display_name": row["display_name"]} if row["display_name"] else {}),
                "archived": bool(row["archived"])}

    def read_process_presentations(self) -> dict[str, dict]:
        self._ensure_open()
        with self._state_lock:
            rows = self._state.execute("SELECT process_id, display_name, archived FROM process_presentation").fetchall()
        return {str(row["process_id"]): {
            **({"display_name": row["display_name"]} if row["display_name"] else {}),
            "archived": bool(row["archived"]),
        } for row in rows}

    def update_process_presentation(self, process_id: str, *,
                                    display_name: str | None = None,
                                    archived: bool | None = None) -> dict:
        """Small independent fields: checkpoints never overwrite user choices.

        Each update changes only the requested column in one transaction, so
        concurrent hosts renaming/archiving cannot clobber one another.
        """
        self._ensure_open()
        if (display_name is None) == (archived is None):
            raise ValueError("exactly one presentation field is required")
        column = "display_name" if display_name is not None else "archived"
        value = display_name if display_name is not None else int(bool(archived))
        self._reserve_authoritative_write(512 + len(str(value)))
        with self._state_lock:
            self._state.execute("BEGIN IMMEDIATE")
            try:
                process = self._state.execute(
                    "SELECT actor_kind, parent_process_id FROM session_processes WHERE process_id=?",
                    (process_id,),
                ).fetchone()
                if process is None:
                    raise ValueError("PROCESS_NOT_FOUND")
                if process["actor_kind"] == "supervisor" or not (
                    process["parent_process_id"] or process["actor_kind"] in ("worker", "reviewer")
                ):
                    raise ValueError("PROCESS_NOT_B")
                self._state.execute(
                    f"""INSERT INTO process_presentation(process_id, {column}, updated_at)
                        VALUES(?, ?, ?) ON CONFLICT(process_id) DO UPDATE SET
                        {column}=excluded.{column}, updated_at=excluded.updated_at
                        WHERE process_presentation.{column} IS NOT excluded.{column}""",
                    (process_id, value, _utc_now()),
                )
                self._state.execute("COMMIT")
            except BaseException:
                self._state.execute("ROLLBACK")
                raise
        return {"process_id": process_id, **self.read_process_presentation(process_id)}

    def read_project_b_processes(self) -> dict[str, dict]:
        """Return every durable B process for this project, across all tasks.

        A supervisor is a task execution lease behind the project Chat and is
        deliberately not part of the user-facing ProcessList. Legacy child rows
        without actor_kind remain identifiable by their parent_process_id.
        """
        self._ensure_open()
        with self._state_lock:
            rows = self._state.execute(
                """SELECT process_id, session_id, task_id, parent_process_id,
                          role, actor_kind, capability_profile_id, status,
                          steps_used, max_steps, runtime_state_ref, created_at,
                          updated_at,
                          (SELECT metadata_ref FROM sessions s WHERE s.session_id=session_processes.session_id) AS metadata_ref
                   FROM session_processes
                   WHERE COALESCE(actor_kind, '') != 'supervisor'
                     AND (parent_process_id IS NOT NULL OR actor_kind IN ('worker', 'reviewer'))
                   ORDER BY created_at, process_id"""
            ).fetchall()
            dependency_rows = self._state.execute(
                """SELECT process_id, depends_on_process_id
                   FROM process_dependencies ORDER BY created_at, depends_on_process_id"""
            ).fetchall()

        dependencies: dict[str, list[str]] = {}
        for dependency in dependency_rows:
            dependencies.setdefault(str(dependency["process_id"]), []).append(
                str(dependency["depends_on_process_id"]),
            )

        presentations = self.read_process_presentations()
        processes: dict[str, dict] = {}
        for row in rows:
            process_id = str(row["process_id"])
            runtime: dict = {}
            storage_error: StorageReferenceMissing | None = None
            if row["runtime_state_ref"]:
                try:
                    loaded = self._read_json_blob(str(row["runtime_state_ref"]))
                    if isinstance(loaded, dict):
                        runtime = dict(loaded)
                except StorageReferenceMissing as exc:
                    storage_error = exc
            durable_state = None
            if not runtime.get("estimated_tokens") or not runtime.get("context"):
                try:
                    durable_state = self.load_agent_process_state(process_id)
                except StorageReferenceMissing as exc:
                    storage_error = storage_error or exc
            durable_session = dict((durable_state or {}).get("session") or {})
            estimated_tokens = int(runtime.get("estimated_tokens", 0) or 0)
            if estimated_tokens <= 0 and durable_session:
                estimated_tokens = _estimate_durable_session_tokens(durable_session)
            context = dict(runtime.get("context") or {})
            if not context and durable_session:
                # load_agent_session_state returns durable metadata flattened
                # beside messages/provider_state, not under session_metadata.
                metadata = dict(durable_session.get("session_metadata") or durable_session)
                context = {
                    "estimated_tokens": estimated_tokens,
                    "limit": int(metadata.get("model_context_limit", 0) or 0),
                    "epoch": int(metadata.get("context_epoch", 0) or 0),
                }
            if durable_session and not dict(context.get("breakdown") or {}):
                context["breakdown"] = _durable_context_breakdown(durable_session)
            cache_summary = dict(runtime.get("cache_summary") or {})
            if not cache_summary and durable_session:
                cache_summary = _durable_cache_summary(durable_session)
            owner = str(row["parent_process_id"] or "")
            depends_on = dependencies.get(process_id, [])
            worktree = dict(runtime.get("worktree") or {})
            worktree_path = str(
                runtime.get("worktree_path") or worktree.get("path") or ""
            )
            context_snapshot = dict(runtime.get("context_snapshot") or {})
            from backend.core.loop.coordination import coordination_summary_from_context
            display_name = ""
            if row["metadata_ref"]:
                try:
                    loaded_metadata = self._read_json_blob(str(row["metadata_ref"]))
                    if isinstance(loaded_metadata, dict):
                        display_name = str(loaded_metadata.get("display_name") or "")
                except StorageReferenceMissing as exc:
                    storage_error = storage_error or exc
            processes[process_id] = {
                "process_id": process_id,
                "session_id": str(row["session_id"]),
                "display_name": display_name,
                "role": str(row["role"] or ""),
                "ring_level": int(runtime.get("ring_level", 3) or 3),
                "status": str(row["status"] or ""),
                "steps_used": int(row["steps_used"] or 0),
                "max_steps": int(row["max_steps"] or 0),
                "parent_id": owner or None,
                "parent_ids": list(dict.fromkeys(([owner] if owner else []) + depends_on)),
                "depends_on": depends_on,
                "downstream_ids": [],
                "child_ids": list(runtime.get("child_ids") or []),
                "child_contracts": dict(runtime.get("delegated_contracts") or {}),
                "child_reviews": dict(runtime.get("child_reviews") or {}),
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"] or ""),
                "worktree_path": worktree_path,
                "worktree": {
                    **worktree,
                    "path": str(worktree.get("path") or worktree_path),
                    "isolated": bool(worktree.get("isolated", False)),
                    "state": str(
                        worktree.get("state")
                        or ("isolated" if worktree.get("isolated") else "shared_workspace")
                    ),
                },
                "provider_id": str(runtime.get("provider_id") or ""),
                "model_id": str(runtime.get("model_id") or ""),
                "estimated_tokens": estimated_tokens,
                "active_task_id": str(row["task_id"] or ""),
                "task_kind": str(runtime.get("task_kind") or ""),
                "actor_kind": str(row["actor_kind"] or "worker"),
                "capability_profile_id": str(row["capability_profile_id"] or ""),
                "relationship_policy": dict(
                    context_snapshot.get("relationship_policy") or {}
                ),
                "coordination": coordination_summary_from_context(context_snapshot),
                "pending_decision": runtime.get("pending_decision"),
                "recovery": (
                    {
                        "state": "storage_reference_missing",
                        "code": StorageReferenceMissing.code,
                        "message": str(storage_error),
                        "ref": storage_error.ref,
                    }
                    if storage_error else runtime.get("recovery")
                ),
                "storage_error": (
                    {
                        "code": StorageReferenceMissing.code,
                        "message": str(storage_error),
                        "ref": storage_error.ref,
                    }
                    if storage_error else None
                ),
                "mailbox": runtime.get("mailbox"),
                "task_budget": runtime.get("task_budget"),
                "context": context or None,
                "cache_summary": cache_summary,
                "durable": True,
                **presentations.get(process_id, {}),
            }

        for process_id, process in processes.items():
            process["downstream_ids"] = [
                candidate_id
                for candidate_id, candidate in processes.items()
                if process_id in candidate.get("depends_on", [])
            ]
        return processes

    def read_latest_root_process(self) -> dict | None:
        """Return the latest durable A control projection without starting a Daemon.

        The project ProcessList still consumes ``read_project_b_processes`` and
        therefore excludes A.  This minimal root projection exists for the Chat
        controller: a pending decision, recovery state, context meter, and A
        status must survive a cold Dashboard/Daemon restart.
        """
        state = self.load_latest_root_process_state()
        if not state:
            return None
        row = dict(state.get("process") or {})
        runtime = dict(state.get("runtime_state") or {})
        durable_session = dict(state.get("session") or {})
        estimated_tokens = int(runtime.get("estimated_tokens", 0) or 0)
        if estimated_tokens <= 0 and durable_session:
            estimated_tokens = _estimate_durable_session_tokens(durable_session)
        context = dict(runtime.get("context") or {})
        if not context and durable_session:
            metadata = dict(durable_session.get("session_metadata") or durable_session)
            context = {
                "estimated_tokens": estimated_tokens,
                "limit": int(metadata.get("model_context_limit", 0) or 0),
                "epoch": int(metadata.get("context_epoch", 0) or 0),
            }
        if durable_session and not dict(context.get("breakdown") or {}):
            context["breakdown"] = _durable_context_breakdown(durable_session)
        cache_summary = dict(runtime.get("cache_summary") or {})
        if not cache_summary and durable_session:
            cache_summary = _durable_cache_summary(durable_session)
        contract = dict((state.get("task") or {}).get("contract") or {})
        process_id = str(row.get("process_id") or "")
        if not process_id:
            return None
        worktree = dict(runtime.get("worktree") or {})
        worktree_path = str(
            runtime.get("worktree_path") or worktree.get("path") or ""
        )
        context_snapshot = dict(runtime.get("context_snapshot") or {})
        from backend.core.loop.coordination import coordination_summary_from_context
        return {
            "process_id": process_id,
            "session_id": str(row.get("session_id") or ""),
            "role": str(row.get("role") or "supervisor"),
            "ring_level": int(runtime.get("ring_level", 0) or 0),
            "status": str(row.get("status") or ""),
            "steps_used": int(row.get("steps_used", 0) or 0),
            "max_steps": int(row.get("max_steps", 0) or 0),
            "parent_id": None,
            "parent_ids": [],
            "depends_on": [],
            "downstream_ids": [],
            "child_ids": list(runtime.get("child_ids") or []),
            "child_contracts": dict(runtime.get("delegated_contracts") or {}),
            "child_reviews": dict(runtime.get("child_reviews") or {}),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "worktree_path": worktree_path,
            "worktree": {
                **worktree,
                "path": str(worktree.get("path") or worktree_path),
                "isolated": bool(worktree.get("isolated", False)),
                "state": str(
                    worktree.get("state")
                    or ("isolated" if worktree.get("isolated") else "shared_workspace")
                ),
            },
            "provider_id": str(runtime.get("provider_id") or ""),
            "model_id": str(runtime.get("model_id") or ""),
            "estimated_tokens": estimated_tokens,
            "active_task_id": str(row.get("task_id") or ""),
            "task_kind": str(
                contract.get("task_kind") or runtime.get("task_kind") or "answer"
            ),
            "actor_kind": str(row.get("actor_kind") or "supervisor"),
            "capability_profile_id": str(
                row.get("capability_profile_id") or "supervisor.control"
            ),
            "relationship_policy": dict(
                context_snapshot.get("relationship_policy") or {}
            ),
            "coordination": coordination_summary_from_context(context_snapshot),
            "pending_decision": runtime.get("pending_decision"),
            "recovery": runtime.get("recovery"),
            "mailbox": runtime.get("mailbox"),
            "task_budget": runtime.get("task_budget"),
            "context": context or None,
            "cache_summary": cache_summary,
            "durable": True,
        }

    def read_latest_root_status(self) -> dict | None:
        """Cheap project-list projection with no CAS/session materialisation.

        The Dashboard polls this for cold projects.  One indexed SQLite read is
        deliberately separate from ``read_latest_root_process`` so project-list
        refreshes cannot repeatedly decode the whole context/session graph.
        """
        self._ensure_open()
        with self._state_lock:
            row = self._state.execute(
                """SELECT process_id, status, updated_at
                   FROM session_processes
                   WHERE COALESCE(actor_kind, '') = 'supervisor'
                      OR parent_process_id IS NULL
                   ORDER BY updated_at DESC, created_at DESC
                   LIMIT 1"""
            ).fetchone()
        if row is None:
            return None
        return {
            "process_id": str(row["process_id"] or ""),
            "status": str(row["status"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def read_project_b_conversations(self) -> dict[str, list[dict]]:
        """Return public durable transcripts for every B in the project."""
        self._ensure_open()
        with self._state_lock:
            rows = self._state.execute(
                """SELECT process_id, session_id, updated_at FROM session_processes
                   WHERE COALESCE(actor_kind, '') != 'supervisor'
                     AND (parent_process_id IS NOT NULL OR actor_kind IN ('worker', 'reviewer'))
                   ORDER BY created_at, process_id"""
            ).fetchall()
        cache = getattr(self, "_b_conversation_cache", {})
        current_ids: set[str] = set()
        result: dict[str, list[dict]] = {}
        for row in rows:
            process_id = str(row["process_id"])
            current_ids.add(process_id)
            version = str(row["updated_at"] or "")
            cached = cache.get(process_id)
            if cached is None or cached[0] != version:
                cached = (
                    version,
                    self._public_session_messages(str(row["session_id"])),
                )
                cache[process_id] = cached
            result[process_id] = cached[1]
        self._b_conversation_cache = {
            process_id: cache[process_id] for process_id in current_ids
        }
        return result

    def load_agent_process_state(self, process_id: str) -> dict | None:
        """Return task/runtime state alongside the restored session snapshot."""
        session_state = self.load_agent_session_state(process_id)
        with self._state_lock:
            row = self._state.execute(
                """SELECT process_id, session_id, task_id, parent_process_id,
                          role, actor_kind, capability_profile_id, status,
                          steps_used, max_steps, runtime_state_ref, created_at,
                          updated_at
                   FROM session_processes WHERE process_id=?""",
                (process_id,),
            ).fetchone()
            session_row = None
            task_row = None
            receipt_rows = []
            dependency_rows = self._state.execute(
                """SELECT depends_on_process_id, state, artifact_ref
                   FROM process_dependencies WHERE process_id=?
                   ORDER BY created_at, depends_on_process_id""",
                (process_id,),
            ).fetchall()
            if row is not None and row["session_id"]:
                session_row = self._state.execute(
                    """SELECT status, checkpoint_at, checkpoint_sequence,
                              created_at, updated_at
                       FROM sessions WHERE session_id=?""",
                    (row["session_id"],),
                ).fetchone()
            if row is not None and row["task_id"]:
                task_row = self._state.execute(
                    """SELECT task_id, parent_task_id, role, status, contract_ref,
                              outcome_ref, created_at, updated_at
                       FROM tasks WHERE task_id=?""",
                    (row["task_id"],),
                ).fetchone()
                receipt_rows = self._state.execute(
                    """SELECT evidence_ref FROM receipts
                       WHERE task_id=? ORDER BY created_at""",
                    (row["task_id"],),
                ).fetchall()
        if row is None:
            return None
        runtime_state: dict = {}
        if row["runtime_state_ref"]:
            loaded = self._read_json_blob(str(row["runtime_state_ref"]))
            if isinstance(loaded, dict):
                runtime_state = dict(loaded)
        task: dict = {}
        if task_row is not None:
            task = {
                key: task_row[key]
                for key in task_row.keys()
                if key not in {"contract_ref", "outcome_ref"}
            }
            if task_row["contract_ref"]:
                contract = self._read_json_blob(str(task_row["contract_ref"]))
                task["contract"] = dict(contract) if isinstance(contract, dict) else {}
            if task_row["outcome_ref"]:
                outcome = self._read_json_blob(str(task_row["outcome_ref"]))
                task["outcome"] = dict(outcome) if isinstance(outcome, dict) else outcome
        receipts = []
        for receipt_row in receipt_rows:
            receipt = self._read_json_blob(str(receipt_row["evidence_ref"]))
            if isinstance(receipt, dict):
                receipts.append(dict(receipt))
        return {
            "process": {key: row[key] for key in row.keys() if key != "runtime_state_ref"},
            "runtime_state": runtime_state,
            "session": session_state,
            "session_checkpoint": (
                {key: session_row[key] for key in session_row.keys()}
                if session_row is not None else {}
            ),
            "task": task,
            "receipts": receipts,
            "dependencies": [
                {key: dependency[key] for key in dependency.keys()}
                for dependency in dependency_rows
            ],
        }

    def load_latest_root_process_state(self) -> dict | None:
        """Load the latest durable A process, including terminal sessions."""
        self._ensure_open()
        with self._state_lock:
            row = self._state.execute(
                """SELECT process_id FROM session_processes
                   WHERE parent_process_id IS NULL
                   ORDER BY updated_at DESC, created_at DESC LIMIT 1"""
            ).fetchone()
        return self.load_agent_process_state(str(row["process_id"])) if row else None

    def save_worktree_state(self, record: dict) -> None:
        """Persist one low-frequency Agent worktree lifecycle transition."""
        self._ensure_open()
        worktree_id = str(record.get("worktree_id") or "").strip()
        process_id = str(record.get("process_id") or "").strip()
        if not worktree_id or not process_id:
            raise ValueError("worktree_id and process_id are required")
        metadata = {
            key: record.get(key)
            for key in (
                "upstream_commits", "changed_files", "runtime_offsets", "error", "created_at",
                "updated_at", "isolated", "promoted", "promoted_at",
            )
        }
        descriptor = self._write_blob_file(
            self._encoded_object(metadata), media_type="application/json"
        )
        self._reserve_authoritative_write(2048)
        now = _utc_now()
        task_id = str(record.get("task_id") or "").strip()
        with self._state_lock:
            known_task = None
            if task_id:
                known_task = self._state.execute(
                    "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            self._state.execute("BEGIN IMMEDIATE")
            try:
                self._register_object(self._state, descriptor)
                self._state.execute(
                    """INSERT INTO worktrees(
                           worktree_id, project_id, task_id, path_hint, git_head,
                           state, created_at, updated_at, process_id, mode,
                           root_workspace_hint, base_commit, snapshot_commit,
                           result_commit, own_commit, metadata_ref
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(worktree_id) DO UPDATE SET
                           task_id=COALESCE(excluded.task_id, worktrees.task_id),
                           path_hint=excluded.path_hint,
                           git_head=excluded.git_head,
                           state=excluded.state,
                           updated_at=excluded.updated_at,
                           process_id=excluded.process_id,
                           mode=excluded.mode,
                           root_workspace_hint=excluded.root_workspace_hint,
                           base_commit=excluded.base_commit,
                           snapshot_commit=excluded.snapshot_commit,
                           result_commit=excluded.result_commit,
                           own_commit=excluded.own_commit,
                           metadata_ref=excluded.metadata_ref""",
                    (
                        worktree_id, self.paths.project_id,
                        task_id if known_task is not None else None,
                        str(record.get("path") or ""),
                        str(record.get("result_commit") or record.get("snapshot_commit") or ""),
                        str(record.get("state") or "unknown"),
                        str(record.get("created_at") or now), now, process_id,
                        str(record.get("mode") or "write"),
                        str(record.get("root_workspace") or ""),
                        str(record.get("base_commit") or ""),
                        str(record.get("snapshot_commit") or ""),
                        str(record.get("result_commit") or ""),
                        str(record.get("own_commit") or ""), descriptor["ref"],
                    ),
                )
                self._state.execute("COMMIT")
            except BaseException:
                self._state.execute("ROLLBACK")
                raise

    def load_worktree_state(self, process_id: str) -> dict | None:
        self._ensure_open()
        with self._state_lock:
            row = self._state.execute(
                """SELECT worktree_id, process_id, task_id, path_hint, state,
                          mode, root_workspace_hint, base_commit, snapshot_commit,
                          result_commit, own_commit, metadata_ref, created_at, updated_at
                   FROM worktrees WHERE process_id=?""",
                (process_id,),
            ).fetchone()
        if row is None:
            return None
        result = {key: row[key] for key in row.keys() if key != "metadata_ref"}
        result["path"] = result.pop("path_hint")
        result["root_workspace"] = result.pop("root_workspace_hint")
        if row["metadata_ref"]:
            loaded = self._read_json_blob(str(row["metadata_ref"]))
            if isinstance(loaded, dict):
                result.update(loaded)
        result["isolated"] = bool(result.get("path")) and result.get("state") != "disposed"
        return result

    def list_worktree_states(self, *, active_only: bool = False) -> list[dict]:
        self._ensure_open()
        with self._state_lock:
            rows = self._state.execute(
                "SELECT process_id FROM worktrees ORDER BY created_at"
            ).fetchall()
        values = [self.load_worktree_state(str(row["process_id"])) for row in rows]
        result = [value for value in values if value is not None]
        if active_only:
            result = [value for value in result if value.get("state") != "disposed"]
        return result

    def list_incomplete_processes(self) -> list[str]:
        self._ensure_open()
        with self._state_lock:
            rows = self._state.execute(
                """SELECT process_id FROM session_processes
                   UNION SELECT process_id FROM session_events"""
            ).fetchall()
        terminal = {
            "completed", "failed", "cancelled", "timed_out", "killed", "orphaned",
        }
        incomplete: list[str] = []
        for row in rows:
            process_id = str(row["process_id"])
            with self._state_lock:
                event = self._state.execute(
                    """SELECT payload_ref FROM session_events
                       WHERE process_id=? AND event_type='agent_complete'
                       ORDER BY sequence DESC LIMIT 1""",
                    (process_id,),
                ).fetchone()
            status = ""
            if event is not None:
                payload = self._read_json_blob(str(event["payload_ref"]))
                if isinstance(payload, dict):
                    status = str(payload.get("status", ""))
            if status not in terminal:
                incomplete.append(process_id)
        return sorted(incomplete)

    def list_agent_process_links(self) -> list[dict]:
        """Return the minimal topology needed to rebuild a recovery tree."""
        self._ensure_open()
        with self._state_lock:
            rows = self._state.execute(
                """SELECT process_id, parent_process_id, session_id, task_id, status
                   FROM session_processes ORDER BY created_at"""
            ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def session_events_since_checkpoint(self, process_id: str) -> int:
        self._ensure_open()
        with self._state_lock:
            row = self._state.execute(
                """SELECT COUNT(*) FROM session_events
                   WHERE process_id=? AND sequence > COALESCE((
                       SELECT checkpoint_event_sequence FROM session_processes
                       WHERE process_id=?
                   ), 0)""",
                (process_id, process_id),
            ).fetchone()
        return int(row[0] if row else 0)

    def delete_process_session(self, process_id: str) -> None:
        self._ensure_open()
        self._reserve_authoritative_write(4096)
        with self._state_lock:
            self._state.execute("BEGIN IMMEDIATE")
            try:
                row = self._state.execute(
                    "SELECT session_id, task_id FROM session_processes WHERE process_id=?",
                    (process_id,),
                ).fetchone()
                session_id = str(row["session_id"] or "") if row else ""
                task_id = str(row["task_id"] or "") if row else ""
                self._state.execute(
                    "DELETE FROM session_events WHERE process_id=?", (process_id,)
                )
                self._state.execute(
                    "DELETE FROM process_dependencies WHERE process_id=? OR depends_on_process_id=?",
                    (process_id, process_id),
                )
                self._state.execute(
                    "DELETE FROM worktrees WHERE process_id=?", (process_id,)
                )
                self._state.execute(
                    "DELETE FROM session_processes WHERE process_id=?", (process_id,)
                )
                if task_id:
                    self._state.execute(
                        "UPDATE messages SET task_id=NULL WHERE task_id=?", (task_id,)
                    )
                    self._state.execute("DELETE FROM receipts WHERE task_id=?", (task_id,))
                    self._state.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
                if session_id:
                    other = self._state.execute(
                        "SELECT 1 FROM session_processes WHERE session_id=? LIMIT 1",
                        (session_id,),
                    ).fetchone()
                    if other is None:
                        self._state.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
                        self._state.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
                self._state.execute("COMMIT")
            except BaseException:
                self._state.execute("ROLLBACK")
                raise

    def is_process_deleted(self, process_id: str) -> bool:
        with self._state_lock:
            return self._state.execute("SELECT 1 FROM deleted_processes WHERE process_id=?", (process_id,)).fetchone() is not None

    def read_deleted_process_ids(self) -> set[str]:
        with self._state_lock:
            return {str(row[0]) for row in self._state.execute("SELECT process_id FROM deleted_processes").fetchall()}

    def purge_b_session(self, session_id: str, plan_id: str) -> None:
        """Remove all executions of one B session under exclusive maintenance.

        A's own historic evidence is retained. Task identity/usage and explicit
        deletion tombstones are not successful outcomes. Unreferenced blobs use
        the existing bounded CAS GC, not per-message physical overwrites.
        """
        if not self._exclusive_maintenance:
            raise ValueError("B deletion requires exclusive storage maintenance")
        self._reserve_authoritative_write(16384)
        with self._state_lock:
            self._state.execute("BEGIN IMMEDIATE")
            try:
                rows = self._state.execute("SELECT * FROM session_processes WHERE session_id=?", (session_id,)).fetchall()
                if not rows:
                    raise ValueError("PROCESS_NOT_FOUND")
                ids = {str(row["process_id"]) for row in rows}
                tasks = [str(row["task_id"]) for row in rows if row["task_id"]]
                for row in rows:
                    if row["actor_kind"] == "supervisor" or not row["parent_process_id"]:
                        raise ValueError("PROCESS_NOT_B")
                    if row["status"] not in {"completed", "failed", "cancelled", "timed_out", "killed", "orphaned"}:
                        raise ValueError("PROCESS_NOT_TERMINAL")
                    pid = row["process_id"]
                    children = self._state.execute("SELECT process_id FROM session_processes WHERE parent_process_id=?", (pid,)).fetchall()
                    if any(str(child[0]) not in ids for child in children):
                        raise ValueError("B owns another session; remove that B explicitly first")
                    self._state.execute("INSERT INTO deleted_processes VALUES(?, ?, ?)", (pid, plan_id, _utc_now()))
                    self._state.execute("DELETE FROM session_events WHERE process_id=?", (pid,))
                    self._state.execute("DELETE FROM process_dependencies WHERE process_id=? OR depends_on_process_id=?", (pid, pid))
                    self._state.execute("DELETE FROM worktrees WHERE process_id=?", (pid,))
                self._state.execute("DELETE FROM session_processes WHERE session_id=?", (session_id,))
                for tid in tasks:
                    for table in ("tool_calls", "receipts", "test_evidence", "provider_attempts"):
                        self._state.execute(f"DELETE FROM {table} WHERE task_id=?", (tid,))
                    self._state.execute("DELETE FROM mailbox_messages WHERE task_id=? OR sender_task_id=?", (tid, tid))
                self._state.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
                self._state.execute("UPDATE tasks SET session_id=NULL, contract_ref=NULL, outcome_ref=NULL, status='deleted' WHERE session_id=?", (session_id,))
                self._state.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
                self._state.execute("COMMIT")
            except BaseException:
                self._state.execute("ROLLBACK")
                raise
        with self._observability_lock:
            for tid in tasks:
                self._observability.execute("DELETE FROM trace_events WHERE trace_id=?", (tid,))
                self._observability.execute("DELETE FROM events WHERE task_id=?", (tid,))
            self._observability.execute("DELETE FROM events WHERE session_id=?", (session_id,))

    def create_deletion_plan(self, manifest: dict, *, not_before: float) -> dict:
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > 65536:
            raise ValueError("Deletion manifest is too large")
        self._reserve_authoritative_write(len(encoded) + 1024)
        now = _utc_now()
        with self._state_lock:
            count = int(self._state.execute("SELECT COUNT(*) FROM deletion_plans").fetchone()[0])
            if count >= 4096:
                self._state.execute("""DELETE FROM deletion_plans WHERE plan_id IN (
                    SELECT plan_id FROM deletion_plans
                    WHERE state IN ('completed','cancelled') ORDER BY updated_at LIMIT 128
                )""")
                count = int(self._state.execute("SELECT COUNT(*) FROM deletion_plans").fetchone()[0])
                if count >= 4096:
                    raise ValueError("DELETION_PLAN_LIMIT: resolve active/recovery plans before creating more")
            self._state.execute("INSERT INTO deletion_plans VALUES(?, ?, 'preview', ?, ?, '', ?, ?)",
                                (manifest["plan_id"], manifest["subject_id"], not_before, encoded, now, now))
        return self.read_deletion_plan(manifest["plan_id"])

    def read_deletion_plan(self, plan_id: str) -> dict:
        with self._state_lock:
            row = self._state.execute("SELECT * FROM deletion_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise ValueError("DELETION_PLAN_NOT_FOUND")
        return {**{k: row[k] for k in row.keys() if k != "manifest_json"}, "manifest": json.loads(row["manifest_json"])}

    def list_deletion_plans(self, *, limit: int = 100, due_only: bool = False) -> list[dict]:
        with self._state_lock:
            rows = self._state.execute("SELECT * FROM deletion_plans " +
                ("WHERE state='scheduled' AND not_before<=? " if due_only else "") +
                "ORDER BY CASE WHEN state IN ('scheduled','executing','blocked','recovery_required') THEN 0 ELSE 1 END, created_at DESC LIMIT ?",
                tuple([*([time.time()] if due_only else []), max(1, min(limit, 200))])).fetchall()
        return [{**{k: row[k] for k in row.keys() if k != "manifest_json"}, "manifest": json.loads(row["manifest_json"])} for row in rows]

    def transition_deletion_plan(self, plan_id: str, expected: str, state: str, *, error: str = "", not_before: float | None = None) -> dict:
        self._reserve_authoritative_write(2048)
        with self._state_lock:
            cursor = self._state.execute("""UPDATE deletion_plans SET state=?, error=?, updated_at=?,
                not_before=COALESCE(?, not_before) WHERE plan_id=? AND state=?""",
                (state, error[:2000], _utc_now(), not_before, plan_id, expected))
            if cursor.rowcount != 1:
                raise ValueError("DELETION_PLAN_STATE_CHANGED: refresh before acting")
        return self.read_deletion_plan(plan_id)

    def session_storage_counts(self) -> dict[str, int]:
        """Small diagnostic surface used by migration verification and tests."""
        with self._state_lock:
            return {
                table: int(self._state.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "sessions", "session_processes", "session_events",
                    "tasks", "messages", "receipts",
                )
            }

    def _prepare_state_json(self, value: object, *, media_type: str) -> dict:
        encoded = self._encoded_object(value)
        return self._write_blob_file(encoded, media_type=media_type)

    def append_history_record(
        self,
        record: dict,
        *,
        event_id: str | None = None,
        compact_threshold: int = 400,
        keep_entries: int = 200,
    ) -> str:
        """Append one authoritative audit event in one bounded transaction."""
        self._ensure_open()
        safe = redact_for_persistence(dict(record))
        descriptor = self._prepare_state_json(
            safe, media_type="application/vnd.gitgo.history+json",
        )
        identifier = event_id or "he_" + uuid.uuid4().hex
        logical = int(descriptor["byte_length"]) + 512
        self._reserve_authoritative_write(logical)
        with self._state_lock:
            self._state.execute("BEGIN IMMEDIATE")
            try:
                self._register_object(self._state, descriptor)
                cursor = self._state.execute(
                    """INSERT OR IGNORE INTO history_events(
                           event_id, project_name, operation, status,
                           correlation_id, parent_event_id, record_ref,
                           occurred_at, created_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        identifier,
                        str(safe.get("project_name") or ""),
                        str(safe.get("operation") or ""),
                        str(safe.get("status") or ""),
                        str(safe.get("correlation_id") or ""),
                        str(safe.get("parent_event_id") or ""),
                        str(descriptor["ref"]),
                        str(safe.get("timestamp") or _utc_now()),
                        _utc_now(),
                    ),
                )
                if cursor.rowcount and compact_threshold > 0:
                    sequence = int(cursor.lastrowid or 0)
                    interval = max(1, compact_threshold - keep_entries)
                    if sequence and sequence % interval == 0:
                        count = int(self._state.execute(
                            "SELECT COUNT(*) FROM history_events"
                        ).fetchone()[0])
                        cutoff = self._state.execute(
                            """SELECT sequence FROM history_events
                               ORDER BY sequence DESC LIMIT 1 OFFSET ?""",
                            (max(0, keep_entries - 1),),
                        ).fetchone() if count >= compact_threshold else None
                        if cutoff is not None:
                            self._state.execute(
                                "DELETE FROM history_events WHERE sequence<?",
                                (int(cutoff[0]),),
                            )
                self._state.execute("COMMIT")
            except BaseException:
                self._state.execute("ROLLBACK")
                raise
        return identifier

    def replace_history_records(self, records: list[dict]) -> None:
        """Atomically replace History for compatibility imports and tests."""
        prepared: list[tuple[str, dict, dict]] = []
        logical = 512
        for index, record in enumerate(records):
            safe = redact_for_persistence(dict(record))
            encoded = self._encoded_object(safe)
            identifier = "he_" + hashlib.sha256(
                encoded + b"\0" + str(index).encode("ascii")
            ).hexdigest()[:32]
            descriptor = self._write_blob_file(
                encoded, media_type="application/vnd.gitgo.history+json",
            )
            logical += len(encoded) + 256
            prepared.append((identifier, safe, descriptor))
        self._reserve_authoritative_write(logical)
        with self._state_lock:
            self._state.execute("BEGIN IMMEDIATE")
            try:
                self._state.execute("DELETE FROM history_events")
                for identifier, safe, descriptor in prepared:
                    self._register_object(self._state, descriptor)
                    self._state.execute(
                        """INSERT INTO history_events(
                               event_id, project_name, operation, status,
                               correlation_id, parent_event_id, record_ref,
                               occurred_at, created_at
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            identifier,
                            str(safe.get("project_name") or ""),
                            str(safe.get("operation") or ""),
                            str(safe.get("status") or ""),
                            str(safe.get("correlation_id") or ""),
                            str(safe.get("parent_event_id") or ""),
                            str(descriptor["ref"]),
                            str(safe.get("timestamp") or _utc_now()),
                            _utc_now(),
                        ),
                    )
                self._state.execute("COMMIT")
            except BaseException:
                self._state.execute("ROLLBACK")
                raise

    def load_history_records(self, *, limit: int | None = None) -> list[dict]:
        self._ensure_open()
        query = "SELECT record_ref FROM history_events ORDER BY sequence"
        parameters: tuple = ()
        if limit is not None:
            bounded = max(1, int(limit))
            query = (
                "SELECT record_ref FROM (SELECT sequence, record_ref "
                "FROM history_events ORDER BY sequence DESC LIMIT ?) "
                "ORDER BY sequence"
            )
            parameters = (bounded,)
        with self._state_lock:
            rows = self._state.execute(query, parameters).fetchall()
        return [dict(self._read_json_blob(str(row["record_ref"]))) for row in rows]

    def history_count(self) -> int:
        with self._state_lock:
            return int(self._state.execute(
                "SELECT COUNT(*) FROM history_events"
            ).fetchone()[0])

    def upsert_lesson_record(
        self, record: dict, *, scope: str, content_hash: str,
    ) -> str:
        """Insert or update one lesson; pending exact duplicates are idempotent."""
        self._ensure_open()
        if scope not in {"pending", "instance", "abstract"}:
            raise ValueError("invalid lesson scope")
        safe = redact_for_persistence(dict(record))
        lesson_id = str(safe.get("id") or "")
        if not lesson_id:
            raise ValueError("lesson id is required")
        project_name = str(safe.get("project_name") or "")
        if scope == "pending":
            with self._state_lock:
                existing = self._state.execute(
                    """SELECT lesson_id FROM lessons
                       WHERE scope='pending' AND project_name=? AND content_hash=?""",
                    (project_name, content_hash),
                ).fetchone()
            if existing is not None and str(existing["lesson_id"]) != lesson_id:
                return str(existing["lesson_id"])
        descriptor = self._prepare_state_json(
            safe, media_type="application/vnd.gitgo.lesson+json",
        )
        self._reserve_authoritative_write(int(descriptor["byte_length"]) + 512)
        now = _utc_now()
        with self._state_lock:
            self._state.execute("BEGIN IMMEDIATE")
            try:
                self._register_object(self._state, descriptor)
                self._state.execute(
                    """INSERT INTO lessons(
                           lesson_id, scope, project_name, tech_stack, category,
                           severity, content_hash, record_ref, created_at, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(lesson_id) DO UPDATE SET
                           scope=excluded.scope,
                           project_name=excluded.project_name,
                           tech_stack=excluded.tech_stack,
                           category=excluded.category,
                           severity=excluded.severity,
                           content_hash=excluded.content_hash,
                           record_ref=excluded.record_ref,
                           updated_at=excluded.updated_at""",
                    (
                        lesson_id, scope, project_name,
                        str(safe.get("tech_stack") or ""),
                        str(safe.get("category") or ""),
                        str(safe.get("severity") or "medium"),
                        content_hash, str(descriptor["ref"]),
                        str(safe.get("created_at") or now), now,
                    ),
                )
                self._state.execute("COMMIT")
            except sqlite3.IntegrityError:
                self._state.execute("ROLLBACK")
                if scope == "pending":
                    existing = self._state.execute(
                        """SELECT lesson_id FROM lessons
                           WHERE scope='pending' AND project_name=?
                           AND content_hash=?""",
                        (project_name, content_hash),
                    ).fetchone()
                    if existing is not None:
                        return str(existing["lesson_id"])
                raise
            except BaseException:
                self._state.execute("ROLLBACK")
                raise
        return lesson_id

    def load_lesson_records(
        self,
        *,
        scope: str,
        project_name: str | None = None,
        tech_stack: str | None = None,
    ) -> list[dict]:
        clauses = ["scope=?"]
        parameters: list[str] = [scope]
        if project_name is not None:
            clauses.append("project_name=?")
            parameters.append(project_name)
        if tech_stack:
            clauses.append("tech_stack=?")
            parameters.append(tech_stack)
        with self._state_lock:
            rows = self._state.execute(
                "SELECT record_ref FROM lessons WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at, lesson_id",
                tuple(parameters),
            ).fetchall()
        return [dict(self._read_json_blob(str(row["record_ref"]))) for row in rows]

    def get_lesson_record(self, lesson_id: str) -> tuple[str, dict] | None:
        with self._state_lock:
            row = self._state.execute(
                "SELECT scope, record_ref FROM lessons WHERE lesson_id=?",
                (lesson_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["scope"]), dict(
            self._read_json_blob(str(row["record_ref"]))
        )

    def delete_lesson_record(
        self, lesson_id: str, *, project_name: str | None = None,
        scopes: tuple[str, ...] = ("pending", "instance"),
    ) -> bool:
        clauses = ["lesson_id=?"]
        parameters: list[str] = [lesson_id]
        if project_name is not None:
            clauses.append("project_name=?")
            parameters.append(project_name)
        placeholders = ",".join("?" for _ in scopes)
        clauses.append(f"scope IN ({placeholders})")
        parameters.extend(scopes)
        self._reserve_authoritative_write(512)
        with self._state_lock:
            cursor = self._state.execute(
                "DELETE FROM lessons WHERE " + " AND ".join(clauses),
                tuple(parameters),
            )
        return bool(cursor.rowcount)

    def lesson_count(self, *, scope: str, project_name: str = "") -> int:
        with self._state_lock:
            return int(self._state.execute(
                "SELECT COUNT(*) FROM lessons WHERE scope=? AND project_name=?",
                (scope, project_name),
            ).fetchone()[0])

    def save_harvest_signal_records(self, records: list[dict]) -> int:
        """Upsert changed harvest signals into the shared governance bus.

        Callers may pass a complete logical store for compatibility.  Content
        addressing filters unchanged rows before reserving a transaction, so a
        lease/update never rewrites every historical signal.
        """
        self._ensure_open()
        prepared: list[tuple[dict, dict]] = []
        for record in records:
            safe = redact_for_persistence(dict(record))
            signal_id = str(safe.get("signal_id") or "")
            if not signal_id.startswith("hs_"):
                raise ValueError("harvest signal id must use hs_ prefix")
            descriptor = self._prepare_state_json(
                safe, media_type="application/vnd.gitgo.harvest-signal+json",
            )
            with self._state_lock:
                existing = self._state.execute(
                    "SELECT payload_ref FROM governance_signals WHERE signal_id=?",
                    (signal_id,),
                ).fetchone()
            if existing is not None and str(existing["payload_ref"]) == str(
                descriptor["ref"]
            ):
                continue
            prepared.append((safe, descriptor))
        if not prepared:
            return 0
        logical = sum(int(item[1]["byte_length"]) + 384 for item in prepared)
        self._reserve_authoritative_write(logical)
        now = _utc_now()
        with self._state_lock:
            self._state.execute("BEGIN IMMEDIATE")
            try:
                for safe, descriptor in prepared:
                    self._register_object(self._state, descriptor)
                    self._state.execute(
                        """INSERT INTO governance_signals(
                               signal_id, project_id, task_id, signal_type,
                               severity, payload_ref, state, lease_owner,
                               retry_count, created_at, updated_at
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(signal_id) DO UPDATE SET
                               signal_type=excluded.signal_type,
                               severity=excluded.severity,
                               payload_ref=excluded.payload_ref,
                               state=excluded.state,
                               lease_owner=excluded.lease_owner,
                               retry_count=excluded.retry_count,
                               updated_at=excluded.updated_at""",
                        (
                            str(safe["signal_id"]), self.paths.project_id,
                            None,
                            str(safe.get("signal_type") or "unknown"),
                            str(safe.get("severity") or "info"),
                            str(descriptor["ref"]),
                            str(safe.get("state") or "pending"),
                            str(safe.get("lease_id") or "") or None,
                            int(safe.get("retry_count", 0) or 0),
                            str(safe.get("created_at") or now),
                            str(safe.get("updated_at") or now),
                        ),
                    )
                self._state.execute("COMMIT")
            except BaseException:
                self._state.execute("ROLLBACK")
                raise
        return len(prepared)

    def load_harvest_signal_records(self) -> list[dict]:
        self._ensure_open()
        with self._state_lock:
            rows = self._state.execute(
                """SELECT payload_ref FROM governance_signals
                   WHERE project_id=? AND signal_id LIKE 'hs_%'
                   ORDER BY created_at, signal_id""",
                (self.paths.project_id,),
            ).fetchall()
        return [dict(self._read_json_blob(str(row["payload_ref"]))) for row in rows]

    def harvest_signal_count(self) -> int:
        with self._state_lock:
            return int(self._state.execute(
                """SELECT COUNT(*) FROM governance_signals
                   WHERE project_id=? AND signal_id LIKE 'hs_%'""",
                (self.paths.project_id,),
            ).fetchone()[0])

    def append_event(
        self,
        *,
        event_type: str,
        summary: str,
        severity: str = "info",
        task_id: str | None = None,
        session_id: str | None = None,
        sequence: int | None = None,
        payload_ref: str | None = None,
        occurred_at: str | None = None,
    ) -> bool:
        """Append a bounded semantic event.

        Returns ``False`` when optional observability is throttled.  Core task
        execution must not fail because verbose telemetry cannot be retained.
        """
        self._ensure_open()
        encoded = summary.encode("utf-8")
        logical = len(encoded) + len(payload_ref or "") + 256
        if logical > self.policy.max_observability_record_bytes:
            raise ValueError("observability event is too large; store the body in CAS")
        health = self.check_health()
        estimated_write = self._estimated_sqlite_write(logical)
        if (
            health.observability_bytes + estimated_write > self.policy.observability_max_bytes
            or health.total_bytes + estimated_write > self.policy.total_max_bytes
            or not self._global_sqlite_budget.reserve(estimated_write)
            or not self._observability_budget.reserve(logical)
        ):
            self._publish_degraded("observability_write_throttled")
            return False
        now = _utc_now()
        with self._observability_lock:
            self._observability.execute(
                """INSERT INTO events(
                       event_id, task_id, session_id, sequence, event_type, severity,
                       summary, payload_ref, occurred_at, recorded_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    task_id,
                    session_id,
                    sequence,
                    event_type,
                    severity,
                    summary,
                    payload_ref,
                    occurred_at or now,
                    now,
                ),
            )
        return True

    def _reserve_observability_write(
        self, logical: int, *, publish_degraded: bool = True,
    ) -> bool:
        """Reserve one optional observability transaction.

        Observability is intentionally lossy under pressure.  The reservation
        covers the complete SQLite file family and the shared physical-write
        budget; callers must not retry in a hot loop when this returns false.
        """
        health = self.check_health()
        estimated_write = self._estimated_sqlite_write(logical)
        allowed = not (
            health.observability_bytes + estimated_write
            > self.policy.observability_max_bytes
            or health.total_bytes + estimated_write > self.policy.total_max_bytes
            or not self._global_sqlite_budget.reserve(estimated_write)
            or not self._observability_budget.reserve(logical)
        )
        if not allowed and publish_degraded:
            self._publish_degraded("observability_write_throttled")
        return allowed

    def trace_last_sequence(self, trace_id: str) -> int:
        self._ensure_open()
        with self._observability_lock:
            row = self._observability.execute(
                "SELECT MAX(sequence) FROM trace_events WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
        return int(row[0] or 0)

    def append_trace_record(
        self,
        trace_id: str,
        record: dict,
        *,
        detail: object | None = None,
    ) -> dict | None:
        """Persist one bounded trace record in the observability database.

        Small event projections stay inline to avoid creating thousands of
        tiny CAS files.  Only the optional large detail body is content
        addressed.  A throttled observer returns ``None`` and never blocks the
        Agent completion or governance path.
        """
        self._ensure_open()
        safe_record = redact_for_persistence(dict(record))
        safe_record.pop("_trace_detail", None)
        detail_ref: str | None = None
        detail_bytes = 0
        detail_encoded: bytes | None = None
        if detail is not None:
            detail_encoded = self._encoded_object(detail)
            detail_bytes = len(detail_encoded)
            if detail_bytes > self.policy.max_cas_object_bytes:
                raise StorageBlocked("cas_object_too_large")

        encoded = json.dumps(
            safe_record, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        ).encode("utf-8")
        inline_logical = len(encoded) + 384
        if inline_logical > self.policy.max_observability_record_bytes:
            raise ValueError("trace event is too large; move the body to detail")
        # CAS is part of the same optional observability write.  Reserving only
        # the inline row would let large detail objects bypass both the global
        # SSD-write envelope and the observability byte-rate ceiling.
        logical = inline_logical + detail_bytes
        if not self._reserve_observability_write(logical):
            return None

        if detail_encoded is not None:
            descriptor = self._write_blob_file(
                detail_encoded, media_type="application/vnd.gitgo.trace-detail+json",
            )
            detail_ref = "trace-object:" + str(descriptor["digest"])
            safe_record["detail_ref"] = detail_ref
            safe_record["detail_bytes"] = detail_bytes

        occurred_at = str(safe_record.get("time") or _utc_now())
        event_type = str(safe_record.get("event") or "trace_event")
        severity = str(safe_record.get("severity") or "info")
        proposed = int(safe_record.get("seq", 0) or 0)
        with self._observability_lock:
            self._observability.execute("BEGIN IMMEDIATE")
            try:
                row = self._observability.execute(
                    "SELECT MAX(sequence) FROM trace_events WHERE trace_id=?",
                    (trace_id,),
                ).fetchone()
                sequence = max(proposed, int(row[0] or 0) + 1)
                safe_record["seq"] = sequence
                safe_record["trace_id"] = trace_id
                record_json = json.dumps(
                    safe_record, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), default=str,
                )
                self._observability.execute(
                    """INSERT INTO trace_events(
                           trace_id, sequence, event_type, severity, record_json,
                           detail_ref, detail_bytes, occurred_at, monotonic_ns
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        trace_id, sequence, event_type, severity, record_json,
                        detail_ref, detail_bytes, occurred_at,
                        int(safe_record.get("monotonic_ns", 0) or 0),
                    ),
                )
                self._observability.execute("COMMIT")
            except BaseException:
                self._observability.execute("ROLLBACK")
                raise
        return safe_record

    def read_trace_activity(
        self, event_types: Iterable[str], *, limit: int = 20,
        before: tuple[str, str, int] | None = None,
    ) -> list[dict]:
        """Bounded read projection of existing events; does not copy them into History."""
        self._ensure_open()
        names = sorted(set(str(item) for item in event_types))
        bounded = max(0, min(1000, int(limit)))
        if not names or not bounded:
            return []
        placeholders = ",".join("?" for _ in names)
        cursor_sql = ""
        parameters: list = [*names]
        if before is not None:
            occurred_at, trace_id, sequence = before
            cursor_sql = (
                "AND (occurred_at < ? OR (occurred_at = ? AND "
                "(trace_id < ? OR (trace_id = ? AND sequence < ?)))) "
            )
            parameters.extend([occurred_at, occurred_at, trace_id, trace_id, int(sequence)])
        parameters.append(bounded)
        with self._observability_lock:
            rows = self._observability.execute(
                f"SELECT trace_id, sequence, event_type, severity, occurred_at, record_json "
                f"FROM trace_events WHERE event_type IN ({placeholders}) "
                + cursor_sql
                + "ORDER BY occurred_at DESC, trace_id DESC, sequence DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [{**dict(row), "record": json.loads(row["record_json"])} for row in rows]

    def read_trace_events(
        self, trace_id: str, *, after_seq: int = 0, limit: int = 500,
        process_id: str = "", include_deltas: bool = True,
    ) -> dict:
        self._ensure_open()
        bounded = max(1, min(int(limit), 5000))
        where = "trace_id=?"
        parameters: list = [trace_id]
        if process_id:
            where += " AND json_extract(record_json, '$.process_id')=?"
            parameters.append(process_id)
        if not include_deltas:
            where += " AND event_type NOT IN ('text_delta', 'reasoning_delta', 'toolcall_delta')"
        with self._observability_lock:
            rows = self._observability.execute(
                f"""SELECT sequence, record_json FROM trace_events
                   WHERE {where} AND sequence>? ORDER BY sequence LIMIT ?""",
                (*parameters, int(after_seq), bounded),
            ).fetchall()
            last = self._observability.execute(
                f"SELECT MAX(sequence) FROM trace_events WHERE {where}",
                parameters,
            ).fetchone()
        events = [json.loads(str(row["record_json"])) for row in rows]
        last_sequence = int(last[0] or 0)
        next_sequence = int(events[-1]["seq"]) if events else int(after_seq)
        return {
            "trace_id": trace_id,
            "events": events,
            "next_seq": next_sequence,
            "has_more": next_sequence < last_sequence,
        }

    def read_trace_summary(self, trace_id: str, *, process_id: str = "") -> dict:
        """Return exact semantic-event counts without materializing a verbose trace."""
        self._ensure_open()
        where = "trace_id=?"
        parameters: list = [trace_id]
        if process_id:
            where += " AND json_extract(record_json, '$.process_id')=?"
            parameters.append(process_id)
        with self._observability_lock:
            totals = self._observability.execute(
                f"""SELECT COUNT(*), MIN(sequence), MAX(sequence),
                            MIN(occurred_at), MAX(occurred_at)
                     FROM trace_events WHERE {where}""",
                parameters,
            ).fetchone()
            counts = self._observability.execute(
                f"""SELECT event_type, COUNT(*) AS event_count
                     FROM trace_events WHERE {where}
                     GROUP BY event_type ORDER BY event_type""",
                parameters,
            ).fetchall()
            recent = self._observability.execute(
                f"""SELECT record_json FROM trace_events WHERE {where}
                     ORDER BY sequence DESC LIMIT 8""",
                parameters,
            ).fetchall()
        return {
            "trace_id": trace_id,
            "event_count": int(totals[0] or 0),
            "first_seq": int(totals[1] or 0),
            "last_seq": int(totals[2] or 0),
            "first_at": str(totals[3] or ""),
            "last_at": str(totals[4] or ""),
            "event_counts": {str(row["event_type"]): int(row["event_count"]) for row in counts},
            "recent_events": list(reversed([
                json.loads(str(row["record_json"])) for row in recent
            ])),
            "complete": True,
        }

    def import_trace_records(
        self, trace_id: str,
        records: list[tuple[dict, object | None]],
    ) -> int | None:
        """Import one legacy trace in bounded, idempotent transactions.

        Legacy token fragments are coalesced by the caller.  Original, possibly
        sparse sequence numbers are preserved.  Existing sequences are removed
        before budgeting, so a later daemon start can resume a partial import
        without spending the write allowance on rows already committed.

        Each batch is reserved before either its SQLite rows or detail CAS
        objects are written.  ``None`` means pressure deferred the remaining
        suffix; committed prefixes stay valid and the caller must retain the
        legacy source.  An integer means every supplied sequence is present.
        """
        self._ensure_open()
        with self._observability_lock:
            existing_sequences = {
                int(row[0])
                for row in self._observability.execute(
                    "SELECT sequence FROM trace_events WHERE trace_id=?",
                    (trace_id,),
                ).fetchall()
            }

        prepared: list[tuple[dict, bytes | None, str | None, int, str, int]] = []
        for raw_record, detail in records:
            safe = redact_for_persistence(dict(raw_record))
            safe.pop("_trace_detail", None)
            safe["trace_id"] = trace_id
            sequence = int(safe.get("seq", 0) or 0)
            if sequence <= 0 or sequence in existing_sequences:
                continue
            detail_ref: str | None = None
            detail_bytes = 0
            encoded_detail: bytes | None = None
            inline_encoded = json.dumps(
                safe, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str,
            ).encode("utf-8")
            if len(inline_encoded) + 384 > self.policy.max_observability_record_bytes:
                # Early Trace versions occasionally put a complete outcome or
                # terminal snapshot inline.  Keep the searchable event identity
                # in SQLite and move the lossless legacy body to CAS; raising
                # here would make every resumable pass stall on the same row.
                overflow: dict[str, object] = {"legacy_record": safe}
                if detail is not None:
                    overflow["legacy_detail"] = detail
                encoded_detail = self._encoded_object(overflow)
                projection_keys = (
                    "schema_version", "trace_id", "seq", "time",
                    "monotonic_ns", "event", "severity", "process_id",
                    "agent_task_id", "task_id", "tool_call_id",
                )
                safe = {
                    key: safe[key] for key in projection_keys if key in safe
                }
                safe["legacy_record_in_detail"] = True
            elif detail is not None:
                encoded_detail = self._encoded_object(detail)
            if encoded_detail is not None:
                detail_bytes = len(encoded_detail)
                if detail_bytes > self.policy.max_cas_object_bytes:
                    raise StorageBlocked("cas_object_too_large")
                detail_ref = "trace-object:" + hashlib.sha256(
                    encoded_detail
                ).hexdigest()
                safe["detail_ref"] = detail_ref
                safe["detail_bytes"] = detail_bytes
            record_json = json.dumps(
                safe, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str,
            )
            if len(record_json.encode("utf-8")) + 384 > (
                self.policy.max_observability_record_bytes
            ):
                raise ValueError("legacy trace event exceeds bounded record size")
            logical = len(record_json.encode("utf-8")) + detail_bytes + 192
            prepared.append((
                safe, encoded_detail, detail_ref, detail_bytes, record_json, logical,
            ))
        if not prepared:
            return 0

        # A batch uses at most one eighth of the minute envelope (normally
        # 2 MiB).  This keeps the Daemon responsive, leaves room for live Trace
        # events, and bounds WAL growth/checkpoint work during a legacy import.
        global_logical_limit = (
            self.policy.sqlite_estimated_bytes_per_minute
            // self.policy.sqlite_write_amplification_reserve
        )
        batch_limit = max(4096, min(
            2 * 1024**2,
            self.policy.observability_logical_bytes_per_minute // 8,
            global_logical_limit // 8,
        ))
        inserted = 0
        cursor = 0
        while cursor < len(prepared):
            batch = []
            logical = 512
            while cursor < len(prepared):
                candidate = prepared[cursor]
                candidate_logical = candidate[5]
                if batch and logical + candidate_logical > batch_limit:
                    break
                batch.append(candidate)
                logical += candidate_logical
                cursor += 1
                # A single large detail is allowed only when it fits both
                # minute envelopes; otherwise it can never be imported safely.
                if logical >= batch_limit:
                    break
            if not self._reserve_observability_write(logical):
                return None

            # CAS writes are immutable and idempotent.  They happen only after
            # the complete batch has reserved its estimated physical writes.
            for _safe, encoded_detail, _ref, _bytes, _json, _logical in batch:
                if encoded_detail is not None:
                    self._write_blob_file(
                        encoded_detail,
                        media_type="application/vnd.gitgo.trace-detail+json",
                    )

            with self._observability_lock:
                self._observability.execute("BEGIN IMMEDIATE")
                try:
                    for (
                        safe, _encoded_detail, detail_ref, detail_bytes,
                        record_json, _logical,
                    ) in batch:
                        row_cursor = self._observability.execute(
                            """INSERT OR IGNORE INTO trace_events(
                                   trace_id, sequence, event_type, severity,
                                   record_json, detail_ref, detail_bytes,
                                   occurred_at, monotonic_ns
                               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                trace_id, int(safe["seq"]),
                                str(safe.get("event") or "trace_event"),
                                str(safe.get("severity") or "info"),
                                record_json, detail_ref, detail_bytes,
                                str(safe.get("time") or _utc_now()),
                                int(safe.get("monotonic_ns", 0) or 0),
                            ),
                        )
                        inserted += max(0, int(row_cursor.rowcount or 0))
                    self._observability.execute("COMMIT")
                except BaseException:
                    self._observability.execute("ROLLBACK")
                    raise
        return inserted

    def list_traces(self, *, limit: int = 100) -> list[dict]:
        self._ensure_open()
        bounded = max(1, min(int(limit), 1000))
        with self._observability_lock:
            rows = self._observability.execute(
                """SELECT trace_id, COUNT(*) AS event_count,
                          SUM(LENGTH(record_json)) AS logical_bytes,
                          MAX(occurred_at) AS modified_at
                   FROM trace_events GROUP BY trace_id
                   ORDER BY modified_at DESC LIMIT ?""",
                (bounded,),
            ).fetchall()
        return [
            {
                "trace_id": str(row["trace_id"]),
                "event_count": int(row["event_count"] or 0),
                "size": int(row["logical_bytes"] or 0),
                "modified_at": str(row["modified_at"] or ""),
            }
            for row in rows
        ]

    def observability_counts(self) -> dict[str, int]:
        """Bounded diagnostic surface; callers never receive a connection."""
        with self._observability_lock:
            return {
                table: int(self._observability.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0])
                for table in (
                    "events", "event_rollups", "trace_events",
                    "storage_metrics",
                )
            }

    def read_trace_detail(self, ref: str) -> object:
        value = str(ref)
        if value.startswith("trace-object:"):
            value = "sha256:" + value.removeprefix("trace-object:")
        return self._read_json_blob(value)

    def record_storage_metric(self, *, force: bool = False) -> bool:
        """Sample physical file-family sizes at a low, monotonic cadence."""
        self._ensure_open()
        now_monotonic = time.monotonic()
        if (
            not force
            and now_monotonic - self._last_metric_sample_at
            < self.policy.storage_metric_interval_seconds
        ):
            return False
        health = self.check_health()
        logical = 256
        if not self._reserve_observability_write(
            logical, publish_degraded=False,
        ):
            return False
        with self._observability_lock:
            self._observability.execute(
                """INSERT INTO storage_metrics(
                       sampled_at, state_bytes, observability_bytes, cas_bytes,
                       free_bytes, health_level
                   ) VALUES(?, ?, ?, ?, ?, ?)""",
                (
                    _utc_now(), health.state_bytes, health.observability_bytes,
                    health.cas_bytes, health.free_bytes, health.level.value,
                ),
            )
        self._last_metric_sample_at = now_monotonic
        return True

    def maintain_observability(self, *, force: bool = False) -> dict:
        """Run bounded retention at startup or an explicit idle boundary.

        There is no per-event delete loop and no full VACUUM.  One transaction
        rolls up and removes at most ``observability_retention_batch_rows`` old
        rows, then records its durable cadence in the observability database.
        """
        self._ensure_open()
        now = datetime.now(timezone.utc)
        with self._observability_lock:
            row = self._observability.execute(
                """SELECT completed_at FROM observability_maintenance
                   WHERE maintenance_key='retention'"""
            ).fetchone()
        if row is not None and not force:
            try:
                completed = datetime.fromisoformat(str(row[0]))
                if (now - completed).total_seconds() < (
                    self.policy.observability_maintenance_interval_seconds
                ):
                    return {"ran": False, "deleted": 0}
            except ValueError:
                pass

        cutoff = (now - timedelta(
            days=self.policy.observability_retention_days
        )).isoformat()
        batch = self.policy.observability_retention_batch_rows
        with self._observability_lock:
            has_expired = self._observability.execute(
                """SELECT EXISTS(
                       SELECT 1 FROM events WHERE occurred_at<?
                       UNION ALL
                       SELECT 1 FROM trace_events WHERE occurred_at<?
                       UNION ALL
                       SELECT 1 FROM storage_metrics WHERE sampled_at<?
                   )""",
                (cutoff, cutoff, cutoff),
            ).fetchone()
        if not bool(has_expired and has_expired[0]):
            # An empty/new database must not spend a write-budget token merely
            # to remember that there was nothing to delete.  Rechecking is a
            # read-only startup cost and cannot wear the disk.
            return {"ran": True, "deleted": 0, "cutoff": cutoff}
        logical = max(4096, batch * 64)
        if not self._reserve_observability_write(
            logical, publish_degraded=False,
        ):
            return {"ran": False, "deleted": 0, "throttled": True}
        with self._observability_lock:
            self._observability.execute("BEGIN IMMEDIATE")
            try:
                event_rows = self._observability.execute(
                    """SELECT event_id, event_type, severity, occurred_at
                       FROM events WHERE occurred_at<? ORDER BY occurred_at LIMIT ?""",
                    (cutoff, batch),
                ).fetchall()
                for event_type, severity, bucket, count, first_at, last_at in self._observability.execute(
                    """SELECT event_type, severity, substr(occurred_at, 1, 10),
                              COUNT(*), MIN(occurred_at), MAX(occurred_at)
                       FROM events WHERE event_id IN (
                           SELECT event_id FROM events WHERE occurred_at<?
                           ORDER BY occurred_at LIMIT ?
                       ) GROUP BY event_type, severity, substr(occurred_at, 1, 10)""",
                    (cutoff, batch),
                ).fetchall():
                    bucket_start = str(bucket) + "T00:00:00+00:00"
                    self._observability.execute(
                        """INSERT INTO event_rollups(
                               bucket_start, bucket_seconds, event_type, severity,
                               event_count, first_at, last_at, sample_ref
                           ) VALUES(?, 86400, ?, ?, ?, ?, ?, NULL)
                           ON CONFLICT(bucket_start, bucket_seconds, event_type, severity)
                           DO UPDATE SET
                               event_count=event_count+excluded.event_count,
                               first_at=MIN(first_at, excluded.first_at),
                               last_at=MAX(last_at, excluded.last_at)""",
                        (
                            bucket_start, str(event_type), str(severity),
                            int(count), str(first_at), str(last_at),
                        ),
                    )
                if event_rows:
                    self._observability.executemany(
                        "DELETE FROM events WHERE event_id=?",
                        [(str(item["event_id"]),) for item in event_rows],
                    )
                remaining = max(0, batch - len(event_rows))
                trace_rows = self._observability.execute(
                    """SELECT trace_id, sequence FROM trace_events
                       WHERE occurred_at<? ORDER BY occurred_at LIMIT ?""",
                    (cutoff, remaining),
                ).fetchall() if remaining else []
                if trace_rows:
                    self._observability.executemany(
                        "DELETE FROM trace_events WHERE trace_id=? AND sequence=?",
                        [(str(item["trace_id"]), int(item["sequence"])) for item in trace_rows],
                    )
                metric_rows = self._observability.execute(
                    """SELECT sampled_at FROM storage_metrics
                       WHERE sampled_at<? ORDER BY sampled_at LIMIT ?""",
                    (cutoff, max(0, batch - len(event_rows) - len(trace_rows))),
                ).fetchall()
                if metric_rows:
                    self._observability.executemany(
                        "DELETE FROM storage_metrics WHERE sampled_at=?",
                        [(str(item["sampled_at"]),) for item in metric_rows],
                    )
                deleted = len(event_rows) + len(trace_rows) + len(metric_rows)
                self._observability.execute(
                    """INSERT INTO observability_maintenance(
                           maintenance_key, completed_at, detail_json
                       ) VALUES('retention', ?, ?)
                       ON CONFLICT(maintenance_key) DO UPDATE SET
                           completed_at=excluded.completed_at,
                           detail_json=excluded.detail_json""",
                    (
                        now.isoformat(),
                        json.dumps({"cutoff": cutoff, "deleted": deleted}),
                    ),
                )
                self._observability.execute("COMMIT")
            except BaseException:
                self._observability.execute("ROLLBACK")
                raise
        return {"ran": True, "deleted": deleted, "cutoff": cutoff}

    @staticmethod
    def _digest_from_ref(value: object) -> str | None:
        ref = str(value or "")
        if ref.startswith("sha256:"):
            digest = ref.removeprefix("sha256:")
        elif ref.startswith("trace-object:"):
            digest = ref.removeprefix("trace-object:")
        else:
            return None
        if len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            return None
        return digest

    def _referenced_cas_digests(self) -> set[str]:
        """Mark the transitive CAS closure used by current durable state.

        Older checkpoints embedded split-object refs inside ``metadata_ref``
        before ``object_refs`` was populated.  Reading those metadata objects is
        therefore part of marking, not an optional migration convenience.
        """
        direct, owned, nested = self._cas_reference_inventory()
        return direct | owned | nested

    @staticmethod
    def _nested_cas_digests(value: object) -> set[str]:
        found: set[str] = set()
        pending = [value]
        while pending:
            current = pending.pop()
            if isinstance(current, dict):
                pending.extend(current.values())
            elif isinstance(current, (list, tuple)):
                pending.extend(current)
            else:
                digest = StorageRuntime._digest_from_ref(current)
                if digest:
                    found.add(digest)
        return found

    def _cas_reference_inventory(self) -> tuple[set[str], set[str], set[str]]:
        """Return direct-column, explicit-owner and legacy nested references."""
        state_columns = (
            ("sessions", "metadata_ref"),
            ("tasks", "contract_ref"), ("tasks", "outcome_ref"),
            ("messages", "content_ref"), ("messages", "reasoning_ref"),
            ("tool_calls", "arguments_ref"), ("tool_calls", "result_ref"),
            ("receipts", "evidence_ref"),
            ("governance_signals", "payload_ref"),
            ("test_evidence", "payload_ref"),
            ("provider_attempts", "usage_ref"),
            ("mailbox_messages", "payload_ref"),
            ("worktrees", "metadata_ref"),
            ("dependency_nodes", "metadata_ref"),
            ("dependency_edges", "evidence_ref"),
            ("storage_kv", "value_ref"),
            ("session_events", "payload_ref"),
            ("session_processes", "runtime_state_ref"),
            ("process_dependencies", "artifact_ref"),
            ("history_events", "record_ref"),
            ("lessons", "record_ref"),
            ("tool_result_objects", "content_ref"),
            ("session_lineage", "snapshot_ref"),
            ("custom_tool_versions", "spec_ref"),
            ("custom_tool_versions", "source_ref"),
        )
        observability_columns = (
            ("events", "payload_ref"),
            ("event_rollups", "sample_ref"),
            ("trace_events", "detail_ref"),
        )
        direct: set[str] = set()
        metadata_digests: set[str] = set()
        with self._state_lock:
            for table, column in state_columns:
                for row in self._state.execute(
                    f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"
                ).fetchall():
                    digest = self._digest_from_ref(row[0])
                    if digest:
                        direct.add(digest)
                        if table == "sessions" and column == "metadata_ref":
                            metadata_digests.add(digest)
            owned = {
                str(row[0])
                for row in self._state.execute(
                    "SELECT DISTINCT digest FROM object_refs"
                ).fetchall()
                if self._digest_from_ref(f"sha256:{row[0]}")
            }
        with self._observability_lock:
            for table, column in observability_columns:
                for row in self._observability.execute(
                    f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"
                ).fetchall():
                    digest = self._digest_from_ref(row[0])
                    if digest:
                        direct.add(digest)
        nested: set[str] = set()
        for digest in metadata_digests:
            path = self.paths.cas_dir / digest[:2] / digest[2:]
            if not path.exists() and self._restore_canonical_blob(digest) is None:
                continue
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            nested.update(self._nested_cas_digests(metadata))
        return direct, owned, nested

    def _refresh_cas_reference_status(self, *, force: bool = False) -> None:
        """Audit live references without scanning unrelated CAS objects."""
        now = time.monotonic()
        if (
            not force
            and now - self._last_cas_reference_check_at
            < self.policy.cas_rescan_interval_seconds
        ):
            return
        direct, owned, nested = self._cas_reference_inventory()
        reasons: dict[str, str] = {}
        # Later classes are stronger: a digest referenced by both nested
        # metadata and an authoritative column must remain a direct failure.
        for kind, digests in (("nested", nested), ("owned", owned), ("direct", direct)):
            for digest in digests:
                path = self.paths.cas_dir / digest[:2] / digest[2:]
                if path.exists() or self._restore_canonical_blob(digest) is not None:
                    continue
                reasons[digest] = f"cas_{kind}_reference_missing:{digest}"
        with self._health_lock:
            self._cas_reference_reasons = reasons
            self._last_cas_reference_check_at = now

    def maintain_cas(self, *, force: bool = False) -> dict:
        """Bounded mark-and-sweep with a grace period for crash orphans.

        CAS files are written before their referencing transaction.  The grace
        window guarantees a concurrent or recently crashed writer cannot have
        its object reclaimed.  No VACUUM and no per-write deletion is used.
        """
        self._ensure_open()
        now_monotonic = time.monotonic()
        if (
            not force
            and now_monotonic - self._last_cas_gc_at
            < self.policy.cas_gc_interval_seconds
        ):
            return {"ran": False, "deleted": 0}
        self._last_cas_gc_at = now_monotonic
        marked = self._referenced_cas_digests()
        cutoff = time.time() - self.policy.cas_gc_grace_days * 86400
        candidates: list[tuple[str, Path, int]] = []
        for prefix in self.paths.cas_dir.iterdir():
            if not prefix.is_dir() or len(prefix.name) != 2:
                continue
            for path in prefix.iterdir():
                digest = prefix.name + path.name
                if (
                    len(digest) == 64
                    and digest not in marked
                    and path.is_file()
                    and path.stat().st_mtime < cutoff
                ):
                    candidates.append((digest, path, path.stat().st_size))
                    if len(candidates) >= self.policy.cas_gc_batch_objects:
                        break
            if len(candidates) >= self.policy.cas_gc_batch_objects:
                break
        if not candidates:
            return {"ran": True, "deleted": 0}
        self._reserve_authoritative_write(max(4096, len(candidates) * 64))
        deleted: list[str] = []
        reclaimed = 0
        for digest, path, size in candidates:
            try:
                path.unlink()
                deleted.append(digest)
                reclaimed += size
            except FileNotFoundError:
                deleted.append(digest)
            except OSError:
                continue
        if deleted:
            with self._state_lock:
                self._state.execute("BEGIN IMMEDIATE")
                try:
                    self._state.executemany(
                        "DELETE FROM object_refs WHERE digest=?",
                        [(digest,) for digest in deleted],
                    )
                    self._state.executemany(
                        "DELETE FROM objects WHERE digest=?",
                        [(digest,) for digest in deleted],
                    )
                    self._state.execute("COMMIT")
                except BaseException:
                    self._state.execute("ROLLBACK")
                    raise
            with self._health_lock:
                if self._cas_bytes_hint is not None:
                    self._cas_bytes_hint = max(0, self._cas_bytes_hint - reclaimed)
        return {
            "ran": True, "deleted": len(deleted),
            "reclaimed_bytes": reclaimed,
        }

    def _measure(self) -> tuple[int, int, int, int]:
        state = _file_family_bytes(self.paths.state_db)
        observability = _file_family_bytes(self.paths.observability_db)
        now = time.monotonic()
        if (
            self._cas_bytes_hint is None
            or now - self._last_cas_scan_at >= self.policy.cas_rescan_interval_seconds
        ):
            self._cas_bytes_hint = _directory_bytes(self.paths.cas_dir)
            self._last_cas_scan_at = now
        cas = self._cas_bytes_hint
        free = shutil.disk_usage(self.paths.project_root).free
        return state, observability, cas, free

    def _health_from_measurement(self) -> StorageHealth:
        state, observability, cas, free = self._measure()
        total = state + observability + cas
        reasons: list[str] = []
        level = StorageHealthLevel.OK

        def raise_level(candidate: StorageHealthLevel) -> None:
            nonlocal level
            order = {
                StorageHealthLevel.OK: 0,
                StorageHealthLevel.WARNING: 1,
                StorageHealthLevel.DEGRADED: 2,
                StorageHealthLevel.BLOCKED: 3,
            }
            if order[candidate] > order[level]:
                level = candidate

        if free < self.policy.minimum_free_bytes:
            reasons.append("low_disk_space")
            raise_level(StorageHealthLevel.BLOCKED)
        for name, size, maximum in (
            ("state", state, self.policy.state_max_bytes),
            ("observability", observability, self.policy.observability_max_bytes),
            ("cas", cas, self.policy.cas_max_bytes),
            ("total", total, self.policy.total_max_bytes),
        ):
            ratio = size / maximum
            if ratio >= 1:
                reasons.append(f"{name}_capacity_exceeded")
                raise_level(
                    StorageHealthLevel.BLOCKED
                    if name in {"state", "total"}
                    else StorageHealthLevel.DEGRADED
                )
            elif ratio >= self.policy.critical_ratio:
                reasons.append(f"{name}_capacity_critical")
                raise_level(StorageHealthLevel.DEGRADED)
            elif ratio >= self.policy.warning_ratio:
                reasons.append(f"{name}_capacity_warning")
                raise_level(StorageHealthLevel.WARNING)
        now_monotonic = time.monotonic()
        self._transient_degradations = {
            reason: expires_at
            for reason, expires_at in self._transient_degradations.items()
            if expires_at > now_monotonic
        }
        if self._transient_degradations:
            reasons.extend(sorted(self._transient_degradations))
            raise_level(StorageHealthLevel.DEGRADED)
        for database, reason in sorted(self._integrity_reasons.items()):
            reasons.append(f"{database}_integrity_failed:{reason}")
            raise_level(
                StorageHealthLevel.BLOCKED
                if database == "state" else StorageHealthLevel.DEGRADED
            )
        for reason in sorted(self._cas_reference_reasons.values()):
            reasons.append(reason)
            raise_level(
                StorageHealthLevel.BLOCKED
                if reason.startswith(("cas_direct_", "cas_owned_"))
                else StorageHealthLevel.DEGRADED
            )
        return StorageHealth(
            level=level,
            checked_at=_utc_now(),
            project_id=self.paths.project_id,
            state_bytes=state,
            observability_bytes=observability,
            cas_bytes=cas,
            total_bytes=total,
            free_bytes=free,
            reasons=tuple(reasons),
        )

    def check_health(self, *, force_publish: bool = False) -> StorageHealth:
        self._ensure_open()
        self._refresh_integrity_status(force=force_publish)
        self._refresh_cas_reference_status(force=force_publish)
        with self._health_lock:
            health = self._health_from_measurement()
            previous = self._last_health
            if (
                previous is not None
                and previous.level != StorageHealthLevel.OK
                and health.level == StorageHealthLevel.OK
            ):
                maximum_ratio = max(
                    health.state_bytes / self.policy.state_max_bytes,
                    health.observability_bytes / self.policy.observability_max_bytes,
                    health.cas_bytes / self.policy.cas_max_bytes,
                    health.total_bytes / self.policy.total_max_bytes,
                )
                free_recovered = health.free_bytes >= int(
                    self.policy.minimum_free_bytes * 1.10
                )
                if maximum_ratio >= self.policy.recovery_ratio or not free_recovered:
                    health = StorageHealth(
                        level=StorageHealthLevel.WARNING,
                        checked_at=health.checked_at,
                        project_id=health.project_id,
                        state_bytes=health.state_bytes,
                        observability_bytes=health.observability_bytes,
                        cas_bytes=health.cas_bytes,
                        total_bytes=health.total_bytes,
                        free_bytes=health.free_bytes,
                        reasons=("storage_recovery_hysteresis",),
                    )
            changed = previous is None or previous.level != health.level or previous.reasons != health.reasons
            self._last_health = health
            if force_publish or changed:
                self._write_health_snapshot(health)
                if self._health_listener is not None:
                    self._health_listener(
                        {"event": "storage_health", "storage": health.to_dict()}
                    )
                if health.level != StorageHealthLevel.OK:
                    self._stderr_health(health)
            return health

    def _refresh_integrity_status(self, *, force: bool = False) -> None:
        """Low-frequency corruption detection; never run on every write."""
        now = time.monotonic()
        if (
            not force
            and now - self._last_integrity_check_at
            < self.policy.integrity_check_interval_seconds
        ):
            return
        # Corruption is sticky for this connection lifetime; an unrelated read
        # or successful sample must not declare a known damaged database healthy.
        reasons: dict[str, str] = dict(self._integrity_reasons)
        with self._state_lock:
            reason = reasons.get("state") or _quick_check(self._state)
            if reason:
                reasons["state"] = reason
        with self._observability_lock:
            reason = reasons.get("observability") or _quick_check(self._observability)
            if reason:
                reasons["observability"] = reason
        self._integrity_reasons = reasons
        self._last_integrity_check_at = now

    def _publish_degraded(self, reason: str) -> None:
        with self._health_lock:
            # Keep the signal visible for at least one complete limiter window.
            # A normal state write or Dashboard poll must not erase it
            # immediately after an observability write was rejected.
            self._transient_degradations[reason] = time.monotonic() + 60.0
            measured = self._health_from_measurement()
            health = StorageHealth(
                level=max(
                    measured.level,
                    StorageHealthLevel.DEGRADED,
                    key=lambda item: list(StorageHealthLevel).index(item),
                ),
                checked_at=measured.checked_at,
                project_id=measured.project_id,
                state_bytes=measured.state_bytes,
                observability_bytes=measured.observability_bytes,
                cas_bytes=measured.cas_bytes,
                total_bytes=measured.total_bytes,
                free_bytes=measured.free_bytes,
                reasons=tuple(dict.fromkeys((*measured.reasons, reason))),
            )
            previous = self._last_health
            self._last_health = health
            if previous is None or previous.level != health.level or previous.reasons != health.reasons:
                self._write_health_snapshot(health)
                if self._health_listener is not None:
                    self._health_listener(
                        {"event": "storage_health", "storage": health.to_dict()}
                    )
                self._stderr_health(health)

    def _write_health_snapshot(self, health: StorageHealth) -> None:
        temporary = self.paths.health_file.with_name(
            self.paths.health_file.name + f".{uuid.uuid4().hex}.tmp"
        )
        payload = json.dumps(
            health.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.paths.health_file)
        finally:
            temporary.unlink(missing_ok=True)

    def _stderr_health(self, health: StorageHealth) -> None:
        now = time.monotonic()
        if now - self._last_stderr_at < self.policy.health_stderr_interval_seconds:
            return
        self._last_stderr_at = now
        print(
            "gitgo storage " + health.level.value + ": " + ", ".join(health.reasons),
            file=sys.stderr,
        )

    def diagnostics(self) -> dict:
        self._ensure_open()
        health = self.check_health()
        with self._state_lock, self._observability_lock:
            state_pragmas = {
                name: self._state.execute(f"PRAGMA {name}").fetchone()[0]
                for name in ("journal_mode", "synchronous", "foreign_keys", "max_page_count")
            }
            observability_pragmas = {
                name: self._observability.execute(f"PRAGMA {name}").fetchone()[0]
                for name in ("journal_mode", "synchronous", "foreign_keys", "max_page_count")
            }
        return {
            "sqlite_version": sqlite3.sqlite_version,
            "paths": {
                "project_root": str(self.paths.project_root),
                "state_db": str(self.paths.state_db),
                "observability_db": str(self.paths.observability_db),
                "health_file": str(self.paths.health_file),
            },
            "health": health.to_dict(),
            "state_pragmas": state_pragmas,
            "observability_pragmas": observability_pragmas,
        }

    def close(self) -> None:
        if self._closed:
            return
        errors: list[BaseException] = []
        # Shutdown is a natural low-frequency boundary.  A short-lived process
        # records nothing; a long-lived daemon contributes at most one cadence
        # sample and one bounded retention batch before the passive checkpoint.
        try:
            if not self._integrity_reasons:
                self.record_storage_metric()
                self.maintain_observability()
                self.maintain_cas()
        except BaseException:
            try:
                self._publish_degraded("observability_maintenance_failed")
            except BaseException:
                pass
        with self._state_lock, self._observability_lock:
            for name, connection in (("state", self._state), ("observability", self._observability)):
                try:
                    if name not in self._integrity_reasons:
                        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except BaseException as exc:
                    errors.append(exc)
                try:
                    connection.fault_callback = None
                    connection.close()
                except BaseException as exc:
                    errors.append(exc)
        # One final out-of-band snapshot records post-checkpoint file-family
        # sizes.  This is a single shutdown write, not a per-poll metric log.
        try:
            with self._health_lock:
                health = self._health_from_measurement()
                self._last_health = health
                self._write_health_snapshot(health)
        except BaseException as exc:
            errors.append(exc)
        finally:
            # Connections have been closed even if the out-of-band diagnostic
            # snapshot failed.  A second close must never operate on dead
            # handles or repeat a checkpoint.
            self._closed = True
            self._maintenance_lease.close()
        if errors:
            raise errors[0]

    def __enter__(self) -> "StorageRuntime":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()
