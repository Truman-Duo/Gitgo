"""Retirement repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
import json
import time
from .ports import RetirementPort
from ..support import _utc_now

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


def session_events_since_checkpoint(self: RetirementPort, process_id: str) -> int:
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


def delete_process_session(self: RetirementPort, process_id: str) -> None:
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
            from ..runtime_events import delete_process_facts
            delete_process_facts(self._state, process_id)
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


def is_process_deleted(self: RetirementPort, process_id: str) -> bool:
    with self._state_lock:
        return self._state.execute("SELECT 1 FROM deleted_processes WHERE process_id=?", (process_id,)).fetchone() is not None


def read_deleted_process_ids(self) -> set[str]:
    with self._state_lock:
        return {str(row[0]) for row in self._state.execute("SELECT process_id FROM deleted_processes").fetchall()}


def purge_b_session(self: RetirementPort, session_id: str, plan_id: str) -> None:
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
                from ..runtime_events import delete_process_facts
                delete_process_facts(self._state, pid)
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


def create_deletion_plan(self: RetirementPort, manifest: dict, *, not_before: float) -> dict:
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


def read_deletion_plan(self: RetirementPort, plan_id: str) -> dict:
    with self._state_lock:
        row = self._state.execute("SELECT * FROM deletion_plans WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None:
        raise ValueError("DELETION_PLAN_NOT_FOUND")
    return {**{k: row[k] for k in row.keys() if k != "manifest_json"}, "manifest": json.loads(row["manifest_json"])}


def list_deletion_plans(self: RetirementPort, *, limit: int = 100, due_only: bool = False) -> list[dict]:
    with self._state_lock:
        rows = self._state.execute("SELECT * FROM deletion_plans " +
            ("WHERE state='scheduled' AND not_before<=? " if due_only else "") +
            "ORDER BY CASE WHEN state IN ('scheduled','executing','blocked','recovery_required') THEN 0 ELSE 1 END, created_at DESC LIMIT ?",
            tuple([*([time.time()] if due_only else []), max(1, min(limit, 200))])).fetchall()
    return [{**{k: row[k] for k in row.keys() if k != "manifest_json"}, "manifest": json.loads(row["manifest_json"])} for row in rows]


def transition_deletion_plan(self: RetirementPort, plan_id: str, expected: str, state: str, *, error: str = "", not_before: float | None = None) -> dict:
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
