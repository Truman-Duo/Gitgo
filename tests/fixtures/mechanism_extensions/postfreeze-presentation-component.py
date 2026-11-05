"""New after v22 core freeze: bounded question-presentation recovery audit."""
import json
from backend.core.mechanism_runtime.contracts import Element, Port, digest
from backend.core.mechanism_runtime.runner import NodeResult


def audit(ctx, parameters, inputs):
    ctx.check()
    if ctx.state.get('task_id', ctx.task_id) != ctx.task_id:
        raise ValueError('PRESENTATION_AUDIT_SCOPE_CHANGED')
    page = ctx.call('decision.read', {'offset': 0, 'limit': 1})
    if page['task_id'] != ctx.task_id:
        raise ValueError('PRESENTATION_AUDIT_SCOPE_CHANGED')
    questions = page['questions']
    records = []
    for item in questions['items']:
        if item['task_id'] != ctx.task_id:
            raise ValueError('PRESENTATION_AUDIT_SCOPE_CHANGED')
        if not parameters['include_received'] and item['status'] != 'awaiting_user':
            continue
        view = {k: item.get(k) for k in ('kind', 'question', 'options', 'materials')}
        records.append({'decision_id': item['decision_id'], 'status': item['status'], 'presentation_version': digest(view),
            'option_count': len(item.get('options') or []), 'material_count': len(item.get('materials') or [])})
    versions = {r['decision_id']: r['presentation_version'] for r in records}
    previous = ctx.state.get('versions', {})
    report = {'task_id': ctx.task_id, 'records': records, 'partial': questions['partial'],
        'changed_visible_presentations': sorted(k for k in set(previous) & set(versions) if previous[k] != versions[k]),
        'authority_granted': False,
        'scope': 'canonical bounded question presentation projection; not a complete UI schema or user answer'}
    ctx.call('context.project', {'content': 'Question presentation audit: ' + json.dumps(report, ensure_ascii=False, separators=(',', ':')),
        'include_event': False})
    return NodeResult({'checked': True, 'report': report}, {'task_id': ctx.task_id,
        'versions': previous if questions['partial'] else versions, 'checks': ctx.state.get('checks', 0) + 1})


def install(registry):
    return registry.register(Element('question.presentation_audit', 1,
        {'type': 'object', 'properties': {'include_received': {'type': 'boolean', 'default': False}}, 'additionalProperties': False},
        (Port('go', 'trigger'), Port('event', 'record', False)), (Port('checked', 'trigger'), Port('report', 'record')),
        ('after_compaction', 'restored'), audit, effect='projection', capabilities=('decision.read', 'context.project'),
        description='At compaction/restore, fingerprint the current-task bounded question presentation projection (kind, text, option labels and attached references). Report same-ID presentation changes and history partial status. Configurable received-history inclusion; no answering, approval, execution control or subscriptions.'))
