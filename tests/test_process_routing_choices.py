"""Routing choices remain model decisions; evidence and authority stay Host-owned."""
from copy import deepcopy
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator

from backend.core.loop.task_contract import (
    compile_routing_proposal, get_task_contract, publish_contract,
    validate_contract_proposal, workflow_has_progress,
)
from .test_task_contract_recovery import _supervisor


def proposal(root, **fields):
    return validate_contract_proposal({
        'goal': 'Investigate and implement the requested change',
        'execution_mode': 'either', 'deliverables': [{'kind': 'response'}],
        'acceptance_criteria': ['Explain verified behavior'], **fields,
    }, str(root))


@pytest.mark.parametrize('facts', [{}, {'estimated_complexity': 'unknown', 'independent_workstreams': None},
    {'estimated_complexity': 'bounded', 'independent_workstreams': 1}])
def test_explicit_delegate_is_not_overridden_by_unknown_or_bounded_advice(tmp_path_factory, facts):
    compiled, route = compile_routing_proposal({}, proposal(tmp_path_factory, execution_mode='delegate',
        delegation_required=True, **facts), workflow_started=False)
    assert compiled['execution_mode'] == 'delegate' and compiled['delegation_required']
    assert route['effective'] == 'delegate' and route['next_action'] == 'delegate_task'
    assert route['authority_granted'] is False and route['adjusted'] is False
    assert route['recommended'] == ('self_execute' if facts.get('estimated_complexity') == 'bounded' else 'undecided')


def test_omission_unknown_and_revision_retention_are_distinct(tmp_path_factory):
    root, tools = _supervisor(tmp_path_factory)
    tool = tools['declare_task_contract']
    initial = {'goal': 'Investigate', 'execution_mode': 'either', 'deliverables': [], 'acceptance_criteria': ['Original']}
    Draft202012Validator(tool.to_openai_function()['function']['parameters']).validate(initial)
    first = tool.execute(initial)
    assert first['accepted'], first
    assert 'estimated_complexity' not in first['contract']
    assert 'independent_workstreams' not in first['contract']
    assert first['routing_compilation']['effective'] == 'undecided'
    update = {'operation': 'revise', 'estimated_complexity': 'unknown', 'independent_workstreams': None}
    Draft202012Validator(tool.to_openai_function()['function']['parameters']).validate(update)
    assert tool.execute(update)['accepted']
    result = tool.execute({'operation': 'revise', 'append': {'acceptance_criteria': ['Added']}})
    assert result['accepted'], result
    assert result['contract']['estimated_complexity'] == 'unknown'
    assert result['contract']['independent_workstreams'] is None
    assert result['contract']['acceptance_criteria'] == ['Original', 'Added']
    assert not root.capability_lease and not root.approval_grants


@pytest.mark.parametrize('mode', ['delegate', 'either'])
def test_bounded_advice_does_not_block_real_delegation_admission(tmp_path_factory, mode):
    root, tools = _supervisor(tmp_path_factory)
    publish_contract(root, proposal(tmp_path_factory, execution_mode=mode,
        estimated_complexity='bounded', independent_workstreams=1))
    with patch.object(root._manager, 'start'):
        result = tools['delegate_task'].execute({'task_description': 'Investigate a separate question',
            'target_files': [], 'acceptance_criteria': ['Verified answer'],
            'capability_profile_id': 'governance.observe', 'task_kind': 'answer'})
    assert result['delegated'], result
    assert root.capability_lease is None
    assert not root._manager.get(result['process_id']).tool_registry.has('write_file')


def test_handoff_uses_current_successful_observation_not_failed_attempts(tmp_path_factory):
    root, tools = _supervisor(tmp_path_factory)
    publish_contract(root, proposal(tmp_path_factory, execution_mode='delegate'))
    before = deepcopy(get_task_contract(root))
    failed = tools['delegate_task'].execute({'task_description': 'Invalid profile combination',
        'target_files': [], 'acceptance_criteria': ['Implementation'],
        'capability_profile_id': 'governance.observe', 'task_kind': 'action'})
    assert not failed['delegated'] and not root.child_ids
    assert get_task_contract(root)['delegation_attempts'][-1]['state'] == 'admission_failed'
    assert not workflow_has_progress(root)
    handoff = proposal(tmp_path_factory, execution_mode='delegate', routing_transition='handoff_to_worker')
    with pytest.raises(ValueError, match='Host-observed work'):
        publish_contract(root, handoff)
    receipt = {'task_id': root.active_task_id, 'tool_name': 'read_file', 'effect': 'read',
        'succeeded': True, 'committed': True,
        'execution_contract': {'kind': 'native_process', 'file_action': 'read_existing_file'}}
    for changes in ({'succeeded': False}, {'committed': False}, {'task_id': 'previous-task'},
                    {'execution_contract': {'kind': 'data_broker', 'broker': 'host.runtime.list_agents'}}):
        root.tool_receipts = [{**receipt, **changes}]
        assert not workflow_has_progress(root)
    root.tool_receipts = [receipt]
    published = publish_contract(root, handoff)
    assert published['routing_compilation']['effective'] == 'delegate'
    assert published['delegation_attempts'][-1]['state'] == 'admission_failed'
    assert root.successful_actions == 0 and root.capability_lease is None
    assert before['acceptance_criteria'] == published['acceptance_criteria']


def test_legacy_attempt_flag_alone_cannot_certify_handoff(tmp_path_factory):
    root, _ = _supervisor(tmp_path_factory)
    root.context_snapshot['task_contract']['workflow_started'] = True
    root.successful_actions = 1  # Legacy counters included control-plane calls.
    with pytest.raises(ValueError, match='Host-observed work'):
        publish_contract(root, proposal(tmp_path_factory, execution_mode='delegate', routing_transition='handoff_to_worker'))


@pytest.mark.parametrize('transition,mode', [('keep_supervisor', 'delegate'), ('handoff_to_worker', 'self_execute')])
def test_contradictory_explicit_choices_are_not_silently_rewritten(tmp_path_factory, transition, mode):
    with pytest.raises(ValueError):
        compile_routing_proposal({}, proposal(tmp_path_factory, execution_mode=mode,
            routing_transition=transition), workflow_started=True)
