"""Provider turn requests, transcript commit and bounded recovery.

Routes and context authority remain explicit runtime hooks. Partial calls are
never executable; paid attempts retain usage and cancellation/budget identity.
"""
from dataclasses import dataclass
import json
import time

from .answer_continuation import clear_answer_fragments, pending_answer_text, remember_answer_fragment
from .context_window import ContextWindow
from .lifecycle import transition
from .budget import TaskBudgetExceeded
from .llm import StreamCancelledError, StreamInterruptedError
from .llm_adapter import parse_tool_calls
from .models import ProcessStatus
from .outcome import OutcomeStatus
from .provider_protocol import ProviderEventType, ProviderResponseIncomplete
from .provider_response import ProviderResponse, ProviderResponseCollector
from .provider_turn_protocol import (_iter_provider_events, _provider_idle_timeout,
    _provider_continuation, _filter_incomplete_response_artifacts,
    _stream_recovery_message, _bounded_exception_chain)
from .runtime_outcomes import _make_result, _budget_exhausted_result, _cancellation_result

MAX_STREAM_RECOVERIES = 1

@dataclass(frozen=True)
class ProviderTurnBoundary:
    result: dict | None = None
    retry: bool = False


def _to_provider_tool_calls(tool_calls: list[dict]) -> list[dict]:
    result = []
    for index, call in enumerate(tool_calls):
        result.append(
            {
                'id': str(call.get('id') or f'call_{index}'),
                'type': 'function',
                'function': {
                    'name': str(call.get('name', '')),
                    'arguments': json.dumps(call.get('args', {}), ensure_ascii=False, separators=(',', ':')),
                },
            },
        )
    return result

