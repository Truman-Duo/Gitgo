"""Compile a semantic question selection without losing its interaction contract."""
from __future__ import annotations
import copy


def question_request(question, payload):
    proposed = payload.get('proposed_questions')
    source = question
    if payload.get('stage') == 'question_proposal' and proposed is not None:
        if 'proposal_index' not in question:
            raise ValueError('question_proposal requires proposal_index: an actual zero-based proposal index, or null for a new question')
        index = question['proposal_index']
        if index is not None:
            if type(index) is not int or not 0 <= index < len(proposed):
                raise ValueError('proposal_index must reference an actual proposed question')
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
    request = {
        'kind': source.get('kind', 'clarification'),
        'question': source['question'],
        'why_user_must_decide': source['why_user_must_decide'],
        'options': options,
        'allow_free_form': bool(source.get('allow_free_form', True)),
    }
    previous = source.get('supersedes_decision_id')
    selected_previous = question.get('supersedes_decision_id')
    if previous and selected_previous and previous != selected_previous:
        raise ValueError('selected question revision targets a different previous decision')
    if previous or selected_previous:
        request['supersedes_decision_id'] = previous or selected_previous
    return request
