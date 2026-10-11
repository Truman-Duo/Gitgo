"""Wire-level workload preferences, explicit fallback and paid truncation."""
import json

import pytest

from backend.core.loop.llm import LLMProvider
from backend.core.loop.provider_adapters import OpenAIResponsesAdapter
from backend.core.loop.provider_protocol import ProviderResponseIncomplete
from backend.core.loop.provider_transport import ProviderHttpError


class Transport:
    def __init__(self, rejection=None):
        self.bodies, self.rejection = [], rejection

    def post_json(self, _url, body, _headers, **_kwargs):
        self.bodies.append(body)
        if self.rejection and len(self.bodies) == 1:
            raise self.rejection
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': 'pong'}]}]}


def provider(transport):
    return LLMProvider('https://example.invalid/v1', 'test', 'arbitrary-model',
                       protocol='openai_responses', transport=transport)


def test_alignment_preference_does_not_change_main_generation():
    transport = Transport()
    client = provider(transport)
    client.chat([], max_retries=0, purpose='alignment')
    client.chat([], max_retries=0)
    assert transport.bodies[0]['reasoning'] == {'effort': 'low'}
    assert 'reasoning' not in transport.bodies[1]


def test_optional_parameter_rejection_notifies_and_accounts_one_fallback():
    transport = Transport(ProviderHttpError(400, json.dumps({'error': {
        'param': 'reasoning.effort', 'code': 'unsupported_value'}}), {}))
    client = provider(transport)
    notices, budget_attempts = [], []
    assert client.chat([], max_retries=0, purpose='alignment', on_notice=notices.append,
        before_generation_fallback=lambda: budget_attempts.append('fallback') or 30) == 'pong'
    assert len(transport.bodies) == 2
    assert 'reasoning' not in transport.bodies[1]
    assert len(notices) == 1 and notices[0]['event'] == 'generation_profile_fallback'
    assert budget_attempts == ['fallback']
    client.chat([], max_retries=0, purpose='alignment')
    assert 'reasoning' not in transport.bodies[2]


@pytest.mark.parametrize('status, detail', [
    (401, {'param': 'reasoning', 'code': 'unsupported_parameter'}),
    (429, {'param': 'reasoning', 'code': 'unsupported_parameter'}),
    (400, {'param': 'messages', 'code': 'unsupported_parameter'}),
    (400, {'param': 'reasoning', 'code': 'invalid_request_error', 'message': 'bad request'}),
])
def test_unrelated_errors_do_not_negotiate_or_hide_failure(status, detail):
    transport = Transport(ProviderHttpError(status, json.dumps({'error': detail}), {}))
    with pytest.raises(RuntimeError):
        provider(transport).chat([], max_retries=0, purpose='alignment')
    assert len(transport.bodies) == 1


def test_truncated_response_retains_paid_usage_without_adopting_partial_output():
    with pytest.raises(ProviderResponseIncomplete) as captured:
        OpenAIResponsesAdapter().parse_response({'status': 'incomplete',
            'incomplete_details': {'reason': 'max_output_tokens'},
            'usage': {'input_tokens': 123, 'output_tokens': 8192,
                      'output_tokens_details': {'reasoning_tokens': 8000}},
            'output': [{'type': 'function_call', 'name': 'write_file', 'arguments': '{}'}]})
    assert captured.value.usage['output_tokens'] == 8192
    assert captured.value.usage['reasoning_tokens'] == 8000


def test_budget_exhaustion_in_fallback_is_not_reclassified_as_transport_failure():
    from backend.core.loop.budget import TaskBudgetExceeded
    transport = Transport(ProviderHttpError(400, json.dumps({'error': {
        'param': 'reasoning.effort', 'code': 'unsupported_value'}}), {}))
    exhausted = TaskBudgetExceeded('CALL_LIMIT', 'No request allowance remains')
    def deny():
        raise exhausted
    with pytest.raises(TaskBudgetExceeded) as captured:
        provider(transport).chat([], purpose='alignment', before_generation_fallback=deny)
    assert captured.value is exhausted
    assert len(transport.bodies) == 1
