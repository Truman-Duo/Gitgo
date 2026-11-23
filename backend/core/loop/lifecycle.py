"""Host lifecycle guards. A terminal execution cannot acquire a new lease."""
from __future__ import annotations

import threading

TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out", "killed"})
RECOVERY = frozenset({"orphaned", "resume_available", "recovery_review_required"})
ACTIVE = frozenset({"running", "waiting", "awaiting_user"})


def execution_closeout_from_state(status: str, runtime: dict, result=None) -> dict:
    """Project durable closeout without treating a terminal label as a commit.

    Older checkpoints without finality evidence remain pending until recovery
    reconciles them.  A failed barrier ends execution, but is never durable
    success and must not authorize consuming or promoting its output.
    """
    finalized = runtime.get("lifecycle_finalized") is True
    outcome = result if isinstance(result, dict) else runtime.get("result") or {}
    if status not in TERMINAL:
        state = "active"
    elif finalized:
        state = "finalized"
    elif outcome.get("code") == "SESSION_CHECKPOINT_FAILED":
        state, finalized = "failed", False
    else:
        state = "finalized" if finalized else "pending"
    return {"lifecycle_finalized": finalized, "closeout_state": state}


def execution_closeout(process, manager=None) -> dict:
    """Use the same finality boundary for observation, waiting and acceptance."""
    manager = manager or getattr(process, "_manager", None)
    thread = getattr(manager, "_threads", {}).get(process.process_id)
    view = execution_closeout_from_state(
        process.status.value,
        {"lifecycle_finalized": bool(getattr(process, "lifecycle_finalized", False))},
        getattr(process, "result", None),
    )
    if view["closeout_state"] == "pending":
        if thread is not None and not thread.is_alive():
            view["closeout_state"] = "failed"
        elif (thread is None and not getattr(process, "durable_revision", 0)
              and not getattr(process, "recovery", None)):
            # Synchronous in-memory callers predate thread finality.  This
            # compatibility path excludes every registered run and checkpoint.
            view = {"lifecycle_finalized": True, "closeout_state": "finalized"}
    return view


def is_consumable_terminal(process, manager=None) -> bool:
    return execution_closeout(process, manager)["closeout_state"] == "finalized"


class InvalidTransition(ValueError):
    code = "PROCESS_TRANSITION_REJECTED"


def block_uncertain_checkpoint(process):
    """Stop local continuation without pretending a durable decision rolled back."""
    from .models import ProcessStatus
    from .read_approval import revoke_read_requests
    with process._coordination_lock:
        if process.status.value not in TERMINAL:
            process.status = ProcessStatus.RECOVERY_REVIEW_REQUIRED
        process.pending_decision = None
        process.pending_tool_batch = None
        process.approval_grants = []
        process.recovery = {**(process.recovery or {}),
            "state": "recovery_review_required", "resume_forbidden": True,
            "requires_manual_verification": True,
            "checkpoint_commit": "unknown"}
        revoke_read_requests(process)


def validate_transition(previous: str, proposed: str) -> None:
    if previous == proposed:
        return
    allowed = (
        previous in ACTIVE and proposed in ACTIVE | TERMINAL | {"cancelling"}
        or previous == "cancelling" and proposed in {"cancelled", "timed_out", "killed", "failed"}
        or previous in RECOVERY and proposed in {"recovering", "cancelled", "failed", "killed"}
        or previous == "recovering" and proposed in ACTIVE | RECOVERY | TERMINAL | {"cancelling"}
    )
    if not allowed:
        raise InvalidTransition(f"PROCESS_TRANSITION_REJECTED: {previous} -> {proposed}")


def transition(process, proposed):
    """Cancellation wins a concurrent success; same-state calls are idempotent."""
    from .models import ProcessStatus
    lock = getattr(process, "_coordination_lock", None)
    if lock is None:
        lock = threading.RLock()
        process._coordination_lock = lock
    with lock:
        previous = process.status
        if getattr(process, "cancel_requested", False) and proposed == ProcessStatus.COMPLETED:
            proposed = ProcessStatus.CANCELLED
        # A worker unwinding after cancellation cannot rewrite its terminal outcome.
        if previous.value in {"cancelled", "timed_out", "killed"} and proposed.value in TERMINAL:
            return previous
        # A model result precedes Host sealing/privacy checks. Only that
        # provisional success may still fail, never a finalized execution.
        if (previous == ProcessStatus.COMPLETED and proposed == ProcessStatus.FAILED
                and not getattr(process, "lifecycle_finalized", False)):
            process.status = proposed
            return proposed
        validate_transition(previous.value, proposed.value)
        process.status = proposed
        if proposed.value in TERMINAL | RECOVERY | {"cancelling"}:
            from .read_approval import revoke_read_requests
            revoke_read_requests(process)
        return proposed
