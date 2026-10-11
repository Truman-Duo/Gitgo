"""Durable fact projections; consumers never grant execution authority.

Each stream has its own sequence. Delivery is at least once, with independent
consumer cursors. State and CAS remain the source for security decisions.
"""
from __future__ import annotations


def insert_fact(connection, *, event_id, kind, project_id, session_id, task_id,
                process_id, stream_id, sequence, payload_ref, occurred_at,
                causation_id=""):
    connection.execute("""INSERT INTO runtime_outbox(
        event_id,schema_version,kind,project_id,session_id,task_id,process_id,stream_id,
        stream_sequence,producer,correlation_id,causation_id,payload_ref,occurred_at)
        VALUES(?,1,?,?,?,?,?,?,?,'StateRepository',?,?,?,?)""",
        (event_id, kind, project_id, session_id, task_id, process_id, stream_id,
         sequence, task_id or process_id, causation_id, payload_ref, occurred_at))


def read_facts(runtime, consumer_id, *, limit=100):
    if not consumer_id or len(consumer_id) > 128:
        raise ValueError("RUNTIME_CONSUMER_INVALID")
    runtime._ensure_open()
    with runtime._state_lock:
        rows = runtime._state.execute("""SELECT e.* FROM runtime_outbox e
            LEFT JOIN runtime_consumers c ON c.consumer_id=? AND c.stream_id=e.stream_id
            WHERE e.stream_sequence>COALESCE(c.last_sequence,0)
              AND NOT EXISTS(SELECT 1 FROM deleted_processes d WHERE d.process_id=e.process_id)
            ORDER BY e.stream_id,e.stream_sequence LIMIT ?""",
            (consumer_id, max(1, min(500, int(limit))))).fetchall()
    return [{**dict(row), "delivery_class": "durable_fact", "visibility": "host",
             "payload": runtime._read_json_blob(str(row["payload_ref"]))} for row in rows]


def maintain_facts(runtime, *, retain_per_stream=256, limit=500):
    """Bound acknowledged history; new consumers start with a state snapshot."""
    runtime._ensure_open()
    with runtime._state_lock:
        rows = runtime._state.execute("""SELECT e.event_id,e.process_id,e.kind,e.stream_sequence
            FROM runtime_outbox e WHERE
              EXISTS(SELECT 1 FROM deleted_processes d WHERE d.process_id=e.process_id)
              OR (e.stream_sequence <= COALESCE((SELECT MIN(c.last_sequence) FROM runtime_consumers c
                  WHERE c.stream_id=e.stream_id),0)
                AND e.stream_sequence < (SELECT MAX(t.stream_sequence) FROM runtime_outbox t
                    WHERE t.stream_id=e.stream_id)-?)
            LIMIT ?""", (max(32, int(retain_per_stream)), max(1, min(500, int(limit))))).fetchall()
        if not rows:
            return 0
        runtime._reserve_authoritative_write(512 + len(rows) * 64)
        runtime._state.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                runtime._state.execute("DELETE FROM runtime_outbox WHERE event_id=?", (row["event_id"],))
                if row["kind"] == "checkpoint_committed":
                    runtime._state.execute("DELETE FROM object_refs WHERE owner_type='runtime_fact' AND owner_id=?",
                        (f"{row['process_id']}:{row['stream_sequence']}",))
            runtime._state.execute("COMMIT")
        except BaseException:
            runtime._state.execute("ROLLBACK")
            raise
        return len(rows)


def acknowledge(runtime, consumer_id, event):
    """Ack an existing envelope, never an arbitrary caller-provided sequence."""
    acknowledge_batch(runtime, consumer_id, [event])