class ProviderTurnRuntime:
    def __init__(self, process, session, provider, *, emit, frontend_emit,
                 route_changed, route, envelope, compact):
        self.process, self.session, self.provider = process, session, provider
        self.emit, self.frontend_emit = emit, frontend_emit
        self.route_changed, self.route, self.envelope = route_changed, route, envelope
        self.compact = compact
        self.stream_recoveries = 0

    def before_attempt(self, _index, _client):
        if self.process.task_budget is not None:
            self.process.task_budget.begin_provider_call(self.process.process_id)
        return _provider_idle_timeout(self.process)

    def route_notice(self, artifact):
        self.route_changed(artifact)
        if artifact.get('event') == 'provider_request_started':
            self.emit({**self.request_observation, **artifact}, detail={
                'messages': self.session.to_provider_messages(
                    dynamic_envelope=self.envelope(), provider_route=self.route()),
                'tools': self.tools or []})
        else:
            self.emit(artifact, detail=artifact)

    def run(self, tools, web_mode, protocol):
        self.tools, self.web_mode, self.protocol = tools, web_mode, protocol
        self.response = ProviderResponse()
        self.started, self.content, self.duration_ms = time.time(), '', 0
        # Preparation can exhaust a budget before the first request starts.
        self.budget_prefix = 'provider-unstarted:'
        try:
            self.prepare()
            self.collect()
            self.commit_response()
            return ProviderTurnBoundary()
        except Exception as error:
            return self.failure(error)

    def prepare(self):
        budget_call_index = 0
        if self.process.task_budget is not None and (not hasattr(self.provider, 'stream_session_events')):
            budget_call_index = self.process.task_budget.begin_provider_call(self.process.process_id)
        self.budget_prefix = f'provider-{budget_call_index}:'
        self.idle_timeout = _provider_idle_timeout(self.process)
        self.output_limit = max(1, int(getattr(self.provider, 'max_output_tokens', 4096)))
        if self.process.task_kind == 'answer':
            self.output_limit = min(self.output_limit, 8192)
        if self.process.steps_used == 0:
            self.emit(
                {
                    'event': 'progress_summary',
                    'message': 'Understanding the request and preparing the next action.',
                    'phase': 'provider',
                },
            )
        self.cache_intent = self.session.cache_intent(
            self.tools,
            mode=getattr(getattr(self.provider, 'capabilities', None), 'prompt_cache', 'automatic'),
        )
        self.request_observation = {
            'event': 'provider_request_started',
            'task_kind': self.process.task_kind,
            'answer_continuation': bool(pending_answer_text(self.process)),
            'protocol': str(
                getattr(
                    getattr(self.provider, 'protocol', ''),
                    'value',
                    getattr(self.provider, 'protocol', ''),
                ),
            ),
            'model': str(getattr(self.provider, '_model', '')),
            'context_epoch': self.session.context_epoch,
            'message_count': len(self.session.messages),
            'tool_count': len(self.tools or []),
            'idle_timeout_seconds': self.idle_timeout,
            'cache_intent': self.cache_intent.to_dict(),
        }
        if not hasattr(self.provider, 'stream_session_events'):
            self.emit(
                self.request_observation,
                detail={
                    'representation': 'canonical_provider_input',
                    'messages': self.session.to_provider_messages(
                        dynamic_envelope=self.envelope(),
                        provider_route=self.route(),
                    ),
                    'tools': self.tools or [],
                },
            )

    def collect(self):
        self.collector = ProviderResponseCollector(
            self.process,
            self.session,
            self.provider,
            self.emit,
            self.frontend_emit,
            self.cache_intent,
            self.budget_prefix,
            self.response,
        )
        for event in _iter_provider_events(
            self.provider, self.session, tools=self.tools,
            cancel_event=self.process.cancellation_event, cache_intent=self.cache_intent,
            metadata={
                'web_search_mode': self.web_mode,
                'hosted_web_search': bool(
                    self.web_mode in {'auto', 'provider'}
                    and self.protocol in {'openai_responses', 'anthropic_messages'}
                    and getattr(getattr(self.provider, 'capabilities', None), 'hosted_web_search', False)
                ),
            },
            timeout=self.idle_timeout,
            current_turn_envelope=(lambda: self.envelope()) if hasattr(self.provider, 'stream_session_events') else self.envelope(),
            max_tokens=self.output_limit, before_attempt=self.before_attempt,
        ):
            if event.type == ProviderEventType.ROUTING_NOTICE:
                self.route_notice(dict(event.artifact or {}))
                continue
            self.collector.consume(event)

    def commit_response(self):
        self.duration_ms = (time.time() - self.started) * 1000
        if self.process.task_budget is not None:
            self.process.task_budget.finish_output_fragments(
                self.process.process_id,
                prefix=self.budget_prefix,
            )
        self.stream_recoveries = 0
        self.collector.decode_tool_calls()
        self.content = self.response.accumulated_text
        if self.content or self.response.tool_calls or self.response.accumulated_reasoning or self.response.accumulated_reasoning_details:
            replay_artifacts = self.response.provider_artifacts
            if self.response.provider_incomplete_reason or self.response.invalid_tool_calls:
                replay_artifacts = _filter_incomplete_response_artifacts(
                    self.response.provider_artifacts,
                    {str(call.get('id', '')) for call in self.response.tool_calls},
                )
            continuation = _provider_continuation(
                self.response.accumulated_reasoning,
                self.response.accumulated_reasoning_details,
                replay_artifacts,
            )
            self.session.append_assistant_provider(
                self.content,
                tool_calls=_to_provider_tool_calls(self.response.tool_calls),
                continuation_state=continuation,
            )
        self.process.steps_used += 1
        self.emit(
            {
                'event': (
                    'provider_response_incomplete'
                    if self.response.provider_incomplete_reason
                    else 'provider_response_completed'
                ),
                'completed_step': self.process.steps_used,
                'duration_ms': round(self.duration_ms, 3),
                'text_chars': len(self.response.accumulated_text),
                'reasoning_chars': len(self.response.accumulated_reasoning),
                'tool_call_count': len(self.response.tool_calls),
                'reason': self.response.provider_incomplete_reason,
            },
            detail=(
                self.response.provider_incomplete_detail
                if self.response.provider_incomplete_reason
                else None
            ),
        )

    def failure(self, exc):
        if self.process.task_budget is not None:
            self.process.task_budget.finish_output_fragments(
                self.process.process_id,
                prefix=self.budget_prefix,
            )
        from backend.core.loop.budget import TaskBudgetExceeded
        from backend.core.loop.llm import StreamCancelledError, StreamInterruptedError
        if isinstance(exc, TaskBudgetExceeded):
            return ProviderTurnBoundary(
                result=_budget_exhausted_result(
                    self.process,
                    self.session,
                    exc,
                    response=self.response.accumulated_text,
                    duration_ms=(time.time() - self.started) * 1000,
                ),
            )
        if isinstance(exc, StreamCancelledError):
            if self.response.accumulated_text or self.response.accumulated_reasoning or self.response.accumulated_reasoning_details:
                replay_artifacts = _filter_incomplete_response_artifacts(self.response.provider_artifacts, set())
                self.session.append_assistant_provider(
                    self.response.accumulated_text,
                    continuation_state=_provider_continuation(
                        self.response.accumulated_reasoning,
                        self.response.accumulated_reasoning_details,
                        replay_artifacts,
                    ),
                )
            return ProviderTurnBoundary(
                result=_cancellation_result(
                    self.process,
                    self.session,
                    self.response.accumulated_text,
                    duration_ms=(time.time() - self.started) * 1000,
                ),
            )
        if isinstance(exc, StreamInterruptedError):
            if exc.is_context_overflow:
                from backend.core.errors import error_payload
                overflow = error_payload(
                    'CONTEXT_WINDOW_EXCEEDED',
                    details={
                        'configured_limit': self.session.model_context_limit,
                        'estimated_tokens': self.session.estimate_tokens(),
                        'provider_status': exc.status_code,
                    },
                    next_actions=[{'action': 'compact', 'effect': 'Start a new bounded context epoch, then retry.'}],
                )
                self.emit({'event': 'context_window_exceeded', 'error': overflow['error_info']})
                changed, terminal = self.compact('provider_overflow', failure_required=True)
                if terminal is not None:
                    return ProviderTurnBoundary(result=terminal)
                return ProviderTurnBoundary(retry=True)
            if self.response.accumulated_text or self.response.accumulated_reasoning or self.response.accumulated_reasoning_details:
                self.session.append_assistant_provider(
                    self.response.accumulated_text,
                    continuation_state=_provider_continuation(
                        self.response.accumulated_reasoning,
                        self.response.accumulated_reasoning_details,
                        _filter_incomplete_response_artifacts(
                            self.response.provider_artifacts,
                            set(),
                        ),
                    ),
                )
            if self.response.pending_tool_calls:
                clear_answer_fragments(self.process)
            else:
                remember_answer_fragment(self.process, self.response.accumulated_text)
            self.session.append_user(
                _stream_recovery_message(
                    bool(self.response.accumulated_text),
                    bool(self.response.pending_tool_calls),
                ),
                message_type='host_provider_continuation',
            )
            if self.stream_recoveries < MAX_STREAM_RECOVERIES:
                self.stream_recoveries += 1
                if self.frontend_emit:
                    self.frontend_emit(
                        {
                            'event': 'stream_recovery',
                            'process_id': self.process.process_id,
                            'attempt': self.stream_recoveries,
                            'max': MAX_STREAM_RECOVERIES,
                            'diagnostic': _bounded_exception_chain(exc),
                        },
                    )
                return ProviderTurnBoundary(retry=True)
            return self.fallback()
        else:
            transition(self.process, ProcessStatus.FAILED)
            return ProviderTurnBoundary(
                result=_make_result(
                    self.process,
                    self.session,
                    '',
                    duration_ms=0,
                    error_code='LLM_STREAM_FAILED',
                    error_message=str(exc),
                    outcome_status=OutcomeStatus.FAILED,
                ),
            )

    def fallback(self):
        try:
            routed_chat = hasattr(self.provider, 'stream_session_events')
            if self.process.task_budget is not None and (not routed_chat):
                self.process.task_budget.begin_provider_call(self.process.process_id)
            response = self.provider.chat(
                (
                    self.session.to_provider_messages(
                        dynamic_envelope=self.envelope(),
                        provider_route=self.route(),
                    )
                    if hasattr(self.provider, 'stream_events')
                    else self.session.to_openai_messages(dynamic_envelope=self.envelope())
                ),
                tools=self.tools,
                preserve_provider_state=True,
                cancel_event=self.process.cancellation_event,
                timeout=_provider_idle_timeout(self.process),
                max_retries=0,
                **(
                    {
                        'session': self.session,
                        'current_turn_envelope': lambda: self.envelope(),
                        'before_attempt': self.before_attempt,
                        'on_notice': self.route_notice,
                    }
                    if routed_chat
                    else {}
                ),
            )
            self.duration_ms = (time.time() - self.started) * 1000
            if isinstance(response, dict):
                self.content = response.get('content', '') or ''
                self.response.tool_calls = parse_tool_calls(response)
            else:
                self.content = response or ''
                self.response.tool_calls = parse_tool_calls(self.content)
            if isinstance(response, dict) and response.get('usage'):
                self.record_sync_usage(response['usage'])
            if self.process.task_budget is not None:
                self.process.task_budget.consume_output(self.content, self.process.process_id)
            if self.content or self.response.tool_calls:
                continuation = {}
                if isinstance(response, dict) and response.get('reasoning_content'):
                    continuation['reasoning_content'] = response['reasoning_content']
                if isinstance(response, dict) and response.get('reasoning_details'):
                    continuation['reasoning_details'] = response['reasoning_details']
                if isinstance(response, dict) and response.get('provider_artifacts'):
                    continuation.update(
                        _provider_continuation(
                            str(response.get('reasoning_content') or ''),
                            list(response.get('reasoning_details') or []),
                            response['provider_artifacts'],
                        ),
                    )
                self.session.append_assistant_provider(
                    self.content,
                    tool_calls=_to_provider_tool_calls(self.response.tool_calls),
                    continuation_state=continuation,
                )
            self.process.steps_used += 1
            self.stream_recoveries = 0
        except Exception as chat_exc:
            if isinstance(chat_exc, ProviderResponseIncomplete) and chat_exc.usage:
                self.record_sync_usage(chat_exc.usage)
                self.emit({'event': 'provider_response_incomplete', 'phase': 'sync_fallback',
                           'reason': chat_exc.reason})
            if isinstance(chat_exc, TaskBudgetExceeded):
                return ProviderTurnBoundary(
                    result=_budget_exhausted_result(
                        self.process,
                        self.session,
                        chat_exc,
                        response=self.response.accumulated_text,
                        duration_ms=(time.time() - self.started) * 1000,
                    ),
                )
            if isinstance(chat_exc, StreamCancelledError):
                return ProviderTurnBoundary(
                    result=_cancellation_result(
                        self.process,
                        self.session,
                        self.response.accumulated_text,
                        duration_ms=(time.time() - self.started) * 1000,
                    ),
                )
            transition(self.process, ProcessStatus.FAILED)
            return ProviderTurnBoundary(
                result=_make_result(
                    self.process,
                    self.session,
                    '',
                    duration_ms=0,
                    error_code='STREAM_FALLBACK_FAILED',
                    error_message=str(chat_exc),
                    outcome_status=OutcomeStatus.FAILED,
                ),
            )
        return ProviderTurnBoundary()

    def record_sync_usage(self, usage):
        protocol = getattr(self.provider, 'protocol', 'unknown')
        self.session.record_provider_usage(usage, protocol=str(getattr(protocol, 'value', protocol)))
        if self.process.task_budget is not None:
            self.process.task_budget.record_provider_usage(usage)
        self.emit({'event': 'provider_usage', 'phase': 'sync_fallback', 'purpose': 'task',
                   'usage': {key: value for key, value in usage.items() if key != 'raw'}})
