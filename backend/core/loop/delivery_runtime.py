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
                 nudge_count, final_review=None):
        self.process, self.session, self.guard, self.signal_bus = process, session, guard, signal_bus
        self.emit, self.apply_mailbox, self.pause_blocked = emit, apply_mailbox, pause_blocked
        self.capture_recovery = capture_recovery
        self.increment_nudge, self.nudge_count = increment_nudge, nudge_count
        self.final_review = final_review

    def has_registered_stages(self):
        from backend.core.mechanism_runtime.application import bound_application
        composition = bound_application(self.process)
        return composition is not None and composition.has_completion_stages()

    def finish(self, content, duration_ms, window_check, *, source,
               degraded=False, clear_racing_claim=False):
        from .mechanism_lifecycle import fence
        with fence(self.process), self.session._mechanism_lock:
            if self._refresh_for_input():
                return DeliveryBoundary(retry=True)
        from backend.core.mechanism_runtime.application import bound_application
        from backend.core.mechanism_runtime.contracts import digest
        composition = bound_application(self.process)
        staged_completion = composition is not None and composition.has_completion_stages()
        generations = []
        if composition:
            from .review_evidence import prepare_review_evidence
            event = {"candidate": content, "source": source, **prepare_review_evidence(self.process)}
            event_id = "completion:" + digest({"event": event, "context_version": self.process.context_version})
            runs = composition.dispatch("completion", event_id, event)
            generations = [{"id": run["instance_id"], "epoch": run["epoch"], "definition_ref": run["definition_ref"]} for run in runs]
            directive = composition.completion.outcome(event_id, runs=runs)
            with fence(self.process), self.session._mechanism_lock:
                if self._refresh_for_input() or (directive and directive['kind'] == 'refresh'):
                    self.process.completion_claim = None
                    return DeliveryBoundary(retry=True)
                if directive and directive['kind'] == 'awaiting_user':
                    directive = composition.completion.publish_wait(directive)
                    return self._wait_for_stage(directive)
            if directive and directive["kind"] == "rework":
                import json
                self.process.completion_claim = None
                self.session.append_host_steering("[MECHANISM COMPLETION REVIEW]\n" + json.dumps(directive, ensure_ascii=False)
                    + "\nContinue the existing execution path; all original completion obligations remain.",
                    steering_type="mechanism_completion_rework", version=event_id)
                return DeliveryBoundary(retry=True)
        if staged_completion and self.final_review is not None:
            outcome = self.final_review(content)
            if outcome is not None:
                if "alignment_repair" not in outcome:
                    return DeliveryBoundary(result=outcome)
                from .answer_continuation import clear_answer_fragments
                clear_answer_fragments(self.process)
                self.process.completion_claim = None
                self.increment_nudge(self.process, "alignment_repair")
                if self.nudge_count(self.process, "alignment_repair") >= 3:
                    transition(self.process, ProcessStatus.FAILED)
                    return DeliveryBoundary(result=_make_result(self.process, self.session, content,
                        error_code="ALIGNMENT_DELIVERY_REJECTED", error_message=outcome["alignment_repair"], outcome_status=OutcomeStatus.FAILED))
                self.session.append_host_steering("[HOST REQUIREMENT CHECK]\n" + outcome["alignment_repair"],
                    steering_type="alignment_after_completion_stages", version=event_id)
                return DeliveryBoundary(retry=True)
        with fence(self.process), self.session._mechanism_lock:
            return self._finish_current(content, duration_ms, window_check, source=source,
                degraded=degraded, clear_racing_claim=clear_racing_claim, composition=composition, generations=generations)

    def _refresh_for_input(self):
        if self.process.mailbox is None or not self.process.mailbox.snapshot()['pending']:
            return False
        self.apply_mailbox()
        self.process.completion_claim = None
        self.session.append_host_steering(
            '[COMPLETION INPUT CHANGED]\nAccepted input arrived before delivery. Interpret it and reconcile the current task before the next completion candidate. '
            'Finished stage results retain their original generation; do not replay effects, reset return counters or treat feedback as a user answer.',
            steering_type='completion_input_changed', version=str(self.process.context_version))
        return True

    def _wait_for_stage(self, directive):
        from .decision_support import format_decision_request
        decision = self.process.pending_decision
        if not decision or decision['decision_id'] != directive['decision_id']:
            raise ValueError('MECHANISM_COMPLETION_DECISION_CHANGED')
        transition(self.process, ProcessStatus.AWAITING_USER)
        self.emit({'event': 'decision_required', 'decision': dict(decision)})
        result = _make_result(self.process, self.session, format_decision_request(decision), outcome_status=OutcomeStatus.AWAITING_USER)
        self.process.result = result
        return DeliveryBoundary(result=result)

    def _finish_current(self, content, duration_ms, window_check, *, source,
                        degraded=False, clear_racing_claim=False, composition=None, generations=()):
        process, session = self.process, self.session
        for admitted in generations:
            current, _ = composition.repo.instance(composition.task, admitted["id"])
            if (current is None or current["epoch"] != admitted["epoch"] or current["effective_ref"] != admitted["definition_ref"]
                    or current["phase"] not in {"ready", "pending_activation", "activating"}):
                session.append_host_steering("[COMPLETION GENERATION CHANGED]\nThe observed stage result retains its original generation. Reassess this candidate at the next current boundary; do not replay committed business effects.",
                    steering_type="completion_generation_changed", version=str(process.context_version))
                return DeliveryBoundary(retry=True)
        # Scope adoption and completion linearize on the same admission fence.
        # An earlier assessment cannot close a task after a new accepted delta.
        evaluation = HostCompletionEvaluator.evaluate(process, content)
        if not evaluation.allowed:
            from .completion_gates import accepts_partial
            if not degraded or not accepts_partial(process, list(evaluation.gates)):
                session.append_host_steering("[TASK STATE CHANGED BEFORE DELIVERY]\n" + "\n".join(evaluation.reasons),
                    steering_type="delivery_state_changed", version=str(process.context_version))
                return DeliveryBoundary(retry=True)
        if process.mailbox is not None and not process.mailbox.close_if_empty('task completed'):
            if clear_racing_claim:
                process.completion_claim = None
            self.apply_mailbox()
            return DeliveryBoundary(retry=True)
        self.capture_recovery(process)
        transition(process, ProcessStatus.COMPLETED)
        self.emit({'event': 'completion_gate', 'accepted': True, 'source': source, 'harness_generations': list(generations)})
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
        staged = self.has_registered_stages()
        stage_arguments = {"completion_stage_entry": True} if staged else {}
        guard_result = self.guard.check(process, content, session, self.signal_bus, signals, **stage_arguments)
        if guard_result.is_complete:
            public_content = CompletionClaim.from_response(content, step=process.steps_used).result
            return self.finish(public_content, duration_ms, window_check,
                               source='text_marker', degraded=guard_result.degraded)
        if guard_result.blocked:
            return self.blocked(guard_result, content)
        evaluator = HostCompletionEvaluator.evaluate_stage_entry if staged else HostCompletionEvaluator.evaluate
        if process.task_kind == 'supervisor' and content.strip():
            if evaluator(process, content, signals).allowed:
                return self.finish(content, duration_ms, window_check, source='supervisor_plain_text')
        if (process.task_kind == 'action' and content.strip() and process.completion_claim is None
                and process._nudge_counters.get('evidence_ready', 0)):
            process.completion_claim = CompletionClaim.from_response(content, step=process.steps_used)
            if evaluator(process, content, signals).allowed:
                return self.finish(process.completion_claim.result, duration_ms, window_check,
                    source='host_evidence_plain_text', clear_racing_claim=True)
            # Keep the claim visible to existing outstanding-gate projections.
        return DeliveryBoundary()
