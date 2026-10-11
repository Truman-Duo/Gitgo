import copy
import pytest
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.alignment_runtime import AlignmentRuntime
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.answer_review_cache import remember_unresolved, reuse_unresolved
from backend.core.loop.completion_protocol import HostCompletionEvaluator


def setup(root):
    process = AgentProcess(process_id='cache', role='supervisor', actor_kind='supervisor',
        ring_level=RingLevel.RING_3, active_task_id='task', task_kind='answer',
        task_description='Plan a trip', worktree_path=str(root), session=AgentSession())
    runtime = AlignmentRuntime(process)
    runtime.payload('Plan a trip')
    question = {'kind': 'clarification', 'state_topic': 'dates', 'question': 'Which dates?',
                'why_user_must_decide': 'Season changes the itinerary', 'options': []}
    first = EngineeringWorkflow(process).ask_semantic(question)
    return process, runtime, question, first


def receive(process, request, text='Japan with family'):
    received = {**request, 'event': 'user_decision_received', 'answer': text}
    if request.get('questions'):
        received['answers'] = {q['node_id']: {'answer': text, 'state_topic': q['state_topic']}
                               for q in request['questions']}
    process.session.host_ledger.append(received)
    process.pending_decision = None


def cached_partial(root, *, revised_question=None):
    process, runtime, question, first = setup(root)
    receive(process, first)
    payload = runtime.payload()
    node = payload['actual_answers'][0]['node_id']
    proposal = {'status': 'needs_input', 'questions': [{**question, **(revised_question or {})}], 'interpretations': [{
        'node_id': node, 'decision_id': first['decision_id'], 'disposition': 'partial',
        'value': 'Japan with family', 'quote': 'Japan with family', 'remaining': 'Dates'}]}
    second = runtime.apply(proposal, payload)['decision']
    remember_unresolved(process, payload, proposal)
    return process, runtime, second, node


def test_identical_partial_reply_reuses_validated_interpretation_but_cannot_complete(tmp_path_factory):
    process, runtime, second, node = cached_partial(tmp_path_factory)
    receive(process, second)
    result = reuse_unresolved(process, runtime.payload(), runtime)
    assert result is not None
    assert result['status'] == 'needs_input'
    assert result['decision']['decision_id'] != second['decision_id']
    assert len(result['decision']['questions']) == 1
    assert not EngineeringWorkflow(process).status()['complete']
    assert not HostCompletionEvaluator.evaluate(process, 'Complete itinerary').allowed
    latest = next(e for e in reversed(process.session.host_ledger) if e['event'] == 'user_decision_interpreted')
    assert latest['decision_id'] == second['decision_id']
    assert latest['node_id'] == node and latest['disposition'] == 'partial'


def test_reuse_binds_the_published_residual_question_after_validated_rewording(tmp_path_factory):
    process, runtime, second, _node = cached_partial(tmp_path_factory,
        revised_question={'question': 'What departure and return dates?', 'why_user_must_decide': 'Season and duration determine itinerary'})
    receive(process, second)
    result = reuse_unresolved(process, runtime.payload(), runtime)
    assert result is not None
    assert result['decision']['questions'][0]['question'] == second['questions'][0]['question']
    assert not HostCompletionEvaluator.evaluate(process, 'Complete itinerary').allowed


@pytest.mark.parametrize('change', ['answer', 'constraints', 'question', 'receipt', 'steering', 'policy'])
def test_any_meaningful_input_change_misses_cache(tmp_path_factory, change):
    process, runtime, second, _node = cached_partial(tmp_path_factory)
    receive(process, second, 'Decide the dates yourself; do not ask' if change == 'answer' else 'Japan with family')
    if change == 'constraints':
        process.task_constraints.append('Wheelchair accessible')
    elif change == 'question':
        request = next(e for e in process.session.host_ledger if e.get('event') == 'user_decision_requested'
                       and e['decision_id'] == second['decision_id'])
        request['questions'][0]['question'] = 'Which dates and origin?'
    elif change == 'receipt':
        process.tool_receipts.append({'receipt_id': 'new', 'task_id': 'task', 'succeeded': True})
    elif change == 'steering':
        process.session.messages.append({'role': 'user', 'content': 'Change scope',
                                         'message_type': 'conversation', 'mailbox_message_id': 'update'})
    elif change == 'policy':
        process.update_context_snapshot(lambda ctx: {**ctx, 'brief': 'Different current policy'})
    assert reuse_unresolved(process, runtime.payload(), runtime) is None


def test_cache_without_original_interpretation_is_not_evidence(tmp_path_factory):
    process, runtime, second, _node = cached_partial(tmp_path_factory)
    receive(process, second)
    process.session.host_ledger = [e for e in process.session.host_ledger if e['event'] != 'user_decision_interpreted']
    before = copy.deepcopy(process.context_snapshot)
    assert reuse_unresolved(process, runtime.payload(), runtime) is None
    assert process.context_snapshot == before


def test_cache_without_original_user_receipt_is_not_evidence(tmp_path_factory):
    process, runtime, second, _node = cached_partial(tmp_path_factory)
    receive(process, second)
    process.session.host_ledger = [e for e in process.session.host_ledger
        if not (e['event'] == 'user_decision_received' and e['decision_id'] != second['decision_id'])]
    assert reuse_unresolved(process, runtime.payload(), runtime) is None


def test_malformed_cached_proposal_reports_validation_error(tmp_path_factory):
    process, runtime, second, _node = cached_partial(tmp_path_factory)
    receive(process, second)
    cache = next(e for e in process.session.host_ledger if e['event'] == 'alignment_unresolved_cached')
    cache['proposal']['interpretations'] = []
    with pytest.raises(ValueError, match='invalid unresolved'):
        reuse_unresolved(process, runtime.payload(), runtime)
