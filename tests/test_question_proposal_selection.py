"""Runtime fills mechanical selection only when its reference is exact."""
from copy import deepcopy

import pytest

from backend.core.loop.alignment_question import question_request, QuestionSelectionError
from backend.core.loop.alignment_review import InvalidAlignmentAssessment


def proposal(topic='format', text='Which report format?'):
    return {'state_topic': topic, 'kind': 'choice', 'question': text,
        'why_user_must_decide': 'Format changes the requested artifact',
        'recommendation': 'Use plain text for this small report.', 'allow_free_form': False,
        'options': [{'label': 'Plain text', 'description': 'Readable in any editor'},
                    {'label': 'PDF', 'description': 'Fixed page layout'}]}


@pytest.mark.parametrize('selector', [
    {'question': 'Which report format?'}, {'state_topic': 'format'}, {'proposal_index': 0},
])
def test_exact_selector_preserves_original_options_and_contract(selector):
    original = proposal()
    before = deepcopy(original)
    result = question_request(selector, {'stage': 'question_proposal', 'proposed_questions': [original]})
    assert result['question'] == original['question'] and result['options'] == original['options']
    assert result['allow_free_form'] is False and result['kind'] == 'choice'
    assert original == before


def test_complete_new_question_needs_no_mechanical_null_index():
    new = {**proposal('scope', 'Which deliverable scope?'), 'node_id': 'existing-scope'}
    result = question_request(new, {'stage': 'question_proposal', 'proposed_questions': [proposal()]})
    assert result['question'] == new['question'] and result['options'] == new['options']


@pytest.mark.parametrize('selector,code', [
    ({'question': 'Which report format?'}, 'QUESTION_PROPOSAL_SELECTION_AMBIGUOUS'),
    ({**proposal('unrelated')}, 'QUESTION_PROPOSAL_SELECTION_AMBIGUOUS'),
    ({'state_topic': 'not-recorded'}, 'QUESTION_PROPOSAL_SELECTION_MISSING'),
    ({'proposal_index': True}, 'QUESTION_PROPOSAL_INDEX_INVALID'),
    ({'proposal_index': -1}, 'QUESTION_PROPOSAL_INDEX_INVALID'),
])
def test_ambiguous_missing_and_invalid_references_are_not_guessed(selector, code):
    original = [proposal(), proposal('second')]
    with pytest.raises(QuestionSelectionError) as error:
        question_request(selector, {'stage': 'question_proposal', 'proposed_questions': original})
    assert error.value.validation_details['code'] == code
    assert [item['proposal_index'] for item in error.value.validation_details['available_proposals']] == [0, 1]
    diagnostic = InvalidAlignmentAssessment(str(error.value), validation_details=error.value.validation_details)
    assert diagnostic.code == code.replace('QUESTION_PROPOSAL', 'ALIGNMENT_PROPOSAL')


def test_explicit_invalid_index_cannot_be_overridden_by_a_matching_question():
    with pytest.raises(QuestionSelectionError):
        question_request({**proposal(), 'proposal_index': 9},
                         {'stage': 'question_proposal', 'proposed_questions': [proposal()]})


def test_repair_limit_keeps_structural_cause_instead_of_claiming_unnecessary_questions(tmp_path_factory):
    from backend.core.loop.tool_proposal_runtime import ToolProposalRuntime
    from backend.core.loop.models import ProcessStatus
    from tests.test_alignment_semantics import process
    p = process(tmp_path_factory)
    p.status = ProcessStatus.RUNNING
    failure = {'code': 'ALIGNMENT_PROPOSAL_SELECTION_AMBIGUOUS', 'stage': 'question_proposal',
               'retryable': False, 'execution_state': 'review_not_applied'}
    runtime = ToolProposalRuntime(p, p.session, lambda: {}, lambda **kwargs: {
        'alignment_repair': 'Select the exact proposal.', 'failure': failure})
    call = {'name': 'request_user_decision', 'id': 'actual-model-call', 'args': proposal()}
    assert runtime.review([call], '').retry
    assert runtime.review([call], '').retry
    result = runtime.review([call], '').result
    assert result['error']['code'] == 'ALIGNMENT_QUESTION_REJECTED'
    assert result['error']['details']['details']['failure'] == failure
    assert '不必要' not in result['response']
    assert p.pending_decision is None and not p.approval_grants
