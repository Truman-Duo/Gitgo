"""Lineage repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
import uuid
from .ports import LineagePort
from ..support import _utc_now

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


def preview_session_undo(self: LineagePort, session_id: str) -> dict:
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


def commit_session_undo(self: LineagePort, session_id: str, checkpoint_id: str) -> dict:
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
