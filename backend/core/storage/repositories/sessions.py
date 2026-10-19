"""Sessions repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
import uuid
from ..transactions import authoritative_transaction
from .ports import SessionsPort
from ..support import _utc_now, state_read_snapshot

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
        with authoritative_transaction(self._state):
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
            from ..runtime_events import insert_fact
            identity = self._state.execute("SELECT session_id,task_id FROM session_processes WHERE process_id=?", (process_id,)).fetchone()
            insert_fact(self._state, event_id=f"ledger:{process_id}:{resolved_sequence}",
                kind=event_type, project_id=self.paths.project_id,
                session_id=str(identity["session_id"] or "") if identity else "",
                task_id=str(identity["task_id"] or "") if identity else "", process_id=process_id,
                stream_id=f"ledger:{process_id}", sequence=resolved_sequence,
                payload_ref=descriptor["ref"], occurred_at=occurred_at or _utc_now())
    return resolved_sequence


def load_agent_session_state(self: SessionsPort, process_id: str) -> dict | None:
    self._ensure_open()
    with state_read_snapshot(self._state, self._state_lock):
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
