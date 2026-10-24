"""Observability repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
import hashlib
import json
import time
import uuid
from typing import Iterable
from ..transactions import authoritative_transaction
from ..models import StorageBlocked
from ..redaction import redact_for_persistence
from .ports import ObservabilityPort
from ..support import _MinuteBudget, _utc_now

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
    self._observability.require_available()
    encoded = summary.encode("utf-8")
    logical = len(encoded) + len(payload_ref or "") + 256
    if logical > self.policy.max_observability_record_bytes:
        raise ValueError("observability event is too large; store the body in CAS")
    if not self._reserve_observability_write(logical):
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
    self._observability.require_available()
    health = self.check_health()
    estimated_write = self._estimated_sqlite_write(logical)
    allowed = not (
        health.observability_bytes + estimated_write
        > self.policy.observability_max_bytes
        or health.total_bytes + estimated_write > self.policy.total_max_bytes
        or not _MinuteBudget.reserve_all(
            (self._global_sqlite_budget, estimated_write),
            (self._observability_budget, logical),
        )
    )
    if not allowed and publish_degraded:
        self._publish_degraded("observability_write_throttled")
    return allowed


def trace_last_sequence(self: ObservabilityPort, trace_id: str) -> int:
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
        with authoritative_transaction(self._observability):
            row = self._observability.execute(
                "SELECT MAX(sequence) FROM trace_events WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
            sequence = max(proposed, int(row[0] or 0) + 1)
            safe_record["seq"] = sequence
            from backend.core.messaging import observation_identity
            safe_record["signal"] = observation_identity(safe_record, trace_id, sequence)
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


def read_trace_summary(self: ObservabilityPort, trace_id: str, *, process_id: str = "") -> dict:
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
            with authoritative_transaction(self._observability):
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
    return inserted


def list_traces(self: ObservabilityPort, *, limit: int = 100) -> list[dict]:
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


def read_trace_detail(self: ObservabilityPort, ref: str) -> object:
    value = str(ref)
    if value.startswith("trace-object:"):
        value = "sha256:" + value.removeprefix("trace-object:")
    return self._read_json_blob(value)


def record_storage_metric(self: ObservabilityPort, *, force: bool = False) -> bool:
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
