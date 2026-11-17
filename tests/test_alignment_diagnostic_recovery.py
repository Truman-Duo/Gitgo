"""Exercise assessor diagnostics through real review, recovery and accounting.

Only chat transport is a deterministic fake; no provider/API is contacted.
"""
from copy import deepcopy
import json

import pytest

from backend.core.loop.alignment_payload import decode
from backend.core.loop.alignment_review import AlignmentReview, AlignmentReviewHooks
from backend.core.loop.alignment_runtime import AlignmentRuntime, assess_with_provider
from backend.core.loop.budget import TaskTreeBudget
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession


REQUEST = 'Compare the supplied totals using the confirmed unit.'
ANSWER = 'Keep amounts in cents; do not change the original data.'


class FakeChatProvider:
    max_output_tokens = 32000
    protocol = 'openai_chat'

    def __init__(self, contents):
        self.contents = contents
        self.calls = []

    def assess_alignment(self, payload, **kwargs):
        return assess_with_provider(self, payload, **kwargs)

    def chat(self, messages, **kwargs):
        index = len(self.calls)
        self.calls.append({'payload': decode(messages[-1]['content']), 'options': kwargs})
        assert index < len(self.contents), 'unexpected additional provider call'
        response = self.contents[index]
        response = deepcopy(response) if isinstance(response, dict) else {'content': response}
        return {**response, 'usage': {
            'input_tokens': 101 + index, 'output_tokens': 11 + index,
            'reasoning_tokens': 5 + index, 'cache_read_tokens': 7 + index}}


def _ready():
    return json.dumps({'status': 'ready', 'interpretations': [], 'questions': [],
                      'request_frame': {'kind': 'deliverable', 'source_quote': REQUEST,
                                        'outcome': 'Compare supplied totals in cents'}})


def _review(root, contents):
    process = AgentProcess(process_id='diagnostic-owner', role='supervisor',
        actor_kind='supervisor', ring_level=RingLevel.RING_3, active_task_id='diagnostic-task',
        session=AgentSession(), task_description=REQUEST, worktree_path=str(root),
        task_budget=TaskTreeBudget.create('diagnostic-task'))
    process.session.messages.append({'role': 'user', 'content': ANSWER, 'message_type': 'conversation'})
    workflow = EngineeringWorkflow(process)
    question = workflow.ask_semantic({'kind': 'clarification', 'question': 'Which unit should be used?',
        'state_topic': 'amount-unit', 'why_user_must_decide': 'Determines the numeric interpretation',
        'options': []})
    process.session.host_ledger.append({'event': 'user_decision_received',
        'task_id': process.active_task_id, 'decision_id': question['decision_id'],
        'state_topic': 'amount-unit', 'answer': ANSWER})
    process.pending_decision = None
    node = workflow.status()['nodes'][0]['id']
    workflow.resolve_answer(node, question['decision_id'], {
        'disposition': 'confirmed', 'value': ANSWER, 'quote': ANSWER})
    payload = AlignmentRuntime(process).payload(REQUEST)
    assert payload['confirmed_answers']
    preserved = {'messages': deepcopy(process.session.messages),
                 'ledger': deepcopy(process.session.host_ledger),
                 'confirmed_answers': deepcopy(payload['confirmed_answers'])}
    events, admissions = [], []
    def before_attempt(attempt, provider):
        admissions.append(attempt)
        process.task_budget.begin_provider_call(process.process_id)
        return 30
    def emit(event, **details):
        events.append({**deepcopy(event), **deepcopy(details)})
    noop = lambda *args, **kwargs: None
    hooks = AlignmentReviewHooks(emit=emit, frontend_emit=None, notice=noop, refresh_prompt=noop,
        before_attempt=before_attempt, route_notice=noop,
        result=lambda response, **kwargs: {'response': response, **kwargs},
        cancellation=noop, budget_exhausted=noop)
    provider = FakeChatProvider(contents)
    result = AlignmentReview(process, provider, hooks).check(
        REQUEST, trigger='contract', proposed_contracts=[{'goal': REQUEST}])
    return process, provider, result, events, admissions, preserved


def _assert_accounted_and_preserved(process, provider, admissions, preserved):
    count = len(provider.calls)
    assert len(admissions) == count
    assert process.steps_used == count
    assert all(call['options']['max_retries'] == 0 for call in provider.calls)
    assert all(call['options']['preserve_provider_state'] is True for call in provider.calls)
    assert all(call['options']['max_tokens'] == 8192 for call in provider.calls)
    usages = process.session.provider_usage
    assert len(usages) == count
    assert all(usage['purpose'] == 'alignment' for usage in usages)
    assert [usage['reasoning_tokens'] for usage in usages] == list(range(5, 5 + count))
    used = process.task_budget.snapshot()['used']
    assert used['provider_calls'] == count
    assert used['reported_input_tokens'] == sum(range(101, 101 + count))
    assert used['reported_output_tokens'] == sum(range(11, 11 + count))
    assert used['reported_cache_read_tokens'] == sum(range(7, 7 + count))
    assert process.task_description == REQUEST
    assert process.session.messages == preserved['messages']
    assert process.session.host_ledger[:len(preserved['ledger'])] == preserved['ledger']
    assert AlignmentRuntime(process).payload(REQUEST)['confirmed_answers'] == preserved['confirmed_answers']
    assert process.pending_decision is None


