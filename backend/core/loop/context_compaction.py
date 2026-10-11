"""Context pressure, compaction accounting and explicit destructive fallback consent."""
from .lifecycle import transition
from .models import ProcessStatus
from .outcome import OutcomeStatus
from .runtime_outcomes import _make_result


class ContextCompaction:
    def __init__(self, process, session, provider, get_window, harness, emit, frontend_emit):
        self.process, self.session, self.provider = process, session, provider
        self.get_window, self.harness = get_window, harness
        self.emit, self.frontend_emit = emit, frontend_emit

    def run(self,
        trigger: str, *, force: bool = False, failure_required: bool = False,
        retention_suggestions: list[str] | None = None,
    ) -> tuple[bool, dict | None]:
        """Run one Host-accounted compaction and possibly pause for consent."""
        process, session = self.process, self.session
        context_window = self.get_window()
        llm_provider = self.provider
        harness = self.harness() if callable(self.harness) else self.harness
        _emit_observation, on_stream_event = self.emit, self.frontend_emit
        previous_epoch = session.context_epoch
        if force:
            changed = context_window.force_compact(session, reason=trigger)
        else:
            changed = context_window.compact(
                session, llm_provider, harness_data=harness,
                retention_suggestions=retention_suggestions,
                task_budget=process.task_budget,
                cancel_event=process.cancellation_event,
                budget_process_id=process.process_id,
                on_provider_notice=_emit_observation,
            )
        reason = str(context_window.last_compaction_error or "")
        no_op = reason == "not_enough_foldable_history" and not failure_required
        if changed:
            session.compaction_failure_count = 0
            session.last_compaction_error = ""
            _emit_observation({
                "event": "context_compaction_completed",
                "trigger": trigger,
                "forced": force,
                "previous_epoch": previous_epoch,
                "context_epoch": session.context_epoch,
                "changed": True,
            }, detail=(session.epoch_archive[-1] if session.epoch_archive else None))
            return True, None
        if no_op:
            _emit_observation({
                "event": "context_compaction_completed",
                "trigger": trigger,
                "forced": force,
                "previous_epoch": previous_epoch,
                "context_epoch": session.context_epoch,
                "changed": False,
                "reason": reason,
            })
            return False, None

        if force:
            transition(process, ProcessStatus.FAILED)
            return False, _make_result(
                process, session, "The immutable prompt prefix does not fit the configured window.",
                error_code="CONTEXT_LIMIT_CONFIGURATION_INVALID",
                error_message=reason or "Forced compaction could not produce a valid epoch",
            )
        session.compaction_failure_count += 1
        session.last_compaction_error = reason or "compaction_returned_no_epoch"
        from backend.core.errors import error_payload
        failure = error_payload(
            "CONTEXT_COMPACTION_FAILED",
            details={
                "trigger": trigger,
                "attempt": session.compaction_failure_count,
                "max_attempts": 3,
                "reason": session.last_compaction_error,
            },
            next_actions=[{
                "action": "retry_compaction",
                "effect": "Retry ordinary non-destructive compaction.",
            }],
        )
        _emit_observation({
            "event": "context_compaction_failed",
            "trigger": trigger,
            "forced": force,
            "attempt": session.compaction_failure_count,
            "max_attempts": 3,
            "error": failure["error_info"],
        })
        if session.compaction_failure_count < 3:
            return False, None

        from backend.core.loop.decision_support import (
            create_context_force_compaction_decision,
            format_decision_request,
        )
        decision = create_context_force_compaction_decision(process)
        transition(process, ProcessStatus.AWAITING_USER)
        if on_stream_event:
            on_stream_event({
                "event": "decision_required",
                "process_id": process.process_id,
                "decision": decision,
            })
        result = _make_result(
            process, session, format_decision_request(decision),
            outcome_status=OutcomeStatus.AWAITING_USER,
            error_code="CONTEXT_FORCE_COMPACTION_REQUIRED",
            error_message="Three ordinary context compactions failed",
        )
        process.result = result
        return False, result

