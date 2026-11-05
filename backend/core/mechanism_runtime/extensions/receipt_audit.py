"""Post-v07-freeze extension: retain bounded, task-local receipt provenance.

An observer of real invocation completion and restore, not a tool executor,
approval, semantic judge, decision recovery adapter or deduplication repair.
"""
import json

from backend.core.mechanism_runtime.contracts import Element, Port
from backend.core.mechanism_runtime.runner import NodeResult


def observe(ctx, params, inputs):
    ctx.check()
    if ctx.state.get('task_id', ctx.task_id) != ctx.task_id:
        raise ValueError('RECEIPT_AUDIT_SCOPE_CHANGED')
    records = list(ctx.state.get('records', []))
    accepted = False
    if ctx.boundary == 'after_tool':
        event = inputs['event']
        receipt = event.get('receipt') or {}
        if receipt.get('task_id') == ctx.task_id and receipt.get('receipt_id'):
            record = {key: receipt.get(key) for key in
                ('receipt_id', 'tool_name', 'effect', 'succeeded', 'committed', 'effect_state')}
            record.update(unit_id=event['unit_id'], instance_epoch=ctx.epoch, event_id=ctx.event_id)
            if not any(item['receipt_id'] == record['receipt_id'] for item in records):
                records.append(record)
            accepted = True
    records = records[-params['limit']:]
    report = {'task_id': ctx.task_id, 'receipt_provenance': records, 'boundary': ctx.boundary,
        'source': 'existing tool-pipeline receipts', 'authority_granted': False}
    ctx.call('context.project', {'content': 'Receipt provenance audit: ' + json.dumps(report,
        ensure_ascii=False, sort_keys=True, separators=(',', ':')), 'include_event': False})
    return NodeResult({'observed': accepted, 'report': report}, {'task_id': ctx.task_id,
        'records': records, 'last_boundary': ctx.boundary, 'checks': ctx.state.get('checks', 0) + 1})


def install(registry):
    return registry.register(Element('tool.receipt_audit', 1,
        {'type': 'object', 'properties': {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 8,
            'default': 4}}, 'additionalProperties': False},
        (Port('go', 'trigger'), Port('event', 'record')), (Port('observed', 'boolean'), Port('report', 'record')),
        ('after_tool', 'restored'), observe, effect='projection', capabilities=('context.project',),
        description='Observe completed actual tool invocations and restore this task’s bounded receipt identities, outcome fields and invocation epochs through ordinary context assembly. Does not run, retry, approve or undo any operation.'))
