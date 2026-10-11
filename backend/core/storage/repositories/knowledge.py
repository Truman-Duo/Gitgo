"""Knowledge repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
import hashlib
import sqlite3
import uuid
from ..redaction import redact_for_persistence
from .ports import KnowledgePort
from ..support import _utc_now

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


def replace_history_records(self: KnowledgePort, records: list[dict]) -> None:
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


def load_history_records(self: KnowledgePort, *, limit: int | None = None) -> list[dict]:
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


def get_lesson_record(self: KnowledgePort, lesson_id: str) -> tuple[str, dict] | None:
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


def lesson_count(self: KnowledgePort, *, scope: str, project_name: str = "") -> int:
    with self._state_lock:
        return int(self._state.execute(
            "SELECT COUNT(*) FROM lessons WHERE scope=? AND project_name=?",
            (scope, project_name),
        ).fetchone()[0])


def save_harvest_signal_records(self: KnowledgePort, records: list[dict]) -> int:
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
