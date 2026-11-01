"""Post-v08-freeze extension: audit pending question continuity on recovery.

Only observes the canonical question read service and projects its identities.
It cannot publish, answer, close, adopt or grant anything.
"""
import json

from backend.core.mechanism_runtime.contracts import Element, Port
from backend.core.mechanism_runtime.runner import NodeResult


def audit(ctx, params, inputs):
    ctx.check()
    if ctx.state.get('task_id', ctx.task_id) != ctx.task_id:
        raise ValueError('QUESTION_AUDIT_SCOPE_CHANGED')
    page = ctx.call('decision.read', {'offset': 0, 'limit': 1})
    if page['task_id'] != ctx.task_id or any(item['task_id'] != ctx.task_id for item in page['questions']['items']):
        raise ValueError('QUESTION_AUDIT_SCOPE_CHANGED')
    history = page['questions']
    visible = {item['decision_id']: item for item in history['items']}
    awaiting = [{key: item.get(key) for key in ('decision_id', 'kind', 'status', 'answer_state')}
        for item in history['items'] if item['status'] == 'awaiting_user' and
        (not params['kinds'] or item.get('kind') in params['kinds'])]
    current = {item['decision_id'] for item in awaiting}
    changed = [{'decision_id': previous['decision_id'],
        'observed_status': visible[previous['decision_id']]['status'] if previous['decision_id'] in visible else 'not_in_visible_history',
        'requires_source_inspection': previous['decision_id'] not in visible}
        for previous in ctx.state.get('awaiting', []) if previous['decision_id'] not in current]
    report = {'task_id': ctx.task_id, 'current_awaiting_questions': awaiting,
        'previous_no_longer_in_awaiting_projection': changed, 'history_partial': history['partial'],
        'source': 'canonical current-task question ledger', 'authority_granted': False}
    ctx.call('context.project', {'content': 'Question continuity audit: ' + json.dumps(report,
        ensure_ascii=False, sort_keys=True, separators=(',', ':')),
        'include_event': False})
    return NodeResult({'checked': True, 'report': report}, {'task_id': ctx.task_id, 'awaiting': awaiting,
        'checks': ctx.state.get('checks', 0) + 1, 'last_boundary': ctx.boundary})


def install(registry):
    return registry.register(Element('question.continuity_audit', 1,
        {'type': 'object', 'properties': {'kinds': {'type': 'array', 'items': {'type': 'string', 'minLength': 1,
            'maxLength': 48}, 'maxItems': 8, 'uniqueItems': True, 'default': []}}, 'additionalProperties': False},
        (Port('go', 'trigger'), Port('event', 'record', False)), (Port('checked', 'trigger'), Port('report', 'record')),
        ('after_compaction', 'restored'), audit, effect='projection', capabilities=('decision.read', 'context.project'),
        description='Audit current-task unanswered question identities at actual compaction/restore boundaries. Distinguish received/superseded history from still-awaiting questions, mark missing bounded history as uncertain, and project the canonical observations without answering or closing a question.'))
