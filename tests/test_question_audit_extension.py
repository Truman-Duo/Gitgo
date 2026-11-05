from types import SimpleNamespace

import pytest

from backend.core.mechanism_runtime.extensions.question_audit import audit


def run(items, state=None, *, partial=False, task='task-a'):
    calls = []
    def call(name, payload):
        calls.append((name, payload))
        return {'task_id': task, 'questions': {'items': items, 'partial': partial}} if name == 'decision.read' else {}
    ctx = SimpleNamespace(task_id='task-a', state=state or {}, boundary='restored', check=lambda: None, call=call)
    return audit(ctx, {'kinds': []}, {}), calls


def question(identifier, status='awaiting_user'):
    return {'task_id': 'task-a', 'decision_id': identifier, 'kind': 'clarification', 'status': status,
        'answer_state': 'not_received' if status == 'awaiting_user' else 'received'}


def test_recovery_distinguishes_real_pending_identity_from_received_history_without_answering():
    first, _ = run([question('q1')])
    second, calls = run([question('q1', 'received'), question('q2')], first.state)
    assert [q['decision_id'] for q in second.state['awaiting']] == ['q2']
    changed = second.outputs['report']['previous_no_longer_in_awaiting_projection']
    assert changed == [{'decision_id': 'q1', 'observed_status': 'received', 'requires_source_inspection': False}]
    assert [name for name, _ in calls] == ['decision.read', 'context.project']
    assert second.outputs['report']['authority_granted'] is False


def test_truncated_history_never_turns_absence_into_a_claim_of_resolution():
    first, _ = run([question('old')])
    result, _ = run([question('new')], first.state, partial=True)
    assert result.outputs['report']['history_partial']
    assert result.outputs['report']['previous_no_longer_in_awaiting_projection'] == [
        {'decision_id': 'old', 'observed_status': 'not_in_visible_history', 'requires_source_inspection': True}]


def test_foreign_task_observation_is_rejected():
    with pytest.raises(ValueError, match='SCOPE_CHANGED'):
        run([question('foreign')], task='another-task')
