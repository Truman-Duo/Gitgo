"""Host identity for feedback across ordered batch slicing and cold resume."""
from __future__ import annotations

FIELD = "_host_feedback_identity"


def call_identity(call, execution_id):
    record = call.get(FIELD)
    if record is None:
        return None
    if (not isinstance(record, dict) or record.get("execution_id") != execution_id
            or type(record.get("ordinal")) is not int or record["ordinal"] < 0):
        raise ValueError("FEEDBACK_CALL_IDENTITY_INVALID")
    return (execution_id, "host_ordinal", record["ordinal"])


def bind_calls(process, calls, execution_id, *, resumed=False):
    """Bind before filtering/slicing; provider IDs may be absent or repeated.

    Only feedback needs this delivery identity. Receipt indexes and provider
    call IDs retain their existing meanings. A fresh provider batch never gets
    to supply its own Host ordinal; persisted suffixes retain the original one.
    """
    bound, seen = [], set()
    for ordinal, call in enumerate(calls):
        if call.get("name") != "send_feedback":
            bound.append(call)
            continue
        identity = call_identity(call, execution_id) if resumed else None
        if resumed and identity is None:
            # Base-version pending batches have no delivery identity and remain
            # resumable. The unmerged older draft used ambiguous local indexes;
            # do not silently replay its already durable receipts on upgrade.
            manager = getattr(process, "_manager", None)
            child = manager.get(str((call.get("args") or {}).get("process_id", ""))) if manager else None
            messages = child.mailbox.snapshot()["messages"] if child and child.mailbox else []
            if any(str(item.get("message_id", "")).startswith("feedback-")
                   and not str(item["message_id"]).startswith("feedback-v2-") for item in messages):
                raise ValueError("FEEDBACK_LEGACY_IDENTITY_REVIEW_REQUIRED")
        record = dict(call[FIELD]) if identity else {"execution_id": execution_id, "ordinal": ordinal}
        if record["ordinal"] in seen:
            raise ValueError("FEEDBACK_CALL_IDENTITY_DUPLICATED")
        seen.add(record["ordinal"])
        bound.append({**call, FIELD: record})
    return bound
