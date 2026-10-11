"""Shared storage encodings and connection guards; no business repositories."""
from __future__ import annotations
import base64
import json
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from .models import StorageCorruptionDetected, StorageRuntimeUnsupported

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
