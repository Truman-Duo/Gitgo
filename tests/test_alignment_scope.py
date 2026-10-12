import copy
import json

import pytest

from backend.core.loop.alignment_runtime import AlignmentRuntime, assess_with_provider
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession


def setup(request, root):
    process = AgentProcess(process_id='p', role='supervisor', ring_level=RingLevel.RING_3,
                           active_task_id='t', actor_kind='supervisor', session=AgentSession(),
                           task_description=request, worktree_path=str(root))
    runtime = AlignmentRuntime(process)
    return process, runtime, runtime.payload(request)


def proposal(kind, quote, questions=None):
    return {'status': 'needs_input' if questions else 'ready', 'interpretations': [],
            'questions': questions or [], 'request_frame': {
                'kind': kind, 'source_quote': quote, 'outcome': 'Respond to the current request'}}


def question(quote):
    return {'state_topic': 'report_subject', 'question': '报告的主题是什么？',
            'why_user_must_decide': 'Different subjects require different evidence and conclusions',
            'tier': 'foundational', 'depends_on_topics': [], 'necessity': {
                'source_quote': quote, 'outcome_component': 'Requested report subject and evidence',
                'answer_changes': ['Product report compares alternatives', 'Project report analyses progress']}}


@pytest.mark.parametrize('prompt', ['你好', '谢谢', '只回答一句：你好', 'Good morning!', '收到，辛苦了'])
def test_conversational_frame_rejects_interview_atomically(prompt, tmp_path_factory):
    process, runtime, payload = setup(prompt, tmp_path_factory)
    invalid = proposal('conversation', prompt, [question(prompt)])
    before = copy.deepcopy(process.context_snapshot), copy.deepcopy(process.session.host_ledger)
    with pytest.raises(ValueError, match='absence of a future task'):
        runtime.apply(invalid, payload)
    assert (process.context_snapshot, process.session.host_ledger) == before
    assert process.pending_decision is None
    assert runtime.apply(proposal('conversation', prompt), payload)['status'] == 'ready'
    assert process.context_snapshot['alignment_runtime']['request_frame']['kind'] == 'conversation'


def test_needed_input_still_publishes_real_requested_report_question(tmp_path_factory):
    process, runtime, payload = setup('请帮我撰写综合报告。', tmp_path_factory)
    result = runtime.apply(proposal('deliverable', payload['request'], [question(payload['request'])]), payload)
    assert result['status'] == 'needs_input'
    assert len(process.pending_decision['questions']) == 1
    assert process.pending_decision['questions'][0]['question'] == '报告的主题是什么？'


@pytest.mark.parametrize('change', ['missing', 'candidate_source', 'same_outcome'])
def test_question_requires_real_outcome_dependency_before_publication(change, tmp_path_factory):
    process, runtime, payload = setup('请帮我撰写综合报告。', tmp_path_factory)
    q = question(payload['request'])
    if change == 'missing':
        q.pop('necessity')
    elif change == 'candidate_source':
        payload['candidate'] = 'Hypothetical new future task'
        q['necessity']['source_quote'] = payload['candidate']
    else:
        q['necessity']['answer_changes'] = ['Same outcome', 'Same outcome']
    before = copy.deepcopy(process.context_snapshot), copy.deepcopy(process.session.host_ledger)
    with pytest.raises(ValueError, match='necessity'):
        runtime.apply(proposal('deliverable', payload['request'], [q]), payload)
    assert (process.context_snapshot, process.session.host_ledger) == before
    assert process.pending_decision is None


def test_production_provider_cannot_omit_frame_and_usage_is_preserved():
    class Provider:
        def chat(self, *args, **kwargs):
            return {'content': json.dumps({'status': 'needs_input', 'questions': [], 'interpretations': []}),
                    'usage': {'input_tokens': 17, 'output_tokens': 12}}
    result = assess_with_provider(Provider(), {'request': '你好', 'stage': 'admission'})
    assert result['status'] == 'invalid_request_frame'
    assert 'request_frame_required' in result['schema_error']
    assert result['_provider_usage']['input_tokens'] == 17
