"""Recovery repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
from .ports import RecoveryPort
from ..support import state_read_snapshot

def load_agent_process_state(self: RecoveryPort, process_id: str) -> dict | None:
    """Return task/runtime state alongside the restored session snapshot."""
    with state_read_snapshot(self._state, self._state_lock):
        session_state = self.load_agent_session_state(process_id)
        row = self._state.execute(
            """SELECT process_id, session_id, task_id, parent_process_id,
                      role, actor_kind, capability_profile_id, status,
                      steps_used, max_steps, runtime_state_ref, created_at,
                      updated_at, state_revision
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
    with state_read_snapshot(self._state, self._state_lock):
        row = self._state.execute(
            """SELECT process_id FROM session_processes
               WHERE parent_process_id IS NULL
               ORDER BY updated_at DESC, created_at DESC LIMIT 1"""
        ).fetchone()
        return self.load_agent_process_state(str(row["process_id"])) if row else None
