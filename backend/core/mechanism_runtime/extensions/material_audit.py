"""Post-v12 registration of a previously unexercised lifecycle component.

Authored as a developer candidate after v10, then registered only after v12
core freeze. It has not guided core repairs and is not a human holdout.
"""
import json
from backend.core.mechanism_runtime.contracts import Element, Port, digest
from backend.core.mechanism_runtime.runner import NodeResult


def audit(ctx, params, inputs):
    ctx.check()
    if ctx.state.get('task_id', ctx.task_id) != ctx.task_id:
        raise ValueError('MATERIAL_AUDIT_SCOPE_CHANGED')
    page = ctx.call('decision.read', {'offset': 0, 'limit': 1})
    if page['task_id'] != ctx.task_id:
        raise ValueError('MATERIAL_AUDIT_SCOPE_CHANGED')
    questions = page['questions']
    if any(q['task_id'] != ctx.task_id for q in questions['items']):
        raise ValueError('MATERIAL_AUDIT_SCOPE_CHANGED')
    selected = sorted(questions['items'], key=lambda q: (q['status'] != 'awaiting_user', q['decision_id']))
    selected = [q for q in selected if params['include_answered'] or q['status'] == 'awaiting_user'][:params['max_questions']]
    checked = []
    incomplete = bool(questions.get('partial')) or len(selected) < len(questions['items'])
    for question in selected:
        refs = sorted({m['reference'] for m in question.get('materials', [])})
        incomplete = incomplete or len(refs) > params['max_materials']
        for reference in refs[:params['max_materials']]:
            facts = ctx.call('decision.read', {'offset': 0, 'limit': 1, 'material_reference': reference})
            if facts['task_id'] != ctx.task_id or any(f['task_id'] != ctx.task_id for f in facts.get('material_facts', [])):
                raise ValueError('MATERIAL_AUDIT_SCOPE_CHANGED')
            matching = [f for f in facts.get('material_facts', []) if f.get('decision_id') == question['decision_id']
                and f.get('reference') == reference and f.get('event') == 'user_decision_material_attached']
            checked.append({'decision_id': question['decision_id'], 'question_status': question['status'], 'reference': reference,
                'canonical_link_observed': len(matching) == 1, 'fact_digest': digest(matching[0]) if len(matching) == 1 else None,
                'requires_source_inspection': len(matching) != 1})
    report = {'task_id': ctx.task_id, 'boundary': ctx.boundary, 'material_links': checked, 'history_or_selection_partial': incomplete,
        'source': 'canonical current-task question material ledger', 'authority_granted': False}
    ctx.call('context.project', {'content': 'Material continuity audit: ' + json.dumps(report, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')), 'include_event': False})
    return NodeResult({'checked': True, 'report': report}, {'task_id': ctx.task_id,
        'checks': ctx.state.get('checks', 0) + 1, 'last_report_digest': digest(report)})


def install(registry):
    return registry.register(Element('material.continuity_audit', 1,
        {'type': 'object', 'properties': {'max_questions': {'type':'integer','minimum':1,'maximum':8,'default':4},
            'max_materials': {'type':'integer','minimum':1,'maximum':4,'default':2},
            'include_answered': {'type':'boolean','default':True}}, 'additionalProperties':False},
        (Port('go','trigger'), Port('event','record',False)), (Port('checked','trigger'), Port('report','record')),
        ('after_compaction','restored'), audit, effect='projection', capabilities=('decision.read','context.project'),
        description='At actual compaction/restore boundaries, cross-check current-task question material references against canonical attachment facts. Keep answered/superseded status explicit, mark missing or bounded evidence uncertain, and project identities/digests only. Never attaches, answers, adopts, recreates questions or grants authority.'))
