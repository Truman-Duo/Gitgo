"""Publishing an Agent-selected ready subset does not resolve omitted nodes."""
from copy import deepcopy

import pytest

from backend.core.loop.alignment_runtime import AlignmentRuntime, AlignmentValidationError
from backend.core.loop.alignment_review import InvalidAlignmentAssessment
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.completion_protocol import HostCompletionEvaluator
from tests.test_alignment_semantics import process, pending_reply, interpretation


def question(topic, **extra):
    return {'state_topic': topic, 'question': 'What is the ' + topic + '?',
            'why_user_must_decide': 'Changes the requested result',
            'recommendation': 'Provide the relevant verified detail.', **extra}


def test_partial_old_facet_is_retained_without_forcing_another_question_now(tmp_path_factory):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    runtime.payload('Plan the requested result')
    workflow, card, node = pending_reply(p, 'Known portion only')
    result = runtime.apply({'status': 'needs_input', 'interpretations': [
        interpretation(node, card, 'Known portion only', 'partial', 'Another part remains unknown')],
        'questions': [question('independent_evidence')]}, runtime.payload())
    assert [q['state_topic'] for q in result['decision']['questions']] == ['independent_evidence']
    assert node not in [q['node_id'] for q in result['decision']['questions']]
    state = workflow._load()
    assert node in state['requests'] and node not in state['records']
    assert not workflow.status()['complete']
    assert not HostCompletionEvaluator.evaluate(p, 'All done').allowed
    assert not workflow.guard('write_file', 'workspace_write', {'path': 'result.py'})['allowed']


def test_a_selected_independent_question_does_not_publish_every_old_ready_node(tmp_path_factory):
    p = process(tmp_path_factory)
    workflow = EngineeringWorkflow(p)
    workflow.configure({'profiles': ['alignment'], 'nodes': [
        {'id': 'earlier', 'kind': 'decision', 'state_topic': 'earlier', 'request': question('earlier')}]})
    runtime = AlignmentRuntime(p)
    result = runtime.apply({'status': 'needs_input', 'interpretations': [],
        'questions': [question('chosen')]}, runtime.payload('Plan the requested result'))
    assert [q['state_topic'] for q in result['decision']['questions']] == ['chosen']
    assert 'earlier' in workflow.status()['question_frontier']
    assert not workflow.status()['complete']


@pytest.mark.parametrize('questions,code', [
    ([], 'QUESTION_FRONTIER_SELECTION_MISSING'),
    ([question('dependent', depends_on_topics=['trip'])], 'QUESTION_FRONTIER_SELECTION_BLOCKED'),
])
def test_invalid_selection_has_specific_diagnostics_and_no_partial_commit(tmp_path_factory, questions, code):
    p = process(tmp_path_factory)
    runtime = AlignmentRuntime(p)
    runtime.payload('Plan the requested result')
    _, card, node = pending_reply(p, 'Known portion only')
    payload = runtime.payload()
    before = deepcopy((p.context_snapshot, p.session.host_ledger, p.pending_decision))
    with pytest.raises(AlignmentValidationError) as caught:
        runtime.apply({'status': 'needs_input', 'interpretations': [
            interpretation(node, card, 'Known portion only', 'partial', 'Another part remains unknown')],
            'questions': questions}, payload)
    assert caught.value.validation_details['code'] == code
    assert caught.value.validation_details['ready_node_ids'] == [node]
    assert (p.context_snapshot, p.session.host_ledger, p.pending_decision) == before
    diagnostic = InvalidAlignmentAssessment(str(caught.value), validation_details=caught.value.validation_details)
    assert diagnostic.code == code.replace('QUESTION_FRONTIER_SELECTION', 'ALIGNMENT_QUESTION_SELECTION')
