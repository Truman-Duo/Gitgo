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


class InvalidAlignmentAssessment(ValueError):
    """A returned semantic proposal failed evidence compilation, not transport."""
    def __init__(self, message, *, schema_error=None, validation_details=None):
        super().__init__(message)
        self.schema_error = schema_error
        self.validation_details = validation_details
        self.code = {
            'invalid_json': 'ALIGNMENT_RESPONSE_JSON_INVALID',
            'object_required': 'ALIGNMENT_RESPONSE_SCHEMA_INVALID',
            'provider_response_incomplete': 'ALIGNMENT_RESPONSE_INCOMPLETE',
            'delivery_check_required': 'ALIGNMENT_DELIVERY_CHECK_MISSING',
        }.get(schema_error, 'ALIGNMENT_REQUEST_FRAME_MISSING' if schema_error and
              schema_error.startswith('request_frame_required:') else
              'ALIGNMENT_RESPONSE_SCHEMA_INVALID' if schema_error else 'ALIGNMENT_EVIDENCE_VALIDATION_FAILED')
        if not schema_error and isinstance(validation_details, dict):
            self.code = {
                'RECEIVED_ANSWER_NOT_CONSUMED': 'ALIGNMENT_ANSWER_NOT_INTERPRETED',
                'DEFAULT_REPLACES_FOUNDATIONAL_INTENT': 'ALIGNMENT_FOUNDATIONAL_DEFAULT_FORBIDDEN',
                'QUESTION_RETIREMENT_INVALID': 'ALIGNMENT_QUESTION_RETIREMENT_INVALID',
                'QUESTION_FRONTIER_SELECTION_MISSING': 'ALIGNMENT_QUESTION_SELECTION_MISSING',
                'QUESTION_FRONTIER_SELECTION_BLOCKED': 'ALIGNMENT_QUESTION_SELECTION_BLOCKED',
                'QUESTION_PROPOSAL_SELECTION_MISSING': 'ALIGNMENT_PROPOSAL_SELECTION_MISSING',
                'QUESTION_PROPOSAL_SELECTION_AMBIGUOUS': 'ALIGNMENT_PROPOSAL_SELECTION_AMBIGUOUS',
                'QUESTION_PROPOSAL_INDEX_INVALID': 'ALIGNMENT_PROPOSAL_INDEX_INVALID',
            }.get(validation_details.get('code'), self.code)


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

    def check(self, instruction, candidate="", proposed_questions=None, *, trigger='admission', proposed_contracts=None, proposed_files=None, observation_only=False):
        try:
            return self._check(instruction, candidate, proposed_questions, trigger=trigger, proposed_contracts=proposed_contracts, proposed_files=proposed_files, observation_only=observation_only)
        except Exception as exc:
            return self.failure(exc)

    def _check(self, instruction, candidate, proposed_questions, *, trigger, proposed_contracts, proposed_files, observation_only):
        process, llm_provider, hooks = self.process, self.provider, self.hooks
        if proposed_questions is not None:
            from .question_surface import question_proposals
            try:
                proposed_questions = question_proposals(process, proposed_questions, require_recommendation=True)
            except (ValueError, TypeError, AttributeError) as invalid:
                # Original tool input must be repaired by its author. An
                # assessor selecting that proposal cannot repair its schema.
                return {"alignment_repair": "Invalid question input: " + str(invalid)
                        + ". Correct the original request_user_decision arguments before proposing it again."}
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
        payload = runtime.payload(instruction, candidate, proposed_questions, proposed_contracts, proposed_files,
            observation_only=not candidate and (proposed_questions is not None or observation_only))
        payload['trigger'] = trigger
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
                         error_type=type(invalid_cache).__name__, phase='requirement_review_reuse',
                         review_state='fresh_review_required', reuse_state='rejected')
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
                    "ALIGNMENT_ASSESSOR_UNAVAILABLE", "当前模型接口未提供语义需求检查；已声明的问题仍受完成门禁约束。",
                    phase='requirement_review_capability', review_state='not_available')
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
                # Retain the exact assessor input through the existing private
                # Trace detail channel, including bounded repair attempts.
                # The event/UI sees identity only, never this whole payload.
                from .alignment_policy import selected_modules
                _emit_observation({'event': 'alignment_assessment_input', 'stage': payload['stage'],
                    'trigger': reason, 'attempt': attempt + 1,
                    'configured_policy_modules':selected_modules(payload)}, detail=payload)
                proposal = assessor(payload, before_attempt=before_attempt,
                                    on_notice=alignment_route_notice,
                                    cancel_event=process.cancellation_event)
                process.steps_used += 1
                usage = proposal.pop("_provider_usage", None) if isinstance(proposal, dict) else None
                cache_data = proposal.pop('_provider_cache_intent', None) if isinstance(proposal, dict) else None
                if usage:
                    from .provider_protocol import CacheIntent
                    protocol = getattr(llm_provider, 'protocol', 'unknown')
                    session.record_provider_usage({**usage, 'purpose': 'alignment'}, protocol=str(getattr(protocol, 'value', protocol)),
                        cache_intent=CacheIntent(**cache_data) if cache_data else None)
                    _emit_observation({"event": "alignment_provider_usage", "purpose": "alignment", "stage": payload["stage"],
                        "usage": {k: v for k, v in usage.items() if k != "raw"},
                        "cache_intent": cache_data,
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
                        # A structurally returned but invalid semantic proposal
                        # rejects this tool batch. Let its initiating Agent
                        # repair it through the existing bounded proposal loop;
                        # do not turn an assessor's bad question identity into
                        # a terminal failure of the user's whole task.
                        # Missing/incomplete Provider output remains a review
                        # failure, never implicit readiness or another retry.
                        if (isinstance(invalid, ValueError) and not proposal.get('schema_error')
                                and (proposed_questions is not None or proposed_contracts is not None)):
                            diagnostic = InvalidAlignmentAssessment(str(invalid),
                                validation_details=getattr(invalid, 'validation_details', None))
                            failure = {'code':diagnostic.code, 'reason':'assessment_invalid',
                                'stage':payload['stage'], 'execution_state':'review_not_applied',
                                'effect_scope':'reviewed_calls', 'retryable':False,
                                'validation_details':diagnostic.validation_details,
                                'recovery_class':'correct_source_bound_proposal'}
                            _emit_observation({'event':'alignment_proposal_repair_required',
                                'stage':payload['stage'],'reason':str(invalid)[:400], 'failure':failure})
                            return {'alignment_repair': 'The semantic review did not validate this proposal; no tool in the batch may execute. '
                                + str(invalid)[:400] + '. Inspect the current decision identities and actual answers. '
                                'Correct or withdraw the proposed scope/question using those facts; preserve accepted requirements. '
                                'Do not treat this rejection as readiness, permission or a user answer.', 'failure':failure}
                        raise InvalidAlignmentAssessment(str(invalid), schema_error=proposal.get('schema_error'),
                            validation_details=getattr(invalid, 'validation_details', None)) from invalid
                    # One compiler-style recovery, retaining source inputs and
                    # requirements. Invalid proposals never become user facts.
                    payload = {**payload, "host_validation_error": str(invalid)[:300], "rejected_proposal": proposal,
                               "provider_output_recovery": proposal.get("schema_error") == "provider_response_incomplete"
                               and proposal.get("incomplete_reason") == "max_output_tokens"}
                    if getattr(invalid, 'validation_details', None):
                        payload['host_validation_details'] = invalid.validation_details
            _emit_observation({"event": "alignment_assessment_completed", "stage": payload["stage"],
                               "status": outcome["status"], "duration_ms": (time.monotonic() - assessment_started) * 1000}, detail=proposal)
            hooks.refresh_prompt()
            try:
                remember_unresolved(process, payload, proposal)
            except (ValueError, KeyError, TypeError) as invalid_cache:
                hooks.notice('ALIGNMENT_CACHE_RECORD_FAILED', '本次解释已采用，但未能建立复用记录；后续仍可正常判断。',
                             error_type=type(invalid_cache).__name__, phase='requirement_review_reuse_record',
                             review_state='applied', reuse_state='not_recorded')
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
        from .provider_failure import provider_diagnostic, provider_failure_message
        from .alignment_policy import AlignmentPolicyUnavailable
        if isinstance(exc, AlignmentPolicyUnavailable):
            failure = {'reason':'policy_resource_unavailable','code':'ALIGNMENT_POLICY_UNAVAILABLE',
                'stage':'requirement_review_input','execution_state':'review_request_not_started',
                'effect_scope':'reviewed_candidate','retryable':False,
                'resource':exc.resource,'recovery_class':'restore_bundled_review_policy'}
            message = '需求检查的指令资源不可用。'
        elif isinstance(exc, InvalidAlignmentAssessment):
            failure = {'reason': 'assessment_invalid', 'code': exc.code, 'retryable': False,
                'exception_types': [type(exc).__name__], 'stage': 'requirement_review',
                'execution_state': 'review_not_applied', 'effect_scope': 'reviewed_candidate',
                'recovery_class': 'inspect_assessment_bindings', 'validation_message': str(exc)[:400],
                'schema_error': exc.schema_error, 'validation_details': exc.validation_details}
            message = {
                'ALIGNMENT_RESPONSE_JSON_INVALID': '需求检查返回的 JSON 无效。',
                'ALIGNMENT_RESPONSE_SCHEMA_INVALID': '需求检查返回的数据结构无效。',
                'ALIGNMENT_RESPONSE_INCOMPLETE': '需求检查的模型输出未完整返回。',
                'ALIGNMENT_DELIVERY_CHECK_MISSING': '需求检查缺少交付检查结果。',
                'ALIGNMENT_REQUEST_FRAME_MISSING': '需求检查缺少有效的当前请求说明。',
                'ALIGNMENT_ANSWER_NOT_INTERPRETED': '需求检查尚未解释已收到的真实答复。',
                'ALIGNMENT_FOUNDATIONAL_DEFAULT_FORBIDDEN': '模型默认值不能替代仍待用户决定的要求。',
                'ALIGNMENT_QUESTION_RETIREMENT_INVALID': '问题撤回缺少有效依据或超出适用范围。',
                'ALIGNMENT_QUESTION_SELECTION_MISSING': '本轮尚未明确选择要发布的问题。',
                'ALIGNMENT_QUESTION_SELECTION_BLOCKED': '本轮所选问题的前置条件尚未满足。',
                'ALIGNMENT_PROPOSAL_SELECTION_MISSING': '需求检查未明确引用原提问，也未给出完整的新问题。',
                'ALIGNMENT_PROPOSAL_SELECTION_AMBIGUOUS': '需求检查引用了多个可能的提问，需要明确选择。',
                'ALIGNMENT_PROPOSAL_INDEX_INVALID': '需求检查引用的提问位置无效。',
            }.get(exc.code, '需求检查的解释未通过真实答复或约束校验。')
        else:
            failure = provider_diagnostic(exc, stage="requirement_review")
            message = provider_failure_message(failure)
        from .provider_failure import expired_task_budget
        deadline = expired_task_budget(process, failure)
        if deadline is not None:
            hooks.emit({'event':'alignment_assessment_deadline_exceeded','failure':failure})
            return hooks.budget_exhausted(deadline)
        hooks.notice('ALIGNMENT_REVIEW_FAILED', message + ' 待审调用尚未执行，原任务和真实答复已保留。',
                     error_type=type(exc).__name__, failure=failure)
        hooks.emit({'event': 'alignment_assessment_failed', 'error_type': type(exc).__name__, 'failure': failure},
                   detail={'failure': failure})
        transition(process, ProcessStatus.FAILED)
        return hooks.result(message + ' 待审调用尚未执行，任务未标记为完成。',
            error_code='ALIGNMENT_REVIEW_FAILED', error_message=message,
            error_details={'failure': failure}, error_retryable=failure['retryable'],
            outcome_status=OutcomeStatus.FAILED)
