"""Append-only execution traces for one native Agent task tree.

The trace is an observer: it never participates in completion, governance, or
tool authorization. Credentials are redacted before persistence. Large request
snapshots are content-addressed and adjacent stream fragments are coalesced, so
repeated prompts and token streaming do not turn a long loop into an O(n^2) log.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.storage import StorageRuntime, get_storage
from backend.core.storage.redaction import redact_for_persistence


TRACE_SCHEMA_VERSION = 1
_LEGACY_MIGRATION_LOCKS: dict[str, threading.RLock] = {}
_LEGACY_MIGRATION_LOCKS_GUARD = threading.Lock()


def _migration_lock(workspace: Path) -> threading.RLock:
    key = str(workspace.resolve())
    with _LEGACY_MIGRATION_LOCKS_GUARD:
        return _LEGACY_MIGRATION_LOCKS.setdefault(key, threading.RLock())


def _redact(value: Any, *, key: str = "") -> Any:
    """Backward-compatible local name for the shared persistence boundary."""
    return redact_for_persistence(value, key=key)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")


class TraceJournal:
    """Thread-safe task-tree journal backed by bounded observability SQLite."""

    def __init__(
        self,
        workspace: str | Path,
        trace_id: str,
        *,
        storage: StorageRuntime | None = None,
    ):
        raw_trace_id = str(trace_id)
        self.trace_id = re.sub(r"[^A-Za-z0-9._-]", "_", raw_trace_id).strip(".")
        if not self.trace_id:
            raise ValueError("trace_id must contain at least one safe character")
        if self.trace_id != raw_trace_id:
            self.trace_id += "-" + hashlib.sha256(
                raw_trace_id.encode("utf-8")
            ).hexdigest()[:12]
        self._workspace = Path(workspace).resolve()
        self._storage = storage or get_storage(self._workspace)
        # Fallback runtimes belong to the bounded process pool.  Closing one
        # after every Dashboard trace poll would checkpoint WAL and rewrite the
        # health snapshot repeatedly.
        self._owns_storage = False
        self._lock = threading.RLock()
        self._migrate_legacy_once()
        self._sequence = self._storage.trace_last_sequence(self.trace_id)
        self._provider_request_snapshots: dict[str, dict] = {}

    @property
    def path(self) -> Path:
        """Compatibility diagnostic path; Trace no longer lives in the repo."""
        return self._storage.paths.observability_db

    def _migrate_legacy_once(self) -> None:
        """Import repository-local JSONL exactly once, then archive the tree."""
        root = self._workspace / ".gitgo" / "traces"
        events_dir = root / "events"
        if not events_dir.exists():
            return
        with _migration_lock(self._workspace):
            if not events_dir.exists():
                return
            try:
                for event_path in sorted(events_dir.glob("*.jsonl")):
                    trace_id = event_path.stem
                    coalescer = DeltaCoalescer()
                    detail_by_sequence: dict[int, object] = {}
                    pending_records: list[dict] = []
                    # Stream the source instead of materializing the largest
                    # JSONL file twice.  All sequences are offered on every
                    # pass: StorageRuntime filters exact committed keys before
                    # reserving a byte, which also repairs sparse partial
                    # imports rather than trusting MAX(sequence).
                    with event_path.open(encoding="utf-8") as event_stream:
                        for line in event_stream:
                            if not line.strip():
                                continue
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                # Preserve malformed tails in the archived
                                # source; valid preceding events remain usable.
                                continue
                            detail = None
                            legacy_ref = str(record.get("detail_ref") or "")
                            if legacy_ref.startswith("trace-object:"):
                                digest = legacy_ref.removeprefix("trace-object:")
                                detail_path = root / "objects" / f"{digest}.json"
                                if detail_path.exists():
                                    detail = json.loads(
                                        detail_path.read_text(encoding="utf-8")
                                    )
                                record.pop("detail_ref", None)
                                record.pop("detail_bytes", None)
                            sequence = int(record.get("seq", 0) or 0)
                            if detail is not None:
                                detail_by_sequence[sequence] = detail
                            pending_records.extend(coalescer.push(record))
                    pending_records.extend(coalescer.flush())
                    batch = [
                        (
                            record,
                            detail_by_sequence.get(int(record.get("seq", 0) or 0)),
                        )
                        for record in sorted(
                            pending_records,
                            key=lambda item: int(item.get("seq", 0) or 0),
                        )
                    ]
                    imported = self._storage.import_trace_records(trace_id, batch)
                    if imported is None:
                        # Optional storage pressure is not data loss: keep the
                        # complete legacy tree for a future bounded pass.
                        return
                suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                archived = root.with_name(f"traces.legacy-imported-{suffix}")
                counter = 1
                while archived.exists():
                    archived = root.with_name(
                        f"traces.legacy-imported-{suffix}-{counter}"
                    )
                    counter += 1
                root.replace(archived)
            except (OSError, ValueError, json.JSONDecodeError):
                # Trace is observational.  A failed legacy import must neither
                # block the Agent nor create a competing new JSONL writer.
                return

    def append(self, event: dict, *, detail: Any = None) -> dict:
        """Persist one semantic event and return its redacted live projection."""
        with self._lock:
            safe = _redact(dict(event))
            safe.pop("_trace_detail", None)
            now = datetime.now(timezone.utc).isoformat()
            monotonic_ns = time.monotonic_ns()
            # Argument fragments are needed by the live tool card, but the
            # completed call already stores the exact arguments once in CAS.
            # Writing both forms duplicates bytes and can consume the minute
            # transaction budget before the authoritative tool result/diff.
            if safe.get("event") == "toolcall_delta":
                return {
                    "schema_version": TRACE_SCHEMA_VERSION,
                    "trace_id": self.trace_id,
                    "time": now,
                    "monotonic_ns": monotonic_ns,
                    **safe,
                    "trace_persisted": False,
                    "consolidated_into": "toolcall_done",
                }

            self._sequence += 1
            record = {
                "schema_version": TRACE_SCHEMA_VERSION,
                "trace_id": self.trace_id,
                "seq": self._sequence,
                "time": now,
                "monotonic_ns": monotonic_ns,
                **safe,
            }
            persisted_detail = _redact(detail) if detail is not None else None
            snapshot = None
            if safe.get("event") == "provider_request_started":
                persisted_detail, snapshot = self._provider_request_delta(
                    str(safe.get("process_id") or "host"), persisted_detail,
                )
            stored = self._storage.append_trace_record(
                self.trace_id, record, detail=persisted_detail,
            )
            if stored is None:
                record["trace_persisted"] = False
                return record
            self._sequence = int(stored["seq"])
            if snapshot is not None and stored.get("detail_ref"):
                snapshot["detail_ref"] = str(stored["detail_ref"])
                self._provider_request_snapshots[
                    str(safe.get("process_id") or "host")
                ] = snapshot
            return stored

    def _provider_request_delta(
        self, process_id: str, detail: Any,
    ) -> tuple[Any, dict | None]:
        """Store a request as one full snapshot followed by prefix deltas.

        Provider-visible histories are append-mostly. Persisting the complete
        message list and unchanged tool schema on every step is quadratic and
        defeats the bounded observability design. A delta always references a
        successfully persisted predecessor; after throttling or compaction the
        next record safely falls back to a full snapshot.
        """
        if (
            not isinstance(detail, dict)
            or "messages" not in detail
            or "tools" not in detail
        ):
            return detail, None
        messages = list(detail.get("messages") or [])
        tools = list(detail.get("tools") or [])
        message_hashes = [hashlib.sha256(_canonical_json(item)).hexdigest() for item in messages]
        tools_hash = hashlib.sha256(_canonical_json(tools)).hexdigest()
        snapshot = {
            "message_hashes": message_hashes,
            "tools_hash": tools_hash,
            "detail_ref": "",
        }
        previous = self._provider_request_snapshots.get(process_id)
        if not previous or not previous.get("detail_ref"):
            return {**detail, "snapshot_mode": "full"}, snapshot

        previous_hashes = list(previous.get("message_hashes") or [])
        common = 0
        for left, right in zip(previous_hashes, message_hashes):
            if left != right:
                break
            common += 1
        tools_reused = previous.get("tools_hash") == tools_hash
        delta = {
            "representation": "canonical_provider_delta",
            "snapshot_mode": "prefix_delta",
            "base_detail_ref": previous["detail_ref"],
            "common_prefix_messages": common,
            "removed_messages": max(0, len(previous_hashes) - common),
            "appended_messages": messages[common:],
            "tools_reused": tools_reused,
            "tools": [] if tools_reused else tools,
        }
        for key, value in detail.items():
            if key not in {"messages", "tools", "representation"}:
                delta[key] = value
        return delta, snapshot

    def read(self, *, after_seq: int = 0, limit: int = 500,
             process_id: str = "", include_deltas: bool = True) -> dict:
        return self._storage.read_trace_events(
            self.trace_id, after_seq=after_seq, limit=limit,
            process_id=process_id, include_deltas=include_deltas,
        )

    def read_detail(self, ref: str) -> Any:
        return self._storage.read_trace_detail(ref)

    def close(self) -> None:
        if self._owns_storage:
            self._storage.close()
            self._owns_storage = False

    def __enter__(self) -> "TraceJournal":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class DeltaCoalescer:
    """Coalesce tiny provider fragments without losing their textual content.

    Providers commonly yield one to three characters per SSE event. Emitting
    each fragment through the daemon control channel can starve command and
    completion messages. Batches remain scoped to one process/event/tool call;
    any semantic event for that process flushes its pending text first.
    """

    DELTA_EVENTS = frozenset({"text_delta", "reasoning_delta", "toolcall_delta"})

    def __init__(self, *, min_chars: int = 1024):
        # The observability store intentionally caps transactions per minute
        # to protect SSDs. Persisting provider-sized fragments in ~96-character
        # rows let verbose reasoning consume the allowance before tool receipts,
        # diffs, and the terminal barrier. A 1 KiB batch still flushes at every
        # semantic boundary while keeping ordinary turns below the write cap.
        self.min_chars = max(1, int(min_chars))
        self._pending: dict[tuple[str, str, str, str], dict] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(event: dict) -> tuple[str, str, str, str]:
        return (
            str(event.get("process_id", "")),
            str(event.get("agent_task_id", event.get("task_id", ""))),
            str(event.get("event", "")),
            str(event.get("tool_call_id", "")),
        )

    def _pop(self, key: tuple[str, str, str, str]) -> dict | None:
        item = self._pending.pop(key, None)
        if item is None:
            return None
        result = dict(item["event"])
        result["delta"] = "".join(item["fragments"])
        result["coalesced_chunks"] = item["count"]
        return result

    def _flush_process(self, process_id: str) -> list[dict]:
        keys = [key for key in self._pending if key[0] == process_id]
        return [item for key in keys if (item := self._pop(key)) is not None]

    def push(self, event: dict) -> list[dict]:
        with self._lock:
            event = dict(event)
            kind = str(event.get("event", ""))
            if kind not in self.DELTA_EVENTS:
                return self._flush_process(str(event.get("process_id", ""))) + [event]

            key = self._key(event)
            item = self._pending.get(key)
            if item is None:
                item = {"event": event, "fragments": [], "count": 0, "chars": 0}
                self._pending[key] = item
            fragment = str(event.get("delta", ""))
            item["fragments"].append(fragment)
            item["count"] += 1
            item["chars"] += len(fragment)
            if item["chars"] >= self.min_chars:
                merged = self._pop(key)
                return [merged] if merged is not None else []
            return []

    def flush(self) -> list[dict]:
        with self._lock:
            return [
                item for key in list(self._pending)
                if (item := self._pop(key)) is not None
            ]


def read_trace(
    workspace: str | Path,
    trace_id: str,
    *,
    after_seq: int = 0,
    limit: int = 500,
    storage: StorageRuntime | None = None,
    process_id: str = "", include_deltas: bool = True,
) -> dict:
    journal = TraceJournal(workspace, trace_id, storage=storage)
    try:
        return journal.read(after_seq=after_seq, limit=limit,
                            process_id=process_id, include_deltas=include_deltas)
    finally:
        journal.close()


def read_trace_detail(
    workspace: str | Path, ref: str, *, storage: StorageRuntime | None = None,
) -> Any:
    journal = TraceJournal(workspace, "_reader", storage=storage)
    try:
        return journal.read_detail(ref)
    finally:
        journal.close()


def list_traces(
    workspace: str | Path, *, limit: int = 100,
    storage: StorageRuntime | None = None,
) -> list[dict]:
    journal = TraceJournal(workspace, "_reader", storage=storage)
    try:
        return journal._storage.list_traces(limit=limit)
    finally:
        journal.close()
