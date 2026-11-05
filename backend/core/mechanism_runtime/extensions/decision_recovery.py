"""Decision recovery through the existing public lifecycle component SDK.

Uses only the registered Context services. No process, ledger, database, tool
registry, permission mutation or provider implementation is accessible here.
"""
import json

from backend.core.mechanism_runtime.contracts import Element, Port, digest
from backend.core.mechanism_runtime.runner import NodeResult


def recover(ctx, params, inputs):
    ctx.check()
    decisions = []
    offset = 0
    for _ in range(4):
        page = ctx.call('decision.read', {'offset': offset, 'limit': 32})
        if page['task_id'] != ctx.task_id:
            raise ValueError('RECOVERY_SCOPE_CHANGED')
        decisions.extend(page['confirmed'])
        offset = page['next_offset']
        if offset is None:
            break
    if offset is not None:
        raise ValueError('RECOVERY_EVIDENCE_CAPACITY')
    if params['topics']:
        decisions = [item for item in decisions if item['topic'] in params['topics']]
    current = [{**item, 'version': digest(item)} for item in decisions]
    prior = ctx.state.get('current', [])
    valid = {item['decision_id']: item['version'] for item in current}
    outdated = [{'decision_id': item['decision_id'], 'topic': item['topic'], 'previous_version': item['version'],
        'action': 'reassess_from_original_question_if_still_needed_do_not_restore_old_value'}
        for item in prior if valid.get(item['decision_id']) != item['version']]
    record = {'task_id': ctx.task_id, 'authority': 'canonical_current_task_decision_ledger',
        'current_confirmed_decisions': current, 'outdated_projection_links': outdated,
        'authority_granted': False}
    serialized = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    # The bounded read adapter exposes actual assembly inputs, not an invented
    # claim that these messages have already reached the provider. The API
    # acceptance separately inspects genuine sent requests in Trace.
    present = False
    offset = 0
    for _ in range(8):
        page = ctx.call('context.inspect', {'offset': offset, 'limit': 32})
        if page['task_id'] != ctx.task_id:
            raise ValueError('RECOVERY_SCOPE_CHANGED')
        escaped = json.dumps(serialized, ensure_ascii=False)[1:-1]
        if any(message.get('message_type') == 'host_dynamic_envelope' and
               (serialized in str(message.get('content', '')) or escaped in str(message.get('content', '')))
               for message in page['messages']):
            present = True
        offset = page['next_offset']
        if offset is None:
            break
    if offset is not None:
        raise ValueError('RECOVERY_CONTEXT_CAPACITY')
    if not present or ctx.state.get('record_digest') != digest(record):
        ctx.call('context.project', {'content': 'Decision recovery: preserve only the current records below. '
            'Outdated links require reassessment from their original questions; never restore their old values, '
            'answer questions or infer permission.\n' + serialized, 'include_event': False})
    return NodeResult({'checked': True, 'report': {'task_id': ctx.task_id, 'decision_count': len(current),
        'outdated_count': len(outdated), 'projection_was_present': present, 'record_digest': digest(record)}},
        {'current': current, 'record_digest': digest(record), 'last_boundary': ctx.boundary,
         'last_event_id': ctx.event_id, 'checks': ctx.state.get('checks', 0) + 1})


def install(registry):
    """Registration declaration; caller owns and awaits the retirement handle."""
    return registry.register(Element('decision.recovery', 1,
        {'type': 'object', 'properties': {'topics': {'type': 'array', 'items': {'type': 'string'},
            'maxItems': 32, 'uniqueItems': True, 'default': []}}, 'additionalProperties': False},
        (Port('go', 'trigger'), Port('event', 'record', False)), (Port('checked', 'trigger'), Port('report', 'record')),
        ('after_compaction', 'restored'), recover, effect='projection',
        capabilities=('decision.read', 'context.inspect', 'context.project'),
        description='After actual compaction or checkpoint restore, reconcile this task’s current confirmed decisions and versions with context assembly. Replace stale extension projections and link outdated original questions for reassessment; never answer or grant permission.'))
