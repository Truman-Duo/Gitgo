"""Post-freeze extension: task-local audit stamp at context lifecycle boundaries.

Uses the public capability interface only. This observer neither recovers
decisions nor participates in tool transaction/event deduplication repairs.
"""
import json

from backend.core.mechanism_runtime.contracts import Element, Port
from backend.core.mechanism_runtime.runner import NodeResult


def audit(ctx, params, inputs):
    snapshot = ctx.call('context.inspect', {'offset': 0, 'limit': 1})
    if snapshot['task_id'] != ctx.task_id or snapshot['stage'] != 'pre_request_assembly_input':
        raise ValueError('CONTEXT_AUDIT_SCOPE_MISMATCH')
    record = {'label': params['label'], 'task_id': ctx.task_id, 'context_epoch': snapshot['context_epoch'],
        'assembly_digest': snapshot['assembly_digest'], 'message_count': snapshot['total'],
        'boundary': ctx.boundary, 'event_id': ctx.event_id, 'instance_epoch': ctx.epoch,
        'observation': 'assembly input, not proof of provider receipt', 'authority_granted': False}
    ctx.call('context.project', {'content': 'Context boundary audit: ' + json.dumps(record,
        ensure_ascii=False, sort_keys=True, separators=(',', ':')), 'include_event': False})
    return NodeResult({'audited': True, 'report': record}, {'last': record, 'checks': ctx.state.get('checks', 0) + 1})


def install(registry):
    return registry.register(Element('context.boundary_audit', 1,
        {'type': 'object', 'properties': {'label': {'type': 'string', 'minLength': 1,
            'maxLength': 80, 'default': 'task-local lifecycle audit'}}, 'additionalProperties': False},
        (Port('go', 'trigger'), Port('event', 'record', False)), (Port('audited', 'trigger'), Port('report', 'record')),
        ('after_compaction', 'restored'), audit, effect='projection',
        capabilities=('context.inspect', 'context.project'),
        description='At actual compaction/restore boundaries project this task’s context epoch, assembly digest and event provenance. No conversation contents, decisions, permissions or tool effects are changed. This is an input audit stamp; actual sent requests must still be checked in Trace.'))