@pytest.mark.parametrize(('malformed', 'reason'), [('', 'empty_response'), ('{not valid json', 'invalid_json')], ids=['empty', 'invalid-json'])
def test_parser_failure_remains_the_recovery_reason_and_two_failures_do_not_complete(tmp_path_factory, malformed, reason):
    process, provider, result, events, admissions, preserved = _review(
        tmp_path_factory, [malformed, malformed])
    assert len(provider.calls) == 2
    recovered = provider.calls[1]['payload']
    assert recovered['host_validation_error'] == reason
    assert recovered['rejected_proposal']['schema_error'] == reason
    if malformed:
        assert recovered['rejected_proposal']['unparsed_output'] == malformed
    assert recovered['provider_output_recovery'] is False
    assert result['error_code'] == 'ALIGNMENT_REVIEW_FAILED'
    assert process.status.value == 'failed'
    assert [event['reason'] for event in events if event['event'] == 'alignment_assessment_rejected'] == [reason] * 2
    assert not any(event['event'] in {'alignment_assessment_completed', 'alignment_proposal_repair_required'} for event in events)
    _assert_accounted_and_preserved(process, provider, admissions, preserved)


@pytest.mark.parametrize(('malformed', 'reason'), [('', 'empty_response'), ('{not valid json', 'invalid_json')], ids=['empty', 'invalid-json'])
def test_parser_failure_can_recover_once_to_a_valid_assessment(tmp_path_factory, malformed, reason):
    process, provider, result, events, admissions, preserved = _review(
        tmp_path_factory, [malformed, _ready()])
    assert len(provider.calls) == 2
    recovered = provider.calls[1]['payload']
    assert recovered['host_validation_error'] == reason
    assert recovered['rejected_proposal']['schema_error'] == reason
    assert recovered['provider_output_recovery'] is False
    assert result is None
    assert process.status.value not in {'failed', 'completed'}  # Ready review does not deliver the task.
    assert [event['status'] for event in events if event['event'] == 'alignment_assessment_completed'] == ['ready']
    _assert_accounted_and_preserved(process, provider, admissions, preserved)


def test_valid_first_assessment_adds_no_recovery_call(tmp_path_factory):
    process, provider, result, events, admissions, preserved = _review(tmp_path_factory, [_ready()])
    assert result is None
    assert len(provider.calls) == 1
    assert 'rejected_proposal' not in provider.calls[0]['payload']
    assert not any(event['event'] == 'alignment_assessment_rejected' for event in events)
    _assert_accounted_and_preserved(process, provider, admissions, preserved)


def test_length_stop_keeps_its_reason_without_expanding_existing_recovery_budget(tmp_path_factory):
    truncated = {'content': '{"status":', 'finish_reason': 'length'}
    process, provider, result, events, admissions, preserved = _review(
        tmp_path_factory, [truncated, truncated])
    assert provider.max_output_tokens > 16384
    assert len(provider.calls) == 2
    recovered = provider.calls[1]['payload']
    assert recovered['host_validation_error'] == 'provider_response_incomplete'
    assert recovered['rejected_proposal']['schema_error'] == 'provider_response_incomplete'
    assert recovered['rejected_proposal']['incomplete_reason'] == 'length'
    assert recovered['provider_output_recovery'] is False
    assert result['error_code'] == 'ALIGNMENT_REVIEW_FAILED'
    assert process.status.value == 'failed'
    assert not any(event['event'] == 'alignment_assessment_completed' for event in events)
    _assert_accounted_and_preserved(process, provider, admissions, preserved)


def test_model_authored_error_fields_cannot_claim_transport_recovery_budget(tmp_path_factory):
    claimed_error = json.dumps({'schema_error': 'provider_response_incomplete',
                                'incomplete_reason': 'max_output_tokens'})
    process, provider, result, events, admissions, preserved = _review(
        tmp_path_factory, [claimed_error, claimed_error])
    assert provider.max_output_tokens > 16384
    assert len(provider.calls) == 2
    recovered = provider.calls[1]['payload']
    assert recovered['host_validation_error'].startswith('request_frame_required:')
    assert recovered['rejected_proposal']['schema_error'].startswith('request_frame_required:')
    assert recovered['provider_output_recovery'] is False
    assert result['error_code'] == 'ALIGNMENT_REVIEW_FAILED'
    assert process.status.value == 'failed'
    assert not any(event['event'] == 'alignment_assessment_completed' for event in events)
    _assert_accounted_and_preserved(process, provider, admissions, preserved)
