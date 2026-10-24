"""Host lifecycle guards. A terminal execution cannot acquire a new lease."""
from __future__ import annotations

import threading

TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out", "killed"})
RECOVERY = frozenset({"orphaned", "resume_available", "recovery_review_required"})
ACTIVE = frozenset({"running", "waiting", "awaiting_user"})


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
