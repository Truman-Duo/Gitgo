"""Host-owned semantic review routing, billing, validation and pause outcomes.

Loop/provider transitions are explicit hooks. This controller has no tool
execution or permission authority; mechanical gates remain in their domains.
"""
from dataclasses import dataclass
from typing import Callable
import json
import time

from .lifecycle import transition
from .models import ProcessStatus
from .outcome import OutcomeStatus


@dataclass(frozen=True)
class AlignmentReviewHooks:
    emit: Callable
    frontend_emit: Callable | None
    notice: Callable
    refresh_prompt: Callable
    before_attempt: Callable
    route_notice: Callable
    result: Callable
    cancellation: Callable
    budget_exhausted: Callable


class AlignmentReview:
    def __init__(self, process, provider, hooks):
        self.process, self.provider, self.hooks = process, provider, hooks

    def check(self, instruction, candidate="", proposed_questions=None, *, trigger='admission', proposed_contracts=None, proposed_files=None):
        try:
            return self._check(instruction, candidate, proposed_questions, trigger=trigger, proposed_contracts=proposed_contracts, proposed_files=proposed_files)
        except Exception as exc:
            return self.failure(exc)

    def _check(self, instruction, candidate, proposed_questions, *, trigger, proposed_contracts, proposed_files):
        process, llm_provider, hooks = self.process, self.provider, self.hooks
        session = process.session
        _emit_observation = hooks.emit
        on_stream_event = hooks.frontend_emit
        before_attempt, alignment_route_notice = hooks.before_attempt, hooks.route_notice
        # A user's root task is assessed without a copied skill prompt. Worker
        # engineering decisions retain the same source-bound resolution tool.
        alignment_context, _ = process.read_context_snapshot()
        own_alignment = alignment_context.get('alignment_runtime') or {}
        participates = (own_alignment.get('enforced') and own_alignment.get('task_id') == (process.active_task_id or process.process_id)
                        and own_alignment.get('process_id') == process.process_id)
        if proposed_questions is None and (process.parent_id or process.actor_kind != "supervisor") and not participates:
            return None
        from backend.core.loop.alignment_runtime import AlignmentRuntime, KEY
        runtime = AlignmentRuntime(process, on_stream_event)
        if candidate:
            from backend.core.loop.completion_protocol import CompletionClaim
            candidate = CompletionClaim.from_response(candidate, step=process.steps_used).result
        payload = runtime.payload(instruction, candidate, proposed_questions, proposed_contracts, proposed_files)
        for notice in payload.get('artifact_notices', []):
            hooks.notice('DELIVERY_ARTIFACT_UNAVAILABLE', '交付文件未能作为完整文本证据核验；保留正文检查。', detail=notice)
        from .alignment_routing import review_reason, REVIEW_POLICY
        process.update_context_snapshot(lambda snapshot: {**snapshot, KEY: {
            **snapshot.get(KEY, {}), 'review_policy': REVIEW_POLICY}})
        from .answer_review_cache import reuse_unresolved, remember_unresolved
        try:
            reused_outcome = reuse_unresolved(process, payload, runtime)
        except (ValueError, KeyError, TypeError) as invalid_cache:
            hooks.notice('ALIGNMENT_CACHE_INVALID', '旧解释不能用于当前问题，已回退到模型判断。',
                         error_type=type(invalid_cache).__name__)
            reused_outcome = None
        if reused_outcome is not None:
            _emit_observation({'event': 'alignment_assessment_reused', 'stage': payload['stage'],
                               'reason': 'identical_unresolved_reply'})
            hooks.notice('UNCHANGED_DECISION_INPUT',
                '答复和问题依赖未变化，复用已验证解释；仍需补充当前问题，或明确委托其余选择。')
            hooks.refresh_prompt()
            return self.pause(reused_outcome)
        reason = review_reason(process, payload, trigger)
        if not reason:
            from .alignment_routing import reuse_assessment
            reused = reuse_assessment(process, payload)
            if reused:
                _emit_observation({'event': 'alignment_assessment_reused', 'stage': payload['stage'], 'reason': reused})
                return None
            if payload['stage'] == 'delivery':
                from .alignment_evidence import alignment_input_digest, answer_digest
                process.update_context_snapshot(lambda snapshot: {**snapshot, KEY: {
                    **snapshot.get(KEY, {}), 'delivery': {
                        'candidate_digest': answer_digest(payload['candidate']),
                        'input_digest': alignment_input_digest(process),
                        'source': 'host_on_demand', 'scope': 'no_declared_semantic_dependency'}}})
            _emit_observation({'event': 'alignment_assessment_deferred', 'stage': payload['stage'],
                               'reason': 'main_model_first_actual_reply' if payload.get('actual_answers')
                               else 'main_model_no_declared_dependency'})
            return None
        assessor = getattr(llm_provider, "assess_alignment", None)
        if not callable(assessor):
            if not process.runtime_preferences.get("alignment_unavailable_notified"):
                process.runtime_preferences["alignment_unavailable_notified"] = True
                hooks.notice(
                    "ALIGNMENT_ASSESSOR_UNAVAILABLE", "当前模型接口未提供语义需求检查；已声明的问题仍受完成门禁约束。")
            return None
        try:
            from .alignment_routing import reuse_assessment
            reused = reuse_assessment(process, payload)
            if reused:
                _emit_observation({"event": "alignment_assessment_reused", "stage": payload["stage"], "reason": reused})
                return None
            assessment_started = time.monotonic()
            _emit_observation({"event": "alignment_assessment_started", "stage": payload["stage"], 'trigger': reason})
            for attempt in range(2):
                proposal = assessor(payload, before_attempt=before_attempt,
                                    on_notice=alignment_route_notice,
                                    cancel_event=process.cancellation_event)
                process.steps_used += 1
                usage = proposal.pop("_provider_usage", None) if isinstance(proposal, dict) else None
                if usage:
                    protocol = getattr(llm_provider, 'protocol', 'unknown')
                    session.record_provider_usage({**usage, 'purpose': 'alignment'}, protocol=str(getattr(protocol, 'value', protocol)))
                    _emit_observation({"event": "alignment_provider_usage", "purpose": "alignment", "stage": payload["stage"],
                        "usage": {k: v for k, v in usage.items() if k != "raw"},
                        "duration_ms": (time.monotonic() - assessment_started) * 1000})
                    if process.task_budget:
                        process.task_budget.record_provider_usage(usage)
                        process.task_budget.consume_output_estimate(int(usage.get("output_tokens", 0)), process.process_id)
                elif process.task_budget:
                    process.task_budget.consume_output(json.dumps(proposal), process.process_id)
                if process.cancel_requested:
                    return hooks.cancellation()
                _emit_observation({"event": "alignment_assessment_proposed", "stage": payload["stage"], "attempt": attempt + 1}, detail=proposal)
                try:
                    outcome = runtime.apply(proposal, payload)
                    break
                except (ValueError, KeyError, TypeError) as invalid:
                    _emit_observation({"event": "alignment_assessment_rejected", "attempt": attempt + 1,
                                       "reason": str(invalid)[:300]})
                    if attempt:
                        raise
                    # One compiler-style recovery, retaining source inputs and
                    # requirements. Invalid proposals never become user facts.
                    payload = {**payload, "host_validation_error": str(invalid)[:300], "rejected_proposal": proposal,
                               "provider_output_recovery": proposal.get("schema_error") == "provider_response_incomplete"
                               and proposal.get("incomplete_reason") == "max_output_tokens"}
            _emit_observation({"event": "alignment_assessment_completed", "stage": payload["stage"],
                               "status": outcome["status"], "duration_ms": (time.monotonic() - assessment_started) * 1000}, detail=proposal)
            hooks.refresh_prompt()
            try:
                remember_unresolved(process, payload, proposal)
            except (ValueError, KeyError, TypeError) as invalid_cache:
                hooks.notice('ALIGNMENT_CACHE_RECORD_FAILED', '本次解释已采用，但未能建立复用记录；后续仍可正常判断。',
                             error_type=type(invalid_cache).__name__)
            if outcome.get("decision"):
                return self.pause(outcome)
            if outcome["status"] == "repair":
                return {"alignment_repair": outcome["reason"] or "Use the confirmed user requirements before delivering."}
            if proposed_questions is not None:
                return {"alignment_repair": "No further user question is necessary for the current deliverable. "
                        + (outcome["reason"] or "Proceed using supplied facts and labelled working defaults.")}
            return None
        except Exception as exc:
            return self.failure(exc)

    def pause(self, outcome):
        from .decision_support import format_decision_request
        decision = outcome['decision']
        transition(self.process, ProcessStatus.AWAITING_USER)
        self.hooks.emit({'event': 'decision_required', 'decision': decision})
        response = ('已暂停方案，保留尚未决定的问题；准备好后可继续回答。\n\n'
                    if outcome['status'] == 'waiting' else '') + format_decision_request(decision)
        result = self.hooks.result(response, outcome_status=OutcomeStatus.AWAITING_USER)
        self.process.result = result
        return result

    def failure(self, exc):
        from .budget import TaskBudgetExceeded
        from .llm import StreamCancelledError
        process, hooks = self.process, self.hooks
        if isinstance(exc, StreamCancelledError) or process.cancel_requested:
            return hooks.cancellation()
        if isinstance(exc, TaskBudgetExceeded):
            return hooks.budget_exhausted(exc)
        hooks.notice('ALIGNMENT_REVIEW_FAILED', '需求语义检查未完成，已保留原任务和真实答案；请重试。', error_type=type(exc).__name__)
        hooks.emit({'event': 'alignment_assessment_failed', 'error_type': type(exc).__name__}, detail={'reason': str(exc)[:400]})
        transition(process, ProcessStatus.FAILED)
        return hooks.result('需求检查暂未完成，已保留任务和答案，未将其标记为完成。',
            error_code='ALIGNMENT_REVIEW_FAILED', error_message=type(exc).__name__, outcome_status=OutcomeStatus.FAILED)
