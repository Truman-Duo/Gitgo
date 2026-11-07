"""Compact display uses the same audit record and retains actual risks."""
from copy import deepcopy
from backend.core.loop.decision_support import format_decision_request


def test_business_choice_does_not_require_five_audit_paragraphs(tmp_path_factory):
    from tests.test_alignment_semantics import process
    from backend.core.loop.engineering_workflow import EngineeringWorkflow
    p = process(tmp_path_factory)
    card = EngineeringWorkflow(p).ask_semantic({'kind': 'choice', 'question': 'Which order?',
        'why_user_must_decide': 'Your presentation preference', 'options': [
            {'label': 'Name', 'description': 'Sort by name'},
            {'label': 'Size', 'description': 'Sort by size', 'risks': 'Names are not alphabetical'}]})
    assert card['options'][0]['description'] == 'Sort by name'
    assert card['options'][1]['risks'] == 'Names are not alphabetical'


def test_compact_permission_keeps_scope_effect_risk_and_has_explicit_details():
    request = {'kind': 'permission', 'question': 'Read this file?',
        'why_user_must_decide': 'The file is outside the project.',
        'permission_request': {'purpose': 'Read requested evidence', 'resource': 'C:/external/note.txt'},
        'options': [{'label': 'Allow once', 'immediate_effect': 'Read the selected file once.',
            'risks': 'Its contents will enter the configured model request.',
            'principle': 'Explicit read scope', 'downstream_effect': 'Continue the original task.',
            'reversibility': 'Future reads need another approval.'}]}
    before = deepcopy(request)
    compact = format_decision_request(request)
    assert 'C:/external/note.txt' in compact
    assert request['options'][0]['risks'] in compact
    assert request['options'][0]['immediate_effect'] in compact
    assert '原理：' not in compact and '可逆性：' not in compact
    detailed = format_decision_request(request, details=True)
    assert request['options'][0]['reversibility'] in detailed
    assert request == before
