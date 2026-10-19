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


def record_lesson_retrieval(self: KnowledgePort, lesson_id: str, retrieved_at: str) -> bool:
    """Merge statistics against the latest record in one cross-writer transaction."""
    self._ensure_open()
    with self._state_lock:
        self._state.execute("BEGIN IMMEDIATE")
        try:
            row = self._state.execute(
                "SELECT record_ref FROM lessons WHERE lesson_id=?", (lesson_id,),
            ).fetchone()
            if row is None:
                self._state.execute("COMMIT")
                return False
            record = dict(self._read_json_blob(str(row["record_ref"])))
            record["trigger_count"] = int(record.get("trigger_count") or 0) + 1
            record["recent_retrievals"] = [
                *(record.get("recent_retrievals") or []), retrieved_at,
            ][-10:]
            self._reserve_authoritative_write(len(self._encoded_object(record)) + 512)
            descriptor = self._prepare_state_json(
                record, media_type="application/vnd.gitgo.lesson+json",
            )
            self._register_object(self._state, descriptor)
            self._state.execute(
                "UPDATE lessons SET record_ref=?, updated_at=? WHERE lesson_id=?",
                (descriptor["ref"], _utc_now(), lesson_id),
            )
            self._state.execute("COMMIT")
            return True
        except BaseException:
            self._state.execute("ROLLBACK")
            raise


def record_lesson_verification(self: KnowledgePort, lesson_id: str,
                               project_id: str, project_name: str, *, user_confirmation: bool = False,
                               expected_version: str = "") -> dict:
    """Record one confirmation per actual project identity, not per click.

    Confirmations are attestations, not inferred passing tests. Legacy counters
    alone do not establish independently attributable confirmations.
    """
    self._ensure_open()
    with self._state_lock:
        self._state.execute("BEGIN IMMEDIATE")
        try:
            row = self._state.execute(
                "SELECT scope, record_ref FROM lessons WHERE lesson_id=?", (lesson_id,),
            ).fetchone()
            if row is None:
                raise ValueError("lesson not found")
            record = dict(self._read_json_blob(str(row["record_ref"])))
            from ...knowledge.models import Lesson
            from ...knowledge.review import can_consume, reviewed_record, content_version
            if expected_version and expected_version != content_version(record):
                raise ValueError("LESSON_CONTENT_CHANGED")
            if user_confirmation:
                if not can_consume(Lesson.from_dict(record)):
                    # Reviewing old knowledge does not register an unknown checker
                    # or grant workflow/tool authority imported from legacy text.
                    record.update(check=None, dangerous_tools=[], prerequisite_tools=[], required_tools=[])
                record = reviewed_record(record, kind="explicit_verification",
                                         reference=project_id)
            elif not can_consume(Lesson.from_dict(record)):
                raise ValueError("LESSON_REVIEW_REQUIRED")
            confirmations = [item for item in record.get("verifications", [])
                             if isinstance(item, dict) and item.get("project_id")]
            if not any(item["project_id"] == project_id for item in confirmations):
                confirmations.append({"project_id": project_id, "project_name": project_name,
                                      "kind": "confirmation", "confirmed_at": _utc_now()})
            record.update(verifications=confirmations, verified_at=_utc_now(),
                          verified_count=len({item["project_id"] for item in confirmations}),
                          verified_in=list(dict.fromkeys(item.get("project_name", "") for item in confirmations)))
            self._reserve_authoritative_write(len(self._encoded_object(record)) + 512)
            descriptor = self._prepare_state_json(record, media_type="application/vnd.gitgo.lesson+json")
            self._register_object(self._state, descriptor)
            self._state.execute(
                "UPDATE lessons SET scope=?, record_ref=?, updated_at=? WHERE lesson_id=?",
                ("instance" if row["scope"] == "pending" else row["scope"],
                 descriptor["ref"], _utc_now(), lesson_id),
            )
            self._state.execute("COMMIT")
            return record
        except BaseException:
            self._state.execute("ROLLBACK")
            raise


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


