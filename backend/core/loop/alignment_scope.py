"""Validate that elicitation serves an existing user request, not a future task.

The model supplies meaning; Host binds it to real input and enforces publication
rules. No greeting keyword list and no extra classifier request.
"""
from __future__ import annotations


def user_sources(payload):
    values = [payload.get('request'), payload.get('original_request')]
    values.extend(payload.get('user_updates') or [])
    values.extend(item.get('answer') for item in
                  [*(payload.get('actual_answers') or []), *(payload.get('confirmed_answers') or [])])
    return [value for value in values if isinstance(value, str) and value.strip()]


def source_quote(value, sources):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2000 and any(value in s for s in sources)


def validate_request_frame(assessment, payload):
    frame = assessment.get('request_frame')
    if frame is None:
        # Legacy/injected assessor adapters remain compatible. The production
        # provider facade requires this field before its result can be applied.
        return None
    sources = user_sources(payload)
    if (not isinstance(frame, dict) or frame.get('kind') not in {'conversation', 'deliverable'}
            or not source_quote(frame.get('source_quote'), sources)
            or not isinstance(frame.get('outcome'), str) or not frame['outcome'].strip()
            or len(frame['outcome']) > 1000):
        raise ValueError('request_frame must describe the current outcome and quote actual user input')
    questions = assessment.get('questions') or []
    if frame['kind'] == 'conversation':
        if questions or assessment.get('status') == 'needs_input':
            raise ValueError('conversation means an already answerable social/bounded response: absence of a future task is NOT a missing requirement. '
                'If actual user input requests requirement elicitation, planning or another substantive outcome through dialogue, '
                'classify request_frame.kind=deliverable and provide source-bound necessity; otherwise return ready without questions')
        return frame
    for question in questions:
        witness = question.get('necessity') if isinstance(question, dict) else None
        if not isinstance(witness, dict) or not source_quote(witness.get('source_quote'), sources):
            raise ValueError('question necessity must quote the actual requested outcome, not candidate/proposed questions/history')
        component = witness.get('outcome_component')
        alternatives = witness.get('answer_changes')
        if (not isinstance(component, str) or not component.strip() or len(component) > 1000
                or not isinstance(alternatives, list) or not 2 <= len(alternatives) <= 4
                or any(not isinstance(value, str) or not value.strip() or len(value) > 1000 for value in alternatives)
                or len(set(alternatives)) != len(alternatives)):
            raise ValueError('question necessity requires an affected requested component and distinct outcome changes; missing a future task is insufficient')
    return frame
