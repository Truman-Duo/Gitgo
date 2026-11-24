"""Explicit public updates in the existing session ledger, never reasoning."""
from datetime import datetime, timezone
import uuid


def record_progress(process, message, update_id="", *, source="agent", status="completed"):
    if not isinstance(message, str) or not message.strip() or (source == "agent" and len(message) > 8192):
        raise ValueError("PUBLIC_PROGRESS_REQUIRES_BOUNDED_MESSAGE")
    message = message.strip()
    identity = str(update_id or uuid.uuid4().hex)
    if len(identity) > 256 or any(ord(char) < 32 for char in identity):
        raise ValueError("PUBLIC_PROGRESS_ID_INVALID")
    task_id = process.active_task_id or process.process_id
    for record in reversed(process.session.host_ledger):
        if (record.get("event"), record.get("task_id"), record.get("process_id"), record.get("update_id")) == (
                "public_progress", task_id, process.process_id, identity):
            if record["message"] != message:
                raise ValueError("PUBLIC_PROGRESS_ID_CONFLICT")
            return record, False
    record = {
        "event": "public_progress", "update_id": identity, "task_id": task_id,
        "process_id": process.process_id, "message": message, "source": source, "status": status,
        "message_sequence": len(process.session.messages), "created_at": datetime.now(timezone.utc).isoformat(),
    }
    process.session.host_ledger.append(record)
    return record, True


def progress_timeline(ledger: list[dict]) -> list[dict]:
    """Public projection from original records; no API calls or state writes."""
    result, seen = [], set()
    for record in ledger:
        if record.get("event") != "public_progress":
            continue
        identity = f"progress:{record.get('task_id','')}:{record.get('process_id','')}:{record.get('update_id','')}"
        if identity in seen:
            continue
        seen.add(identity)
        result.append({
            "message_id": identity, "turn_id": str(record.get("task_id") or ""),
            "role": "assistant", "kind": "progress", "visibility": "public", "final": True,
            "content": str(record.get("message") or ""), "timestamp": str(record.get("created_at") or ""),
            "process_id": str(record.get("process_id") or ""), "update_id": str(record.get("update_id") or ""),
            "sequence": int(record.get("message_sequence", 0)), "progress_status": record.get("status", "completed"),
        })
    return result
