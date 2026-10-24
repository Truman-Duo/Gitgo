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
    return {
        'kind': source.get('kind', 'clarification'),
        'question': source['question'],
        'why_user_must_decide': source['why_user_must_decide'],
        'options': copy.deepcopy(source.get('options') or []),
        'allow_free_form': bool(source.get('allow_free_form', True)),
    }