def save_harvest_signal_records(self: KnowledgePort, records: list[dict], *, create_only: bool = False) -> int:
    """Upsert changed harvest signals into the shared governance bus.

    Callers may pass a complete logical store for compatibility.  Content
    addressing filters unchanged rows before reserving a transaction, so a
    lease/update never rewrites every historical signal.
    """
    self._ensure_open()
    prepared: list[tuple[dict, dict, object]] = []
    for record in records:
        original = dict(record)
        expected = original.pop("_expected_payload_ref", None)
        safe = redact_for_persistence(original)
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
        if existing is not None and (create_only or str(existing["payload_ref"]) == str(descriptor["ref"])):
            continue
        prepared.append((safe, descriptor, expected))
    if not prepared:
        return 0
    logical = sum(int(item[1]["byte_length"]) + 384 for item in prepared)
    self._reserve_authoritative_write(logical)
    now = _utc_now()
    changed = 0
    conflict_clause = "DO NOTHING" if create_only else """DO UPDATE SET
        signal_type=excluded.signal_type, severity=excluded.severity,
        payload_ref=excluded.payload_ref, state=excluded.state,
        lease_owner=excluded.lease_owner, retry_count=excluded.retry_count,
        updated_at=excluded.updated_at"""
    with self._state_lock:
        self._state.execute("BEGIN IMMEDIATE")
        try:
            for safe, descriptor, expected in prepared:
                if expected is not None:
                    current = self._state.execute(
                        "SELECT payload_ref FROM governance_signals WHERE signal_id=?",
                        (str(safe["signal_id"]),)).fetchone()
                    if current is None or str(current["payload_ref"]) != expected:
                        raise ValueError("HARVEST_SIGNAL_CHANGED")
                self._register_object(self._state, descriptor)
                cursor = self._state.execute(
                    """INSERT INTO governance_signals(
                           signal_id, project_id, task_id, signal_type,
                           severity, payload_ref, state, lease_owner,
                           retry_count, created_at, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(signal_id) """ + conflict_clause,
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
                changed += cursor.rowcount
            self._state.execute("COMMIT")
        except BaseException:
            self._state.execute("ROLLBACK")
            raise
    return changed


def load_harvest_signal_records(self, *, with_version: bool = False) -> list[dict]:
    self._ensure_open()
    with self._state_lock:
        rows = self._state.execute(
            """SELECT payload_ref FROM governance_signals
               WHERE project_id=? AND signal_id LIKE 'hs_%'
               ORDER BY created_at, signal_id""",
            (self.paths.project_id,),
        ).fetchall()
    result = []
    for row in rows:
        record = dict(self._read_json_blob(str(row["payload_ref"])))
        if with_version:
            record["_expected_payload_ref"] = str(row["payload_ref"])
        result.append(record)
    return result


def harvest_signal_count(self) -> int:
    with self._state_lock:
        return int(self._state.execute(
            """SELECT COUNT(*) FROM governance_signals
               WHERE project_id=? AND signal_id LIKE 'hs_%'""",
            (self.paths.project_id,),
        ).fetchone()[0])

def resolve_harvest_proposal_records(self: KnowledgePort, project: str, proposal_id: str, *,
                                     accepted: bool, candidates: list[dict] | None,
                                     expected_version: str) -> dict:
    """One authoritative transaction for content approval and source resolution."""
    from ...knowledge.review import proposal_version, reviewed_record
    import json
    self._ensure_open()
    with self._state_lock:
        self._state.execute("BEGIN IMMEDIATE")
        try:
            rows = self._state.execute("""SELECT signal_id,payload_ref FROM governance_signals
                WHERE project_id=? AND signal_id LIKE 'hs_%' AND state='awaiting_confirmation'""",
                (self.paths.project_id,)).fetchall()
            source_refs = {str(row["signal_id"]): str(row["payload_ref"]) for row in rows}
            records = [dict(self._read_json_blob(str(row["payload_ref"]))) for row in rows]
            records = [row for row in records if row.get("project_name") == project
                       and row.get("proposal_id") == proposal_id]
            if not records:
                raise ValueError("HARVEST_PROPOSAL_NOT_FOUND")
            original = list(records[0].get("proposal_lessons") or [])
            if any(row.get("proposal_lessons") != original for row in records):
                raise ValueError("HARVEST_PROPOSAL_INCONSISTENT")
            source_ids = [str(row["signal_id"]) for row in records]
            version = proposal_version(project, proposal_id, source_ids, original)
            if version != expected_version:
                raise ValueError("HARVEST_PROPOSAL_CHANGED")
            chosen = list(candidates if candidates is not None else original) if accepted else []
            saved_ids = []
            for raw in chosen:
                safe = redact_for_persistence(dict(raw))
                # Nothing supplied by the model can carry authority across review.
                safe.update(source="auto_harvested", origin="harvest", abstract=False,
                            project_name=project, check=None, verified_count=0,
                            verified_in=[], verifications=[], verified_at="")
                identity = hashlib.sha256(json.dumps({key: safe.get(key) for key in
                    ("project_name", "trigger", "rule", "qualification", "tech_stack", "expires_at")},
                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                safe["id"] = "lesson_" + identity[:24]
                safe["created_at"] = safe.get("created_at") or _utc_now()
                safe = reviewed_record(safe, kind="user_proposal", reference=proposal_id + ":" + version)
                self._reserve_authoritative_write(len(self._encoded_object(safe)) + 512)
                desc = self._prepare_state_json(safe, media_type="application/vnd.gitgo.lesson+json")
                self._register_object(self._state, desc)
                self._state.execute("""INSERT INTO lessons
                    (lesson_id,scope,project_name,tech_stack,category,severity,content_hash,record_ref,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(lesson_id) DO UPDATE SET
                    record_ref=excluded.record_ref,updated_at=excluded.updated_at""",
                    (safe["id"], "pending", project, str(safe.get("tech_stack") or ""),
                     str(safe.get("category") or ""), str(safe.get("severity") or "medium"),
                     identity, desc["ref"], safe["created_at"], _utc_now()))
                saved_ids.append(safe["id"])
            actually_accepted = accepted and bool(saved_ids)
            for raw in records:
                safe = dict(raw)
                safe.update(state="processed" if actually_accepted else "dismissed",
                    proposal_lessons=[], lesson_ids=list(dict.fromkeys(saved_ids)),
                    resolved_proposal_version=version,
                    resolved_source_ref=source_refs[str(raw["signal_id"])], updated_at=_utc_now())
                self._reserve_authoritative_write(len(self._encoded_object(safe)) + 384)
                desc = self._prepare_state_json(safe, media_type="application/vnd.gitgo.harvest-signal+json")
                self._register_object(self._state, desc)
                self._state.execute("""UPDATE governance_signals SET payload_ref=?,state=?,
                    lease_owner=NULL,updated_at=? WHERE signal_id=? AND project_id=?""",
                    (desc["ref"], safe["state"], _utc_now(), safe["signal_id"], self.paths.project_id))
            self._state.execute("COMMIT")
            return {"proposal_id": proposal_id, "proposal_version": version,
                    "status": "accepted" if actually_accepted else "dismissed",
                    "signal_ids": source_ids, "lesson_ids": list(dict.fromkeys(saved_ids))}
        except BaseException:
            self._state.execute("ROLLBACK")
            raise


def configure_lesson_checker(self, change: dict, *, grant_id: str) -> dict:
    """CAS the approved declaration and its review in the lesson transaction."""
    if not grant_id:raise ValueError("CHECKER_APPROVAL_REQUIRED")
    from ...knowledge.review import content_version,reviewed_record,can_consume
    from ...knowledge.models import Lesson
    self._ensure_open()
    with self._state_lock:
        self._state.execute("BEGIN IMMEDIATE")
        try:
            row=self._state.execute("SELECT record_ref FROM lessons WHERE lesson_id=?",(change["lesson_id"],)).fetchone()
            if row is None:raise ValueError("LESSON_NOT_FOUND")
            record=dict(self._read_json_blob(str(row["record_ref"])))
            change_digest=hashlib.sha256(self._encoded_object(change)).hexdigest()
            previous=[e for e in record.get("resolution_history",[]) if isinstance(e,dict) and e.get("kind")=="checker_configuration" and e.get("grant_id")==grant_id]
            if previous:
                if previous[-1].get("change_digest")!=change_digest:raise ValueError("CHECKER_DECISION_CHANGED")
                self._state.execute("COMMIT")
                return {"lesson":record,"applied":False,"status":"already_applied"}
            if content_version(record)!=change["expected_version"]:raise ValueError("LESSON_CONTENT_CHANGED")
            if not can_consume(Lesson.from_dict(record)):raise ValueError("LESSON_REVIEW_REQUIRED")
            if change["project_id"]!=self.paths.project_id:raise ValueError("CHECKER_PROJECT_CHANGED")
            check=change.get("check")
            if check is not None:
                from ...knowledge.applicability import assess_lesson
                if assess_lesson(Lesson.from_dict(record),self.paths.workspace).get("state")!="current":raise ValueError("LESSON_EVIDENCE_RECHECK_REQUIRED")
                check={**check,"activation":{"kind":"user_permission","project_id":self.paths.project_id,"grant_id":grant_id}}
            record.update(check=check,dangerous_tools=["edit_file","write_file"] if check else [],
                          checker_revision=int(record.get("checker_revision") or 0)+1,
                          resolution_history=[*(record.get("resolution_history") or []),{
                              "kind":"checker_configuration","grant_id":grant_id,"action":change["action"],"change_digest":change_digest,
                              "previous_version":change["expected_version"],"at":_utc_now()}])
            record=reviewed_record(record,kind="explicit_verification",reference=grant_id)
            descriptor=self._prepare_state_json(record,media_type="application/vnd.gitgo.lesson+json")
            self._reserve_authoritative_write(len(self._encoded_object(record))+512)
            self._register_object(self._state,descriptor)
            self._state.execute("UPDATE lessons SET record_ref=?,updated_at=? WHERE lesson_id=?",(descriptor["ref"],_utc_now(),change["lesson_id"]))
            self._state.execute("COMMIT")
            return {"lesson":record,"applied":True,"status":"applied"}
        except BaseException:
            self._state.execute("ROLLBACK")
            raise
