"""Text delivery transitions over existing evidence and completion policy.

This controller consumes an evaluated candidate; it neither interprets user
intent nor executes tools. A mailbox race requests another turn instead of
publishing a terminal result based on an outdated task boundary.
"""
from dataclasses import dataclass

from .completion_protocol import CompletionClaim, HostCompletionEvaluator
from .lifecycle import transition
from .models import ProcessStatus
from .outcome import OutcomeStatus
from .runtime_outcomes import _make_result


@dataclass(frozen=True)
class DeliveryBoundary:
    result: dict | None = None
    retry: bool = False


class DeliveryRuntime:
    def __init__(self, process, session, guard, signal_bus, *, emit,
                 apply_mailbox, pause_blocked, capture_recovery, increment_nudge,
                 nudge_count):
        self.process, self.session, self.guard, self.signal_bus = process, session, guard, signal_bus
        self.emit, self.apply_mailbox, self.pause_blocked = emit, apply_mailbox, pause_blocked
        self.capture_recovery = capture_recovery
        self.increment_nudge, self.nudge_count = increment_nudge, nudge_count

    def finish(self, content, duration_ms, window_check, *, source,
               degraded=False, clear_racing_claim=False):
        process, session = self.process, self.session
        self.emit({'event': 'completion_gate', 'accepted': True, 'source': source})
        if process.mailbox is not None and not process.mailbox.close_if_empty('task completed'):
            if clear_racing_claim:
                process.completion_claim = None
            self.apply_mailbox()
            return DeliveryBoundary(retry=True)
        self.capture_recovery(process)
        transition(process, ProcessStatus.COMPLETED)
        result = _make_result(process, session, content, duration_ms=duration_ms,
                              outcome_status=OutcomeStatus.DEGRADED if degraded else None,
                              window_action=window_check['action'],
                              window_usage_ratio=window_check['usage_ratio'])
        process.result = result
        return DeliveryBoundary(result=result)

    def blocked(self, guard_result, content):
        process, session = self.process, self.session
        self.emit({'event': 'completion_gate', 'accepted': False,
                   'source': 'text_marker', 'reason': guard_result.nudge_text})
        terminal = self.pause_blocked(guard_result, content)
        if terminal is not None:
            return DeliveryBoundary(result=terminal)
        code = guard_result.reason_code or 'completion_rejection'
        self.increment_nudge(process, code)
        if self.nudge_count(process, code) >= 3:
            transition(process, ProcessStatus.FAILED)
            return DeliveryBoundary(result=_make_result(process, session, content,
                error_code='NUDGE_ESCALATION', error_message='Completion requirements remained blocked',
                outcome_status=OutcomeStatus.FAILED))
        session.append_user(guard_result.nudge_text, message_type='governance_nudge')
        return DeliveryBoundary(retry=True)

    def evaluate_text(self, content, duration_ms, window_check, signals):
        process, session = self.process, self.session
        guard_result = self.guard.check(process, content, session, self.signal_bus, signals)
        if guard_result.is_complete:
            public_content = CompletionClaim.from_response(content, step=process.steps_used).result
            return self.finish(public_content, duration_ms, window_check,
                               source='text_marker', degraded=guard_result.degraded)
        if guard_result.blocked:
            return self.blocked(guard_result, content)
        if process.task_kind == 'supervisor' and content.strip():
            if HostCompletionEvaluator.evaluate(process, content, signals).allowed:
                return self.finish(content, duration_ms, window_check, source='supervisor_plain_text')
        if (process.task_kind == 'action' and content.strip() and process.completion_claim is None
                and process._nudge_counters.get('evidence_ready', 0)):
            process.completion_claim = CompletionClaim.from_response(content, step=process.steps_used)
            if HostCompletionEvaluator.evaluate(process, content, signals).allowed:
                return self.finish(process.completion_claim.result, duration_ms, window_check,
                    source='host_evidence_plain_text', clear_racing_claim=True)
            # Keep the claim visible to existing outstanding-gate projections.
        return DeliveryBoundary()
