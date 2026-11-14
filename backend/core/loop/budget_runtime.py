"""Wait for already-admitted work without billing retries or borrowing escrow."""
from .budget import TaskBudgetExceeded
from .llm import StreamCancelledError


def reserve_provider_call(process, emit=None):
    budget = process.task_budget
    waiting = False
    while True:
        if process.cancellation_event.is_set() or process.cancel_requested:
            raise StreamCancelledError("cancelled while waiting for task-tree capacity")
        if budget is not None:
            budget.check_deadline()
        try:
            # Linearize new provider admission with peer feedback checkpoints.
            # Release before any budget wait or model work: already admitted
            # calls remain in flight; later attempts must check again.
            from .mechanism_lifecycle import fence
            from .mailbox import MailboxRecoveryRequired
            with fence(process), process.session._mechanism_lock:
                if (process.recovery or {}).get("checkpoint_commit") == "unknown":
                    raise MailboxRecoveryRequired("PROCESS_RECOVERY_REQUIRED: reconcile the checkpoint before a new provider call")
                index = budget.begin_provider_call(process.process_id) if budget is not None else 0
            if waiting and emit:
                emit({"event": "budget_wait_completed", "process_id": process.process_id})
            return index
        except TaskBudgetExceeded as exc:
            manager = getattr(process, "_manager", None)
            blockers = exc.details.get("blocking_process_ids", [])
            if not exc.details.get("waiting_on_escrow") or manager is None:
                raise
            live = [pid for pid in blockers if manager.get(pid) is not None
                    and not manager.get(pid).lifecycle_finalized]
            if not live:
                raise
            if not waiting and emit:
                emit({"event": "progress_summary", "phase": "budget_wait",
                      "message": "Waiting for admitted workers to finish and release reserved capacity.",
                      "process_id": process.process_id})
            waiting = True
            manager.wait_many(live, timeout=min(0.5, budget.remaining_seconds()),
                              cancel_event=process.cancellation_event, return_when="any_terminal")
