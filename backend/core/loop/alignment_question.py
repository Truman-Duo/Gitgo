"""Compile a semantic question selection without losing its interaction contract."""
from __future__ import annotations
import copy


class QuestionSelectionError(ValueError):
    def __init__(self, message, code, proposed):
        super().__init__(message)
        self.validation_details = {'code': code, 'available_proposals': [
            {'proposal_index': i, 'state_topic': item.get('state_topic')}
            for i, item in enumerate(proposed) if isinstance(item, dict)]}


def proposal_selection(question, proposed):
    """Infer only exact references or a complete new semantic question.

    No string similarity, preferred option, user answer, or permission is
    inferred. An explicit index remains authoritative, including invalid ones.
    """
    if 'proposal_index' in question:
        return question['proposal_index']
    text = question.get('question')
    if isinstance(text, str) and text.strip():
        matches = [i for i, item in enumerate(proposed)
                   if isinstance(item, dict) and item.get('question') == text]
    else:
        matches = [i for i, item in enumerate(proposed)
            if isinstance(item, dict) and question.get('state_topic')
            and item.get('state_topic') == question['state_topic']]
    if len(matches) > 1:
        if question.get('state_topic'):
            matches = [i for i in matches if proposed[i].get('state_topic') == question['state_topic']]
        if len(matches) != 1:
            raise QuestionSelectionError('The proposal reference is ambiguous; select an actual proposed question by its index.',
                                         'QUESTION_PROPOSAL_SELECTION_AMBIGUOUS', proposed)
    if len(matches) == 1:
        return matches[0]
    if all(isinstance(question.get(k), str) and question[k].strip()
           for k in ('question', 'why_user_must_decide', 'recommendation')):
        return None  # complete assessor-authored question; no mechanical null required
    raise QuestionSelectionError('Select an actual proposed question by exact text/topic or index, or supply a complete new question.',
                                 'QUESTION_PROPOSAL_SELECTION_MISSING', proposed)


def question_request(question, payload):
    proposed = payload.get('proposed_questions')
    source = question
    if payload.get('stage') == 'question_proposal' and proposed is not None:
        index = proposal_selection(question, proposed)
        if index is not None:
            if type(index) is not int or not 0 <= index < len(proposed):
                raise QuestionSelectionError('proposal_index must reference an actual proposed question',
                                             'QUESTION_PROPOSAL_INDEX_INVALID', proposed)
            source = proposed[index]
            if not isinstance(source, dict):
                raise ValueError('selected question proposal must be an object')
    # The chosen proposal's labels and option consequences are user interaction
    # data, not authority. The existing broker validates the complete request.
    options = copy.deepcopy(source.get('options') or [])
    if source is question and source.get('kind', 'clarification') == 'clarification' and isinstance(options, list):
        # New assessor clarifications may encode plain labels compactly. Never
        # invent consequences/actions or rewrite a selected original proposal.
        options = [{'label': item} if isinstance(item, str) and item.strip() else item for item in options]
    from .question_surface import question_options
    options = question_options({**source, 'options': options})
    request = {
        'kind': source.get('kind', 'clarification'),
        'question': source['question'],
        'why_user_must_decide': source['why_user_must_decide'],
        'options': options,
        'allow_free_form': bool(source.get('allow_free_form', True)),
    }
    recommendation = source.get('recommendation') or question.get('recommendation')
    if recommendation:
        request['recommendation'] = recommendation
    previous = source.get('supersedes_decision_id')
    selected_previous = question.get('supersedes_decision_id')
    if previous and selected_previous and previous != selected_previous:
        raise ValueError('selected question revision targets a different previous decision')
    if previous or selected_previous:
        request['supersedes_decision_id'] = previous or selected_previous
    return request
