"""Read-only block locations over the existing immutable trace records.

No body, summary, or approval is persisted here. Snapshot-bound locations can
always be rebuilt from the original SQLite events. Bodies use the same source.
"""
from __future__ import annotations

import json

_DELTAS = ("reasoning_delta", "text_delta", "toolcall_delta", "progress_delta")
_BODY_FIELDS = ("delta", "result_preview", "diff", "arguments", "messages", "tools", "payload", "request_body", "content", "text")


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"TRACE_INDEX_INVALID_{name.upper()}")
    return value


def _where(trace_id, process_id):
    sql, parameters = "trace_id=?", [trace_id]
    if process_id:
        sql += " AND json_extract(record_json, '$.process_id')=?"
        parameters.append(process_id)
    return sql, parameters


def _snapshot(self, trace_id, process_id, supplied):
    where, parameters = _where(trace_id, process_id)
    if supplied is not None:
        if not isinstance(supplied, dict) or supplied.get("schema") != 1 or supplied.get("trace_id") != trace_id or supplied.get("process_id") != process_id:
            raise ValueError("TRACE_INDEX_SCOPE_MISMATCH")
        first = _integer(supplied.get("first_seq"), "first_seq")
        last = _integer(supplied.get("last_seq"), "last_seq")
        count = _integer(supplied.get("event_count"), "event_count")
        if last < first:
            raise ValueError("TRACE_INDEX_INVALID_SNAPSHOT")
        where += " AND sequence>=? AND sequence<=?"
        parameters.extend([first, last])
    row = self._observability.execute(
        f"SELECT MIN(sequence), MAX(sequence), COUNT(*) FROM trace_events WHERE {where}", parameters,
    ).fetchone()
    actual = {"schema": 1, "trace_id": trace_id, "process_id": process_id,
              "first_seq": int(row[0] or 0), "last_seq": int(row[1] or 0), "event_count": int(row[2] or 0)}
    if supplied is not None and any(actual[key] != supplied.get(key) for key in actual):
        raise ValueError("TRACE_INDEX_SNAPSHOT_EXPIRED")
    return actual


def read_trace_index(self, trace_id: str, *, process_id: str = "", after_seq: int = -1,
                     limit: int = 200, snapshot: dict | None = None) -> dict:
    self._ensure_open()
    after_seq = _integer(after_seq, "after_seq", -1)
    bounded = min(500, _integer(limit, "limit", 1))
    with self._observability_lock:
        frozen = _snapshot(self, trace_id, process_id, snapshot)
        if after_seq > frozen["last_seq"]:
            raise ValueError("TRACE_INDEX_INVALID_AFTER_SEQ")
        where, parameters = _where(trace_id, process_id)
        parameters.extend([frozen["first_seq"], frozen["last_seq"]])
        delta_names = ",".join("?" for _ in _DELTAS)
        # Only adjacent fragments with identical source/channel/update identity
        # share a body range. A semantic boundary or another actor splits it.
        cte = f"""WITH source AS (
          SELECT sequence, event_type, record_json,
            CASE WHEN event_type IN ({delta_names}) THEN json_array(event_type,
              COALESCE(json_extract(record_json,'$.process_id'),''),
              COALESCE(json_extract(record_json,'$.visibility'),''),
              COALESCE(json_extract(record_json,'$.update_id'),''),
              COALESCE(json_extract(record_json,'$.task_id'),''),
              COALESCE(json_extract(record_json,'$.tool_call_id'),''))
            ELSE json_array(sequence) END AS identity,
            CASE WHEN event_type IN ({delta_names})
              THEN length(COALESCE(json_extract(record_json,'$.delta'),'')) ELSE 0 END AS chars
          FROM trace_events WHERE {where} AND sequence>=? AND sequence<=?
        ), boundaries AS (
          SELECT *, CASE WHEN lag(identity) OVER (ORDER BY sequence)=identity THEN 0 ELSE 1 END AS starts
          FROM source
        ), located AS (
          SELECT *, sum(starts) OVER (ORDER BY sequence) AS block FROM boundaries
        ), spans AS (
          SELECT min(sequence) AS first_seq,max(sequence) AS last_seq,count(*) AS event_count,sum(chars) AS text_chars
          FROM located GROUP BY block
        ) """
        query_parameters = [*_DELTAS, *_DELTAS, *parameters]
        split = self._observability.execute(cte + "SELECT 1 FROM spans WHERE first_seq<=? AND last_seq>? LIMIT 1",
                                            [*query_parameters, after_seq, after_seq]).fetchone()
        if split:
            raise ValueError("TRACE_INDEX_CURSOR_SPLIT_BLOCK")
        ignored = ",".join("?" for _ in _BODY_FIELDS)
        rows = self._observability.execute(cte + f"""
          SELECT s.*, e.event_type, json_extract(e.record_json,'$.decision.decision_id') AS decision_id,
            (SELECT json_group_object(j.key,json(CASE j.type
              WHEN 'text' THEN json_quote(substr(j.value,1,512))
              WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' WHEN 'null' THEN 'null'
              WHEN 'object' THEN CASE
                WHEN length(j.value)<=2048 THEN j.value
                WHEN e.event_type='worktree_promoted' AND j.key='promotion'
                  AND json_type(j.value,'$.process_ids')='array'
                  AND length(json_extract(j.value,'$.process_ids'))<=2048
                  THEN json_object('process_ids',json_extract(j.value,'$.process_ids'))
                ELSE 'null' END
              WHEN 'array' THEN CASE WHEN length(j.value)<=2048 THEN j.value ELSE 'null' END
              ELSE json_quote(j.value) END))
             FROM json_each(e.record_json) j WHERE j.key NOT IN ({ignored})) AS header
          FROM spans s JOIN trace_events e ON e.trace_id=? AND e.sequence=s.first_seq
          WHERE s.first_seq>? ORDER BY s.first_seq LIMIT ?
        """, [*query_parameters, *_BODY_FIELDS, trace_id, after_seq, bounded + 1]).fetchall()
        has_more = len(rows) > bounded
        rows = rows[:bounded]
        events = []
        for row in rows:
            header = json.loads(row["header"] or "{}")
            if row["decision_id"] and not header.get("decision_id"):
                header["decision_id"] = str(row["decision_id"])
            reference = {"schema": 1, "snapshot": frozen, "first_seq": int(row["first_seq"]),
                         "last_seq": int(row["last_seq"]), "event": str(row["event_type"]),
                         "process_id": str(header.get("process_id") or ""),
                         "event_count": int(row["event_count"]), "text_chars": int(row["text_chars"])}
            header.update({"seq": int(row["first_seq"]), "event": str(row["event_type"]), "body_ref": reference})
            events.append(header)
    return {"trace_id": trace_id, "schema": 1, "snapshot": frozen, "events": events,
            "next_seq": int(rows[-1]["last_seq"]) if rows else after_seq, "has_more": has_more}


