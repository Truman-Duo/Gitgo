"""Bounded asynchronous capture, independent of optional canonical Trace writes.

This archive is evidence for an observer, never a task/session/state authority.
Only the writer touches SQLite, compression and disk on the production path.
Queue/disk failures are explicit coverage gaps; they never replay runtime work.
"""
from __future__ import annotations

import atexit
import base64
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
import uuid
import zlib

from backend.core.storage.redaction import redact_for_persistence
from backend.core.storage.runtime import ObserverSqliteError, open_observer_capture
from .files import atomic_json, private_directory


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def retained_bytes(value: Any, limit: int) -> int:
    """Conservative allocation estimate; do not encode megabyte strings on the producer."""
    total, pending = 64, [value]
    while pending and total <= limit:
        item = pending.pop()
        total += 64
        if isinstance(item, str):
            total += len(item) * 4
        elif isinstance(item, dict):
            total += len(item) * 96
            if total > limit:
                break
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            total += len(item) * 8
            if total > limit:
                break
            pending.extend(item)
    return total


class Recorder:
    def __init__(self, directory: Path, *, source: str = "host", queue_bytes: int = 128 << 20,
                 disk_bytes: int = 4 << 30):
        self.directory = private_directory(directory)
        self.source_id = f"{source}-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.path = directory / f"{self.source_id}.sqlite3"
        self.queue_limit, self.disk_limit = queue_bytes, disk_bytes
        self._condition = threading.Condition()
        self._queue = deque()
        self._queued_bytes = 0
        self._closing = False
        self._seq = self._written = self._dropped = 0
        self._error = ""
        self._error_code = ""
        self._ready = threading.Event()
        self._last_health = 0.0
        self._thread = threading.Thread(target=self._run, name="observer-writer", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("OBSERVER_WRITER_START_TIMEOUT")
        if self._error:
            raise RuntimeError("OBSERVER_WRITER_START_FAILED: " + self._error)

    def submit(self, kind: str, payload: Any, *, json_line: bool = False) -> bool:
        """Transfer an owned immutable snapshot. Callers must not mutate it afterwards."""
        size = retained_bytes(payload, self.queue_limit)
        with self._condition:
            self._seq += 1
            seq = self._seq
            if self._closing or self._error or size > self.queue_limit - self._queued_bytes:
                self._dropped += 1
                return False
            self._queue.append((seq, time.time_ns(), time.monotonic_ns(), kind, payload, json_line, size))
            self._queued_bytes += size
            self._condition.notify()
        return True

    def health(self) -> dict:
        with self._condition:
            return {"source_id": self.source_id, "pid": os.getpid(), "seen": self._seq,
                    "written": self._written, "dropped": self._dropped,
                    "queued_bytes": self._queued_bytes, "queue_limit_bytes": self.queue_limit,
                    "error": self._error, "closed": self._closing and not self._thread.is_alive(),
                    "error_code": self._error_code or ("OBSERVER_QUEUE_OVERFLOW" if self._dropped else ""),
                    "coverage": "degraded" if self._error or self._dropped else "capturing",
                    "updated_unix_ns": time.time_ns()}

    def _save_health(self) -> None:
        atomic_json(self.directory / f"{self.source_id}.health.json", self.health())
        self._last_health = time.monotonic()

    @staticmethod
    def _blob(db, raw: bytes, *, codec: str = "raw") -> str:
        digest = hashlib.sha256(codec.encode() + b"\0" + raw).hexdigest()
        if not db.execute("SELECT 1 FROM objects WHERE hash=?", (digest,)).fetchone():
            db.execute("INSERT INTO objects VALUES (?,?,?,?)", (digest, codec, len(raw), zlib.compress(raw)))
        return digest

    def _encode(self, db, value, *, key=""):
        # Whole messages/schemas and long text are independent deduplicated objects.
        # Universal tagged nodes avoid collisions with arbitrary user/tool JSON.
        if key == "tools" or (isinstance(value, str) and len(value) >= 2048):
            return ["r", self._blob(db, json_bytes(value))]
        if isinstance(value, dict):
            return ["d", [[k, self._encode(db, v, key=k)] for k, v in value.items()]]
        if isinstance(value, list):
            return ["l", [["r", self._blob(db, json_bytes(v))] if key == "messages"
                          else self._encode(db, v) for v in value]]
        return ["v", value]

    def _write(self, db, item):
        seq, wall_ns, mono_ns, kind, payload, json_line, _ = item
        if json_line:
            payload = json.loads(payload)
        payload = redact_for_persistence(payload)
        encoded = json_bytes(self._encode(db, payload))
        ref = self._blob(db, encoded, codec="tree")
        fields = payload if isinstance(payload, dict) else {}
        event = fields.get("payload") if isinstance(fields.get("payload"), dict) else fields
        if isinstance(fields.get("event"), dict):
            event = fields["event"]
        summary = {k: event[k] for k in ("event", "type", "stage", "task_id", "process_id",
                   "trace_id", "code", "error_code", "status", "operation", "origin", "provider_id", "model_id") if k in event}
        for k in ("project", "request_id", "operation", "origin"):
            if k in fields:
                summary[k] = fields[k]
        if isinstance(fields.get("error"), dict):
            summary["error_code"] = fields["error"].get("code")
        summary = {k: v[:256] if isinstance(v, str) else v for k,v in summary.items()
                   if v is None or isinstance(v, (str, bool, int, float))}
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                   (seq, wall_ns, mono_ns, kind, json.dumps(summary, ensure_ascii=False), ref))

    def _run(self):
        db = None
        try:
            db = open_observer_capture(self.path)
            db.execute("PRAGMA journal_mode=WAL")
            # Observation is async and non-authoritative. NORMAL avoids fsync per
            # token; clean close checkpoints it. A crash always leaves coverage unknown.
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript("CREATE TABLE events(seq INTEGER PRIMARY KEY,wall_ns INTEGER,mono_ns INTEGER,"
                             "kind TEXT,summary TEXT,ref TEXT);"
                             "CREATE TABLE objects(hash TEXT PRIMARY KEY,codec TEXT,bytes INTEGER,data BLOB);")
            db.commit()
            self._save_health()
            self._ready.set()
            while True:
                with self._condition:
                    if not self._queue and not self._closing:
                        self._condition.wait(0.5)
                    batch = [self._queue.popleft() for _ in range(min(len(self._queue), 64))]
                    closing = self._closing
                if batch:
                    # Archive quota includes all sources and WALs; never auto-delete evidence.
                    used = sum(p.stat().st_size for p in self.directory.glob("*.sqlite3*"))
                    if used >= self.disk_limit:
                        raise OSError("OBSERVER_DISK_QUOTA_EXCEEDED")
                    for item in batch:
                        self._write(db, item)
                    db.commit()
                    with self._condition:
                        self._written += len(batch)
                        self._queued_bytes -= sum(item[-1] for item in batch)
                if time.monotonic() - self._last_health >= 1:
                    self._save_health()
                if closing and not batch:
                    break
        except Exception as exc:
            with self._condition:
                self._error = f"{type(exc).__name__}: {exc}"
                self._error_code = ("OBSERVER_DISK_QUOTA_EXCEEDED" if "OBSERVER_DISK_QUOTA_EXCEEDED" in str(exc)
                    else "OBSERVER_WRITE_FAILED" if isinstance(exc, OSError) else "OBSERVER_ENCODING_FAILED")
                self._dropped = max(self._dropped, self._seq - self._written)
                self._queue.clear()
                self._queued_bytes = 0
            self._ready.set()
        finally:
            if db is not None:
                try:
                    if not self._error and db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                        self._error, self._error_code = "A reader still holds the capture WAL", "OBSERVER_FLUSH_BUSY"
                except ObserverSqliteError as exc:
                    self._error, self._error_code = str(exc), "OBSERVER_FLUSH_FAILED"
                finally:
                    db.close()
            try:
                self._save_health()
            except Exception:
                pass

    def close(self, timeout=10):
        with self._condition:
            if self._closing and not self._thread.is_alive():
                return
            self._closing = True
            self._condition.notify()
        self._thread.join(timeout)
        if self._thread.is_alive():
            with self._condition:
                self._error = "OBSERVER_FLUSH_TIMEOUT"
                self._error_code = "OBSERVER_FLUSH_TIMEOUT"
        try:
            self._save_health()
        except OSError as exc:
            self._error, self._error_code = str(exc), "OBSERVER_HEALTH_WRITE_FAILED"


