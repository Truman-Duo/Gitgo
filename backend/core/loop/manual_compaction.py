"""Parked-session maintenance; never changes the outcome of its completed task."""
from __future__ import annotations

from backend.core.errors import error_payload
from backend.core.loop.context_window import ContextWindow
from backend.core.loop.decision_support import build_context_force_compaction_decision
from backend.core.loop.provider_protocol import deterministic_hash


def compact_parked_session(session, provider, *, process_id: str, task_id: str,
                           decision_id: str = "", choice: str = "") -> dict:
    window = ContextWindow(session.model_context_limit)
    pending = session.pending_compaction_decision
    fingerprint = deterministic_hash(session.messages)
    previous_epoch = session.context_epoch
    if decision_id or choice:
        if (not pending or decision_id != pending.get("decision_id")
                or fingerprint != pending.get("context_fingerprint")):
            return {"status": "failed", **error_payload(
                "CONTEXT_COMPACTION_FAILED", message="Compaction approval is missing or stale; run /compact again.",
            )}
        if choice == "stop":
            session.pending_compaction_decision = None
            return {"status": "cancelled", "changed": False}
        if choice != "force_compact":
            return {"status": "failed", **error_payload(
                "CONTEXT_COMPACTION_FAILED", message="Unknown compaction choice.",
            )}
        changed = window.force_compact(session, reason="explicit parked-session approval")
        session.pending_compaction_decision = None
    elif pending and pending.get("context_fingerprint") == fingerprint:
        return {"status": "awaiting_user", "pending_decision": pending}
    else:
        session.pending_compaction_decision = None
        changed = window.compact(session, provider)

    reason = window.last_compaction_error
    if changed or (not choice and reason == "not_enough_foldable_history"):
        session.compaction_failure_count = 0
        session.last_compaction_error = ""
        return {"status": "completed", "changed": changed,
                "previous_epoch": previous_epoch, "context_epoch": session.context_epoch,
                **({"reason": reason} if reason else {})}

    session.compaction_failure_count += 1
    session.last_compaction_error = reason or "compaction_returned_no_epoch"
    failure = error_payload(
        "CONTEXT_LIMIT_CONFIGURATION_INVALID" if choice else "CONTEXT_COMPACTION_FAILED",
        details={"reason": session.last_compaction_error,
                 "attempt": session.compaction_failure_count, "max_attempts": 3},
    )
    if not choice and session.compaction_failure_count >= 3:
        pending = build_context_force_compaction_decision(process_id, task_id)
        pending["context_fingerprint"] = fingerprint
        # Maintenance leaves the old completed task alone; declining only
        # cancels this maintenance request, never relabels prior work as failed.
        pending["options"][1]["immediate_effect"] = "Cancel compaction and leave the conversation unchanged."
        pending["options"][1]["downstream_effect"] = "You can correct provider limits or retry /compact later."
        pending["options"][1]["risks"] = "The existing context may still exceed the provider limit."
        session.pending_compaction_decision = pending
        return {"status": "awaiting_user", "pending_decision": pending, **failure}
    return {"status": "failed", "attempt": session.compaction_failure_count,
            "retry_compaction": not bool(choice), **failure}
