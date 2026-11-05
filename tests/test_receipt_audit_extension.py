from types import SimpleNamespace

import pytest

from backend.core.mechanism_runtime.extensions.receipt_audit import observe


def context(state, effects, *, boundary='after_tool', task='task-a'):
    return SimpleNamespace(task_id=task, epoch=2, boundary=boundary, event_id='result:actual', state=state,
        check=lambda: None, call=lambda name, value: effects.append((name, value)) or {})


def test_actual_receipt_identity_outcome_and_generation_survive_restore_without_reexecution():
    effects = []
    event = {'unit_id': 'actual-unit', 'receipt': {'task_id': 'task-a', 'receipt_id': 'actual-receipt',
        'tool_name': 'read_file', 'effect': 'read', 'committed': True, 'succeeded': True, 'effect_state': 'committed'}}
    first = observe(context({}, effects), {'limit': 2}, {'event': event})
    assert first.outputs['observed'] is True
    again = observe(context(first.state, effects), {'limit': 2}, {'event': event})
    assert len(again.state['records']) == 1
    restored = observe(context(again.state, effects, boundary='restored'), {'limit': 2}, {'event': {'task_id': 'task-a'}})
    assert restored.state['records'] == first.state['records']
    assert restored.state['records'][0]['instance_epoch'] == 2
    assert {name for name, value in effects} == {'context.project'}
    assert all('authority_granted' in value['content'] for name, value in effects)


def test_foreign_receipts_are_not_restored_or_claimed_observed():
    effects = []
    result = observe(context({}, effects), {'limit': 2}, {'event': {'receipt': {
        'task_id': 'another-task', 'receipt_id': 'foreign'}}})
    assert result.outputs['observed'] is False and result.state['records'] == []
    with pytest.raises(ValueError, match='SCOPE_CHANGED'):
        observe(context({'task_id': 'another-task'}, effects), {'limit': 2}, {'event': {}})


def test_record_window_is_bounded_by_configuration():
    state, effects = {}, []
    for number in range(5):
        result = observe(context(state, effects), {'limit': 2}, {'event': {'unit_id': str(number),
            'receipt': {'task_id': 'task-a', 'receipt_id': str(number)}}})
        state = result.state
    assert [record['receipt_id'] for record in state['records']] == ['3', '4']