class Archive:
    def __init__(self, directory: Path):
        self.directory = directory
        self._body_lock = threading.Lock()
        self._cached_body = None

    def status(self):
        sources = []
        for path in sorted(self.directory.glob("*.health.json")):
            try:
                value = json.loads(path.read_text("utf-8"))
                value["stale"] = time.time_ns() - value["updated_unix_ns"] > 10_000_000_000
                sources.append(value)
            except (OSError, ValueError, KeyError):
                sources.append({"source_id": path.name, "coverage": "unknown", "error": "OBSERVER_HEALTH_UNREADABLE"})
        return {"sources": sources, "coverage": "degraded" if any(
            s.get("coverage") != "capturing" or (s.get("stale") and not s.get("closed")) for s in sources
        ) else "capturing"}

    def _connect(self, source: str):
        if not source or len(source) > 100 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in source):
            raise ValueError("OBSERVER_SOURCE_INVALID")
        path = self.directory / f"{source}.sqlite3"
        if not path.is_file():
            raise ValueError("OBSERVER_SOURCE_MISSING")
        return open_observer_capture(path, read_only=True)

    def events(self, source: str, after_seq=0, limit=200):
        with closing(self._connect(source)) as db:
            rows = db.execute("SELECT seq,wall_ns,mono_ns,kind,summary,ref FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                              (max(0, int(after_seq)), max(1, min(1000, int(limit))))).fetchall()
        events = [{"seq": s, "unix_ns": w, "monotonic_ns": m, "kind": k,
                   "summary": json.loads(summary), "body_ref": ref} for s, w, m, k, summary, ref in rows]
        return {"source_id": source, "events": events, "next_seq": events[-1]["seq"] if events else after_seq}

    @staticmethod
    def _raw(db, digest):
        row = db.execute("SELECT codec,bytes,data FROM objects WHERE hash=?", (digest,)).fetchone()
        if row is None:
            raise ValueError("OBSERVER_BODY_MISSING")
        codec, length, compressed = row
        if length > 128 << 20:
            raise ValueError("OBSERVER_BODY_TOO_LARGE")
        try:
            raw = zlib.decompress(compressed)
        except zlib.error as exc:
            raise ValueError("OBSERVER_BODY_CORRUPT") from exc
        if len(raw) != length or hashlib.sha256(codec.encode() + b"\0" + raw).hexdigest() != digest:
            raise ValueError("OBSERVER_BODY_CORRUPT")
        return codec, json.loads(raw)

    def _decode(self, db, node):
        kind, value = node
        if kind == "r":
            return self._raw(db, value)[1]
        if kind == "d":
            return {k: self._decode(db, v) for k, v in value}
        if kind == "l":
            return [self._decode(db, v) for v in value]
        if kind == "v":
            return value
        raise ValueError("OBSERVER_BODY_CORRUPT")

    def body(self, source: str, ref: str):
        if len(ref) != 64 or any(c not in "0123456789abcdef" for c in ref):
            raise ValueError("OBSERVER_REFERENCE_INVALID")
        with closing(self._connect(source)) as db:
            codec, node = self._raw(db, ref)
            return self._decode(db, node) if codec == "tree" else node

    def chunk(self, source: str, ref: str, offset=0, limit=262144):
        with self._body_lock:
            if self._cached_body is None or self._cached_body[:2] != (source, ref):
                raw = json_bytes(self.body(source, ref))
                self._cached_body = (source, ref, raw, hashlib.sha256(raw).hexdigest())
            _, _, raw, digest = self._cached_body
        offset, limit = max(0, int(offset)), max(1, min(262144, int(limit)))
        data = raw[offset:offset + limit]
        return {"source_id": source, "ref": ref, "offset": offset, "total_bytes": len(raw),
                "sha256": digest, "data_base64": base64.b64encode(data).decode(),
                "next_offset": offset + len(data), "complete": offset + len(data) >= len(raw)}


_recorder = None
_recorder_key = None
_failed_key = None
_lock = threading.Lock()


def process_recorder() -> Recorder | None:
    """Child processes inherit only an archive directory, never control credentials."""
    global _recorder, _recorder_key, _failed_key
    raw = os.environ.get("GITGO_OBSERVER_CAPTURE_DIR", "")
    if not raw:
        return None
    key = (os.getpid(), raw)
    with _lock:
        if _failed_key == key:
            return None
        if _recorder_key != key:
            if _recorder is not None and _recorder_key[0] == os.getpid():
                _recorder.close()
            try:
                _recorder = Recorder(Path(raw), source="runtime")
            except Exception:
                _failed_key = key
                raise
            _recorder_key = key
            atexit.register(_recorder.close)
        return _recorder


def close_process_recorder():
    global _recorder, _recorder_key
    with _lock:
        previous, _recorder, _recorder_key = _recorder, None, None
    if previous is not None:
        previous.close()


def capture(kind: str, payload: Any) -> None:
    # Failures are observational; no Agent action depends on capture success.
    try:
        recorder = process_recorder()
        if recorder is not None:
            recorder.submit(kind, payload)
    except Exception as exc:
        # Startup faults must be visible to the parent archive, not silently swallowed.
        import sys
        print(f"[gitgo-observer] OBSERVER_CAPTURE_UNAVAILABLE: {exc}", file=sys.stderr, flush=True)

