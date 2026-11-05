"""New after v20 freeze: observe confirmed-decision lineage across restores."""
import json
from backend.core.mechanism_runtime.contracts import Element, Port, digest
from backend.core.mechanism_runtime.runner import NodeResult


def audit(ctx, parameters, inputs):
    ctx.check()
    if ctx.state.get('task_id', ctx.task_id) != ctx.task_id:
        raise ValueError('DECISION_LINEAGE_SCOPE_CHANGED')
    records = {}
    offset = 0
    for _ in range(parameters['max_pages']):
        page = ctx.call('decision.read', {'offset': offset, 'limit': 32})
        if page['task_id'] != ctx.task_id:
            raise ValueError('DECISION_LINEAGE_SCOPE_CHANGED')
        for value in page['confirmed']:
            identifier = value['decision_id']
            records[identifier] = {'decision_id': identifier, 'topic': value['topic'],
                'version': digest(value), 'supersedes_decision_id': value.get('supersedes_decision_id', '')}
        offset = page['next_offset']
        if offset is None:
            break
    previous = ctx.state.get('known', {})
    partial = offset is not None
    retired = sorted(set(previous) - set(records)) if not partial else []
    report = {'task_id': ctx.task_id, 'records': list(records.values()), 'partial': partial,
        'retired_since_previous_complete_snapshot': retired,
        'replacements': [{'previous': r['supersedes_decision_id'], 'current': r['decision_id']}
            for r in records.values() if r['supersedes_decision_id']],
        'changed_versions': sorted(k for k in set(previous) & set(records)
            if previous[k]['version'] != records[k]['version']),
        'authority_granted': False, 'scope': 'canonical current-task confirmed decision lineage; observation only'}
    ctx.call('context.project', {'content': 'Decision lineage audit: ' + json.dumps(report, ensure_ascii=False, separators=(',', ':')),
        'include_event': False})
    return NodeResult({'checked': True, 'report': report}, {'task_id': ctx.task_id,
        'known': previous if partial else records, 'checks': ctx.state.get('checks', 0) + 1, 'last_report': report})


def install(registry):
    return registry.register(Element('decision.lineage_audit', 1,
        {'type': 'object', 'properties': {'max_pages': {'type': 'integer', 'minimum': 1, 'maximum': 4, 'default': 2}},
         'additionalProperties': False},
        (Port('go', 'trigger'), Port('event', 'record', False)), (Port('checked', 'trigger'), Port('report', 'record')),
        ('after_compaction', 'restored'), audit, effect='projection', capabilities=('decision.read', 'context.project'),
        description='At compaction/restore, compare current-task confirmed decision IDs and exact value versions with the previous complete snapshot. Report actual supersession links and retired IDs. Partial pages never imply retirement; no answering, approval, execution control or subscriptions.'))