def acknowledge_batch(runtime, consumer_id, events):
    if not consumer_id or len(consumer_id) > 128:
        raise ValueError("RUNTIME_CONSUMER_INVALID")
    if not events:
        return
    runtime._ensure_open()
    with runtime._state_lock:
        runtime._reserve_authoritative_write(256 + len(events) * 32)
        runtime._state.execute("BEGIN IMMEDIATE")
        try:
            for event in events:
                row = runtime._state.execute("SELECT stream_id,stream_sequence FROM runtime_outbox WHERE event_id=?",
                    (str(event["event_id"]),)).fetchone()
                if row is None or row["stream_id"] != event["stream_id"] or row["stream_sequence"] != event["stream_sequence"]:
                    raise ValueError("RUNTIME_EVENT_ACK_INVALID")
                next_row = runtime._state.execute("""SELECT MIN(e.stream_sequence) FROM runtime_outbox e
                    WHERE e.stream_id=? AND e.stream_sequence>COALESCE((SELECT last_sequence
                    FROM runtime_consumers WHERE consumer_id=? AND stream_id=?),0)""",
                    (row["stream_id"], consumer_id, row["stream_id"])).fetchone()
                current = runtime._state.execute("SELECT last_sequence FROM runtime_consumers WHERE consumer_id=? AND stream_id=?",
                    (consumer_id, row["stream_id"])).fetchone()
                if (current is None or row["stream_sequence"] > current[0]) and next_row[0] != row["stream_sequence"]:
                    raise ValueError("RUNTIME_EVENT_ACK_OUT_OF_ORDER")
                runtime._state.execute("""INSERT INTO runtime_consumers(consumer_id,stream_id,last_sequence)
                    VALUES(?,?,?) ON CONFLICT(consumer_id,stream_id) DO UPDATE
                    SET last_sequence=MAX(runtime_consumers.last_sequence,excluded.last_sequence)""",
                    (consumer_id, row["stream_id"], row["stream_sequence"]))
            runtime._state.execute("COMMIT")
        except BaseException:
            runtime._state.execute("ROLLBACK")
            raise


def delete_process_facts(connection, process_id):
    """Called inside the existing deletion transaction, including privacy purge."""
    from .repositories.commands import purge_process_receipts
    purge_process_receipts(connection, process_id)
    rows = connection.execute("SELECT stream_sequence FROM runtime_outbox WHERE process_id=? AND kind='checkpoint_committed'",
        (process_id,)).fetchall()
    for row in rows:
        connection.execute("DELETE FROM object_refs WHERE owner_type='runtime_fact' AND owner_id=?",
            (f"{process_id}:{row[0]}",))
    connection.execute("DELETE FROM runtime_outbox WHERE process_id=?", (process_id,))
    connection.execute("DELETE FROM runtime_consumers WHERE stream_id IN (?,?)",
        (f"process:{process_id}", f"ledger:{process_id}"))


class FactDispatcher:
    def __init__(self, runtime, consumer_id, handler, on_error):
        self.runtime, self.consumer_id = runtime, consumer_id
        self.handler, self.on_error = handler, on_error
        self.failed_event = ""
        self.last_maintenance = 0.0

    def drain(self, limit=100):
        delivered = []
        try:
            events = read_facts(self.runtime, self.consumer_id, limit=limit)
            for event in events:
                self.handler(event)
                delivered.append(event)
            acknowledge_batch(self.runtime, self.consumer_id, delivered)
            self.failed_event = ""
            import time
            if time.monotonic() - self.last_maintenance > 60:
                maintain_facts(self.runtime)
                self.last_maintenance = time.monotonic()
            return len(events)
        except Exception as exc:
            # Ack only the handled prefix; a failed callback and all following
            # facts remain available. Ack failure safely replays observations.
            if delivered:
                try:
                    acknowledge_batch(self.runtime, self.consumer_id, delivered)
                except Exception:
                    pass
            identity = locals().get("event", {}).get("event_id", "read")
            if identity != self.failed_event:
                self.on_error({"event": "runtime_projection_failed", "consumer_id": self.consumer_id,
                    "event_id": identity, "message": str(exc), "retryable": True})
            self.failed_event = identity
            return 0
