"""Source-selected changes reuse atomic maintenance without redundant fields."""
from copy import deepcopy

from jsonschema import Draft202012Validator
import pytest

from tests.test_task_maintenance import runtime, raw, publish
from tests.test_task_contract_activation import revision_tool
from backend.core.loop.task_maintenance import add_change, source_receipts, state_for, TaskChangeError


def pending(runtime, count=1):
    _, process, store, _ = runtime
    publish(runtime, raw(process))
    for index in range(count):
        text = f'Add requirement {index} and verify it.'
        process.session.append_user(text)
        ref = next(key for key, value in source_receipts(process).items() if value.get('content') == text)
        add_change(process, text, identifier=f'feedback-{index}', source_ref=ref,
            expected_task_id=process.active_task_id, expected_contract_revision=1,
            store=store, user_adopted=False)
    return process


def amendment(process, count=1):
    candidate = raw(process, revision=1, maintenance_revision=state_for(process)['revision'])
    candidate['acceptance_criteria'].append('Verify the newly requested behavior')
    candidate['maintenance']['resolve_changes'] = [
        {'id': f'feedback-{index}', 'reason': 'Adopt the actual requested change'} for index in range(count)]
    return candidate


def test_single_submitted_delta_binds_without_an_extra_call_or_ui_adoption(runtime):
    process = pending(runtime)
    candidate = amendment(process)
    original = deepcopy(candidate)
    publish(runtime, candidate)
    change = state_for(process)['changes']['feedback-0']
    assert change['resolution']['contract_fields'] == ['acceptance_criteria']
    assert change['status'] == 'adopted' and change['user_adopted'] is False
    assert candidate == original
    assert process.approval_grants == [] and process.capability_lease is None


def test_public_incremental_form_and_canonical_schema_share_the_binding(runtime):
    process = pending(runtime)
    tool = revision_tool(process, runtime[3])
    candidate = amendment(process)
    args = {'operation': 'revise', 'append': {'acceptance_criteria': ['Verify the newly requested behavior']},
        'plan': candidate['maintenance']['plan'], 'related_review': candidate['maintenance']['related_review'],
        'maintenance': {'resolve_changes': candidate['maintenance']['resolve_changes']}}
    Draft202012Validator(tool.to_openai_function()['function']['parameters']).validate(args)
    bound = tool.prepare_args(args)
    Draft202012Validator(tool.parameters).validate(bound)
    assert bound['maintenance']['resolve_changes'][0]['contract_fields'] == ['acceptance_criteria']
    assert 'contract_fields' not in args['maintenance']['resolve_changes'][0]


def test_multiple_changes_need_agent_associations_and_failure_is_atomic(runtime):
    process = pending(runtime, 2)
    before = deepcopy(process.context_snapshot)
    with pytest.raises(TaskChangeError) as caught:
        publish(runtime, amendment(process, 2))
    assert caught.value.code == 'TASK_CHANGE_FIELDS_REQUIRED'
    assert process.context_snapshot == before


@pytest.mark.parametrize('field,value,code', [
    ('id', 'foreign-change', 'TASK_CHANGE_REFERENCE_INVALID'),
    ('contract_fields', ['permissions'], 'TASK_CHANGE_FIELDS_INVALID'),
    ('question_ids', ['nonexistent-question'], 'TASK_CHANGE_QUESTION_REFERENCE_INVALID'),
    ('work_item_ids', ['nonexistent-step'], 'TASK_CHANGE_WORK_ITEM_INVALID'),
])
def test_distinct_invalid_bindings_keep_state_unchanged(runtime, field, value, code):
    process = pending(runtime)
    before = deepcopy(process.context_snapshot)
    candidate = amendment(process)
    candidate['maintenance']['resolve_changes'][0][field] = value
    with pytest.raises(TaskChangeError) as caught:
        publish(runtime, candidate)
    assert caught.value.code == code
    assert process.context_snapshot == before


def test_raw_update_can_request_fresh_evidence_for_real_current_steps(runtime):
    process = pending(runtime)
    candidate = amendment(process)
    candidate['maintenance']['resolve_changes'][0]['work_item_ids'] = ['implement']
    publish(runtime, candidate)
    change = state_for(process)['changes']['feedback-0']
    assert change['resolution']['work_item_ids'] == ['implement']
    assert change['user_adopted'] is False and not process.approval_grants
