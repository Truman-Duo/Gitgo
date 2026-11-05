"""Predicate generation is bounded and charged without adopting partial output."""
from copy import deepcopy
from time import monotonic

import pytest

from tests.test_mechanism_interactions import runtime
from backend.core.mechanism_runtime.runner import Context
from backend.core.loop.provider_protocol import ProviderResponseIncomplete
from backend.core.loop.budget import TaskBudgetExceeded

USAGE = {'input_tokens': 500, 'cache_read_tokens': 128, 'output_tokens': 4096, 'reasoning_tokens': 4096}


class Provider:
    supports_generation_purpose = True
    max_output_tokens = 16000

    def __init__(self, results):
        self.results, self.calls = list(results), []

    def chat(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), kwargs))
        value = self.results.pop(0)
        if callable(value):
            return value()
        if isinstance(value, Exception):
            raise value
        return value


def setup(runtime, results):
    app, process, *_ = runtime
    provider, events = Provider(results), []
    app.bind_provider(provider, events.append)
    context = Context(app.repo.project_id, app.task, 'predicate', 1, 'event', 'discussion_candidate', 'judge', {}, {},
        lambda: process.cancellation_event.is_set(), monotonic() + 120)
    return app, process, provider, events, context


def test_one_output_recovery_has_identical_evidence_separate_budget_and_usage(runtime):
    valid = {'content': '{"matches":true,"reason":"Actual evidence matches","evidence_refs":["decision-real"]}',
        'usage': {**USAGE, 'output_tokens': 50, 'reasoning_tokens': 20}}
    app, process, provider, events, context = setup(runtime, [ProviderResponseIncomplete('max_output_tokens', USAGE), valid])
    before = process.steps_used
    result = app.judge(context, {'criterion': 'Does this support the question?', 'event': {'decision_id': 'decision-real'}})
    assert result['matches'] and result['evidence_refs'] == ['decision-real'] and not result['authority_granted']
    assert [options['max_tokens'] for _, options in provider.calls] == [4096, 8192]
    assert provider.calls[0][0] == provider.calls[1][0]
    assert all(o['purpose'] == 'mechanism_judgment' and o['max_retries'] == 0 for _, o in provider.calls)
    usage = [e for e in events if e['event'] == 'mechanism_provider_usage']
    assert len(usage) == 2 and len({e['usage_call_id'] for e in usage}) == 2
    assert usage[0]['incomplete'] and not usage[1]['incomplete']
    assert sum(e['usage']['output_tokens'] for e in usage) == 4146
    assert process.steps_used == before + 2 and not process.approval_grants


@pytest.mark.parametrize('ceiling,calls', [(4096, 1), (16000, 2)])
def test_repeated_truncation_does_not_emit_a_false_verdict_or_unbounded_retry(runtime, ceiling, calls):
    app, _, provider, events, context = setup(runtime, [ProviderResponseIncomplete('max_output_tokens', USAGE)] * 2)
    provider.max_output_tokens = ceiling
    with pytest.raises(ProviderResponseIncomplete):
        app.judge(context, {'criterion': 'Actual unresolved criterion', 'event': {}})
    assert len(provider.calls) == calls
    assert len([e for e in events if e['event'] == 'mechanism_provider_usage']) == calls


@pytest.mark.parametrize('stop', ['cancel', 'step_budget'])
def test_generation_recovery_rechecks_cancellation_and_budget(runtime, stop):
    app, process, provider, events, context = setup(runtime, [])
    def truncate():
        if stop == 'cancel':
            process.cancellation_event.set()
        else:
            process.steps_used = process.max_steps
        raise ProviderResponseIncomplete('max_output_tokens', USAGE)
    provider.results = [truncate]
    with pytest.raises((RuntimeError, ValueError)):
        app.judge(context, {'criterion': 'Actual unresolved criterion', 'event': {}})
    assert len(provider.calls) == 1
    assert len([e for e in events if e['event'] == 'mechanism_provider_usage']) == 1


def test_invalid_json_is_not_retried_or_reported_as_zero_cost(runtime):
    app, _, provider, events, context = setup(runtime, [{'content': 'incomplete json'}])
    with pytest.raises(ValueError):
        app.judge(context, {'criterion': 'Actual unresolved criterion', 'event': {}})
    assert len(provider.calls) == 1
    missing = next(e for e in events if e['event'] == 'auxiliary_provider_usage')
    assert missing['usage_reported'] is False and 'usage' not in missing


def test_router_rejection_before_request_is_not_reported_as_unknown_provider_cost(runtime):
    app, process, provider, events, context = setup(runtime, [])
    provider.stream_session_events = True
    reached_request = []
    def route(messages, **options):
        options['before_attempt']()
        reached_request.append(True)
        raise AssertionError('An exhausted step budget must stop before provider access')
    provider.chat = route
    process.steps_used = process.max_steps
    before = process.steps_used
    with pytest.raises(TaskBudgetExceeded, match='step budget'):
        app.judge(context, {'criterion': 'Actual unresolved criterion', 'event': {}})
    assert not reached_request and process.steps_used == before
    assert not [e for e in events if e['event'] in {'mechanism_provider_usage', 'auxiliary_provider_usage'}]