def read_trace_block(self, trace_id: str, reference: dict, *, after_seq: int | None = None, limit: int = 500) -> dict:
    self._ensure_open()
    if not isinstance(reference, dict) or reference.get("schema") != 1:
        raise ValueError("TRACE_BLOCK_REFERENCE_INVALID")
    snapshot = reference.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("TRACE_BLOCK_REFERENCE_INVALID")
    process_id = str(reference.get("process_id") or "")
    scope = str(snapshot.get("process_id") or "")
    if scope and process_id != scope:
        raise ValueError("TRACE_INDEX_SCOPE_MISMATCH")
    first = _integer(reference.get("first_seq"), "first_seq")
    last = _integer(reference.get("last_seq"), "last_seq")
    cursor = first - 1 if after_seq is None else _integer(after_seq, "after_seq", -1)
    bounded = min(5000, _integer(limit, "limit", 1))
    if last < first or cursor < first - 1 or cursor > last:
        raise ValueError("TRACE_BLOCK_RANGE_MISMATCH")
    with self._observability_lock:
        frozen = _snapshot(self, trace_id, scope, snapshot)
        if first < frozen["first_seq"] or last > frozen["last_seq"]:
            raise ValueError("TRACE_BLOCK_RANGE_MISMATCH")
        where = "trace_id=? AND sequence>=? AND sequence<=? AND event_type=? AND COALESCE(json_extract(record_json,'$.process_id'),'')=?"
        parameters = [trace_id, first, last, str(reference.get("event") or ""), process_id]
        limits = self._observability.execute(f"SELECT MIN(sequence),MAX(sequence),COUNT(*) FROM trace_events WHERE {where}", parameters).fetchone()
        if int(limits[0] or 0) != first or int(limits[1] or 0) != last or int(limits[2]) != reference.get("event_count"):
            raise ValueError("TRACE_BLOCK_REFERENCE_MISMATCH")
        rows = self._observability.execute(f"SELECT sequence,record_json FROM trace_events WHERE {where} AND sequence>? ORDER BY sequence LIMIT ?",
                                            [*parameters, cursor, bounded]).fetchall()
        events = [json.loads(row["record_json"]) for row in rows]
        next_seq = int(rows[-1]["sequence"]) if rows else cursor
    return {"trace_id": trace_id, "snapshot": frozen, "body_ref": reference, "events": events,
            "next_seq": next_seq, "has_more": next_seq < last}
