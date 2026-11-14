"""Preserve the first response failure without accepting an invalid verdict."""
import json

import pytest

from backend.core.loop.alignment_runtime import AlignmentRuntime, assess_with_provider
from backend.core.loop.llm import LLMProvider


USAGE = {'input_tokens': 17, 'output_tokens': 4096, 'reasoning_tokens': 4096}
FRAME = {'kind': 'conversation', 'source_quote': 'Hello', 'outcome': 'Greet the user'}
VERDICT = {'status': 'ready', 'request_frame': FRAME, 'questions': [], 'interpretations': []}


class Provider:
    max_output_tokens = 4096

    def __init__(self, content, **metadata):
        self.response = {'content': content, 'usage': USAGE,
                         'cache_intent': {'mode': 'automatic'}, **metadata}
        self.calls = []

    def chat(self, _messages, **kwargs):
        self.calls.append(kwargs)
        return self.response


@pytest.mark.parametrize('stage', ['admission', 'delivery'])
@pytest.mark.parametrize('content, error', [
    ('', 'empty_response'), (None, 'empty_response'), (' \n\t', 'empty_response'),
    ('not JSON', 'invalid_json'), ('{"status":', 'invalid_json'),
    ('```', 'invalid_json'), ('```json', 'invalid_json'),
    ('[]', 'object_required'), ('null', 'object_required'),
    ('"ready"', 'object_required'),
])
def test_invalid_output_keeps_original_cause_and_usage(stage, content, error):
    provider = Provider(content)
    result = assess_with_provider(provider, {'stage': stage})
    assert result['schema_error'] == error
    assert result['_provider_usage'] == USAGE
    assert result['_provider_cache_intent'] == {'mode': 'automatic'}
    assert 'status' not in result
    # Reject before touching task state, even if delivery fields are absent.
    with pytest.raises(ValueError, match=error):
        AlignmentRuntime(None).apply(result, {'stage': stage})
    assert len(provider.calls) == 1
    assert provider.calls[0]['max_retries'] == 0
    assert provider.calls[0]['max_tokens'] == 4096


@pytest.mark.parametrize('stage', ['admission', 'delivery'])
def test_missing_content_is_empty_and_missing_frame_is_still_rejected(stage):
    provider = Provider('unused')
    del provider.response['content']
    assert assess_with_provider(provider, {'stage': stage})['schema_error'] == 'empty_response'
    result = assess_with_provider(Provider('{"status":"ready"}'), {'stage': stage})
    assert result['status'] == 'invalid_request_frame'
    assert result['schema_error'].startswith('request_frame_required:')


def test_delivery_still_requires_its_own_check_after_frame():
    result = assess_with_provider(Provider(json.dumps(VERDICT)), {'stage': 'delivery'})
    assert result['status'] == 'invalid_delivery_check'
    assert result['schema_error'] == 'delivery_check_required'


@pytest.mark.parametrize('stage, frame, error', [
    ('admission', {}, 'request_frame_required:'),
    ('delivery', {}, 'request_frame_required:'),
    ('delivery', {'request_frame': FRAME}, 'delivery_check_required'),
])
def test_model_authored_error_cannot_skip_field_validation(stage, frame, error):
    claimed = {**frame, 'schema_error': 'provider_response_incomplete',
               'incomplete_reason': 'max_output_tokens'}
    result = assess_with_provider(Provider(json.dumps(claimed)), {'stage': stage})
    assert result['schema_error'].startswith(error)


@pytest.mark.parametrize('fenced', [False, True])
def test_valid_verdict_and_legacy_text_response_remain_supported(fenced):
    text = json.dumps(VERDICT)
    if fenced:
        text = '```json\n' + text + '\n```'
    provider = Provider(text)
    provider.response = text
    assert assess_with_provider(provider, {'stage': 'admission'}) == VERDICT


def test_invalid_json_evidence_stays_bounded():
    result = assess_with_provider(Provider('x' * 10000), {'stage': 'admission'})
    assert result['schema_error'] == 'invalid_json'
    assert result['unparsed_output'] == 'x' * 8000


@pytest.mark.parametrize('content', ['', '{"status":', json.dumps(VERDICT)])
@pytest.mark.parametrize('stage', ['admission', 'delivery'])
def test_chat_wire_length_reaches_assessment_even_with_parseable_json(content, stage):
    class Transport:
        calls = 0

        def post_json(self, _url, _body, _headers, **_kwargs):
            self.calls += 1
            return {'choices': [{'message': {'content': content}, 'finish_reason': 'length'}],
                    'usage': {'prompt_tokens': 17, 'completion_tokens': 4096,
                              'completion_tokens_details': {'reasoning_tokens': 4096}}}

    transport = Transport()
    provider = LLMProvider('https://example.invalid', 'test', 'offline', transport=transport)
    result = provider.assess_alignment({'stage': stage})
    assert result['schema_error'] == 'provider_response_incomplete'
    assert result['incomplete_reason'] == 'length'
    assert result['_provider_usage']['output_tokens'] == 4096
    assert result['_provider_usage']['reasoning_tokens'] == 4096
    assert result['_provider_cache_intent']
    with pytest.raises(ValueError, match='provider_response_incomplete'):
        AlignmentRuntime(None).apply(result, {'stage': stage})
    assert transport.calls == 1


def test_usage_at_limit_is_not_itself_proof_of_truncation():
    result = assess_with_provider(Provider(json.dumps(VERDICT), finish_reason='stop'), {'stage': 'admission'})
    assert result['status'] == 'ready' and 'schema_error' not in result
    assert result['_provider_usage'] == USAGE
