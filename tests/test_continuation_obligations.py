"""Continuation preserves unresolved responsibilities without reviving old evidence."""
from copy import deepcopy
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.core.loop.continuation_contract import compile_continuation, require_direct_followup
from backend.core.loop.models import ProcessStatus
from backend.core.loop.task_contract import get_task_contract
from .test_task_contract_recovery import _supervisor


def delegate(tools, **extra):
    return tools['delegate_task'].execute({'task_description': 'Investigate the original bug',
        'capability_profile_id': 'governance.observe', 'task_kind': 'answer',
        'target_files': [], 'acceptance_criteria': ['Original obligation'], **extra})


def fixture(root_path, *, required_calls=None):
    root, tools = _supervisor(root_path)
    manager = root._manager
    with patch.object(manager, 'start'):
        result = delegate(tools, required_tool_calls=required_calls or [])
    assert result['delegated'], result
    old = manager.get(result['process_id'])
    old.status = ProcessStatus.FAILED
    old.result = {'status': 'failed', 'response': 'Retain this partial investigation'}
    old.lifecycle_finalized = True
    old.session.append_user('Original context remains available')
    return root, tools, old


def test_default_remaining_retains_obligations_without_resetting_budget_or_evidence(tmp_path_factory):
    root, tools, old = fixture(tmp_path_factory,
        required_calls=[{'tool_name': 'read_file', 'min_calls': 1, 'max_calls': 3}])
    budget = root.task_budget
    budget.begin_provider_call(old.process_id)
    budget.release_process(old.process_id, status='failed')
    consumed = deepcopy(budget.snapshot()['used'])
    original = deepcopy(root.delegated_contracts[old.process_id])
    with patch.object(root._manager, 'start'):
        result = delegate(tools, continue_process_id=old.process_id,
            acceptance_criteria=['Additional obligation'],
            required_tool_calls=[{'tool_name': 'read_file', 'min_calls': 2, 'max_calls': 4}])
    assert result['delegated'], result
    new = root._manager.get(result['process_id'])
    contract = root.delegated_contracts[new.process_id]
    assert new.session is old.session and new.task_budget is budget
    assert budget.snapshot()['used']['provider_calls'] == consumed['provider_calls']
    assert contract['acceptance_criteria'] == ['Original obligation', 'Additional obligation']
    assert contract['required_for_parent_completion'] is True
    assert contract['required_tool_calls'][0]['min_calls'] == 2
    assert contract['required_tool_calls'][0]['max_calls'] == 3
    assert new.tool_receipts == [] and new.completion_claim is None
    assert old.result['status'] == 'failed'
    assert {k: v for k, v in root.delegated_contracts[old.process_id].items() if k != 'superseded_by'} == original
    assert result['continuation']['mode'] == 'remaining'
    assert result['continuation']['retained_assignment'] == old.task_description
    assert 'Original obligation' in new.task_description


@pytest.mark.parametrize('mode', ['follow_up', 'replace'])
def test_explicit_replacement_cannot_drop_unfinished_contract(tmp_path_factory, mode):
    root, tools, old = fixture(tmp_path_factory)
    before = deepcopy(root.delegated_contracts)
    agents = root.task_budget.snapshot()['used']['agents']
    result = delegate(tools, continue_process_id=old.process_id, continuation_mode=mode,
        acceptance_criteria=['Only a new obligation'])
    assert not result['delegated']
    assert result['error_info']['details']['continuation_code'] == 'CONTINUATION_OBLIGATIONS_PENDING'
    assert result['error_info']['next_actions'][-1]['patch']['continuation_mode'] == 'remaining'
    assert root.delegated_contracts == before
    assert root.task_budget.snapshot()['used']['agents'] == agents


def test_completed_accepted_task_is_not_readded_to_every_future_turn(tmp_path_factory):
    root, tools, old = fixture(tmp_path_factory)
    old.status = ProcessStatus.COMPLETED
    old.result = {'status': 'completed'}
    root.child_reviews[old.process_id] = {'verdict': 'approved', 'summary': 'Verified original obligation'}
    with patch.object(root._manager, 'start'):
        result = delegate(tools, continue_process_id=old.process_id,
            acceptance_criteria=['Independent follow-up'])
    assert result['delegated'], result
    new = root._manager.get(result['process_id'])
    assert get_task_contract(new)['acceptance_criteria'] == ['Independent follow-up']
    assert root.delegated_contracts[new.process_id]['required_for_parent_completion']
    assert result['continuation']['mode'] == 'follow_up'
    assert not result['continuation']['retained_obligations']


@pytest.mark.parametrize('boundary', ['unreviewed', 'pending_closeout', 'checkpoint_failed', 'superseded'])
def test_completed_label_does_not_release_old_obligations(tmp_path_factory, boundary):
    root, tools, old = fixture(tmp_path_factory)
    old.status = ProcessStatus.COMPLETED
    old.result = {'status': 'completed'}
    root.child_reviews[old.process_id] = {'verdict': 'approved', 'summary': 'Verified'}
    if boundary == 'unreviewed':
        root.child_reviews.clear()
    elif boundary == 'pending_closeout':
        old.lifecycle_finalized = False
        old.durable_revision = 1
    elif boundary == 'checkpoint_failed':
        old.status = ProcessStatus.FAILED
        old.result = {'status': 'failed', 'code': 'SESSION_CHECKPOINT_FAILED'}
    else:
        root.delegated_contracts[old.process_id]['superseded_by'] = 'another-execution'
    result = delegate(tools, continue_process_id=old.process_id, continuation_mode='follow_up')
    assert not result['delegated'], result
    assert len(root.child_ids) == 1


@pytest.mark.parametrize('receipt', [
    {'effect': 'external', 'succeeded': True, 'committed': True},
    {'effect': 'process', 'succeeded': False, 'effect_state': 'ambiguous'},
])
def test_pending_external_effect_is_not_forced_to_replay(tmp_path_factory, receipt):
    root, tools, old = fixture(tmp_path_factory)
    old.tool_receipts.append(receipt)
    result = delegate(tools, continue_process_id=old.process_id)
    assert not result['delegated']
    assert result['error_info']['details']['continuation_code'] == 'CONTINUATION_EFFECT_RECONCILIATION_REQUIRED'
    assert len(root.child_ids) == 1 and 'superseded_by' not in root.delegated_contracts[old.process_id]


def test_fresh_session_supersession_cannot_bypass_retained_contract(tmp_path_factory):
    root, tools, old = fixture(tmp_path_factory)
    with patch.object(root._manager, 'start'):
        result = delegate(tools, supersedes_process_id=old.process_id, acceptance_criteria=['New'])
    assert result['delegated'], result
    new = root._manager.get(result['process_id'])
    assert new.session is not old.session
    assert get_task_contract(new)['acceptance_criteria'] == ['Original obligation', 'New']


def test_conflicting_required_call_bounds_reject_before_admission(tmp_path_factory):
    root, tools, old = fixture(tmp_path_factory,
        required_calls=[{'tool_name': 'read_file', 'min_calls': 2, 'max_calls': 3}])
    result = delegate(tools, continue_process_id=old.process_id,
        required_tool_calls=[{'tool_name': 'read_file', 'min_calls': 0, 'max_calls': 1}])
    assert not result['delegated']
    assert 'CONTINUATION_REQUIREMENTS_CONFLICT' in result['error_info']['message']
    assert len(root.child_ids) == 1


def test_direct_terminal_feedback_cannot_reset_pending_assignment_or_budget(tmp_path_factory):
    from backend.core.daemon.dispatch import _cmd_task
    root, tools, old = fixture(tmp_path_factory)
    before = deepcopy(root.delegated_contracts), deepcopy(old.session.messages), deepcopy(root.task_budget.snapshot()['used'])
    emitted = []
    _cmd_task({'action': 'chat', 'process_id': old.process_id, 'task_id': 'fresh-user-turn',
        'instruction': 'Also inspect the related behavior', 'fresh_task_budget': True},
        SimpleNamespace(workspace_path=tmp_path_factory), SimpleNamespace(name='test'),
        {'apm': root._manager, 'dispatcher': object(), 'evq': Queue()}, emitted.append)
    assert emitted[-1]['code'] == 'CONTINUATION_OBLIGATIONS_PENDING'
    assert emitted[-1]['owner_process_id'] == root.process_id
    assert root.delegated_contracts == before[0]
    assert deepcopy(old.session.messages) == before[1]
    after = root.task_budget.snapshot()['used']
    assert {k: v for k, v in after.items() if k != 'elapsed_seconds'} == {k: v for k, v in before[2].items() if k != 'elapsed_seconds'}
    assert len(root.child_ids) == 1
    old.status = ProcessStatus.COMPLETED
    old.result = {'status': 'completed'}
    root.child_reviews[old.process_id] = {'verdict': 'approved', 'summary': 'Verified all original obligations'}
    assert require_direct_followup(old, root._manager)['continuation']['mode'] == 'follow_up'


@pytest.mark.parametrize('completed', [False, True])
def test_explicit_optional_successor_cannot_silently_retire_parent_obligation(tmp_path_factory, completed):
    root, tools, old = fixture(tmp_path_factory)
    if completed:
        old.status = ProcessStatus.COMPLETED
        old.result = {'status': 'completed'}
        root.child_reviews[old.process_id] = {'verdict': 'approved', 'summary': 'Verified'}
    result = delegate(tools, continue_process_id=old.process_id, required_for_parent_completion=False)
    assert not result['delegated']
    assert result['error_info']['details']['continuation_code'] == 'CONTINUATION_PARENT_OBLIGATION_PENDING'
    assert len(root.child_ids) == 1


def test_omitted_parent_requirement_retains_an_optional_predecessor(tmp_path_factory):
    root, tools = _supervisor(tmp_path_factory)
    with patch.object(root._manager, 'start'):
        first = delegate(tools, required_for_parent_completion=False)
    assert first['delegated'], first
    old = root._manager.get(first['process_id'])
    old.status = ProcessStatus.FAILED
    old.result = {'status': 'failed'}
    old.lifecycle_finalized = True
    assert get_task_contract(root).get('minimum_delegated_outcomes', 0) == 0
    with patch.object(root._manager, 'start'):
        result = delegate(tools, continue_process_id=old.process_id)
    assert result['delegated'], result
    assert not root.delegated_contracts[result['process_id']]['required_for_parent_completion']
    root_contract = get_task_contract(root)
    assert root_contract.get('minimum_delegated_outcomes', 0) == 0
    assert root_contract['delegation_attempts'][-1]['required_for_parent_completion'] is False


def test_retained_interfaces_keep_their_frozen_definitions(tmp_path_factory):
    from backend.core.loop.interface_contract import capture_declared_contract, verify_declared_contract
    root, tools, old = fixture(tmp_path_factory)
    source = tmp_path_factory / 'api.py'
    source.write_text('def fetch(value):\n    return value\n')
    frozen = capture_declared_contract(tmp_path_factory, [{'node_id': 'original', 'input_interfaces': ['api.py:fetch']}])
    old_contract = root.delegated_contracts[old.process_id]
    old_contract.update(input_interfaces=['api.py:fetch'], interface_contract=frozen)
    source.write_text('def fetch(value, changed):\n    return value\n')
    with patch.object(root._manager, 'start'):
        result = delegate(tools, continue_process_id=old.process_id)
    assert result['delegated'], result
    contract = root.delegated_contracts[result['process_id']]
    assert contract['input_interfaces'] == ['api.py:fetch']
    assert contract['interface_contract']['interfaces'] == frozen['interfaces']
    assert verify_declared_contract(contract['interface_contract'], tmp_path_factory, ['api.py:fetch'])


def test_redeclared_interface_cannot_silently_refresh_the_old_boundary(tmp_path_factory):
    from backend.core.loop.interface_contract import capture_declared_contract
    root, tools, old = fixture(tmp_path_factory)
    source = tmp_path_factory / 'api.py'
    source.write_text('def fetch(value):\n    return value\n')
    root.delegated_contracts[old.process_id].update(input_interfaces=['api.py:fetch'],
        interface_contract=capture_declared_contract(tmp_path_factory, [{'input_interfaces': ['api.py:fetch']}]))
    source.write_text('def fetch(value, changed):\n    return value\n')
    result = delegate(tools, continue_process_id=old.process_id, input_interfaces=['api.py:fetch'])
    assert not result['delegated']
    assert 'CONTINUATION_INTERFACE_CONFLICT' in result['error_info']['message']
    assert len(root.child_ids) == 1


def test_cold_accepted_owner_and_child_can_start_a_direct_followup(tmp_path_factory):
    from backend.core.loop.manager import AgentProcessManager, SessionStore
    from backend.core.loop.conversation_continuation import resolve_conversation_process
    root, tools, old = fixture(tmp_path_factory)
    old.status = ProcessStatus.COMPLETED
    old.result = {'status': 'completed'}
    root.child_reviews[old.process_id] = {'verdict': 'approved', 'summary': 'Verified original'}
    root.status = ProcessStatus.COMPLETED
    root.result = {'status': 'completed'}
    root.lifecycle_finalized = True
    store = SessionStore(str(tmp_path_factory), state_home=tmp_path_factory / 'state')
    try:
        for process in (root, old):
            store.save_process_checkpoint(process)
            store.append_event(process.process_id, 'agent_complete', {'status': 'completed'})
        cold = AgentProcessManager()
        restored = resolve_conversation_process(cold, store, tmp_path_factory, old.process_id)
        assert cold.get(root.process_id) is not None
        assert require_direct_followup(restored, cold)['continuation']['prior_outcome_accepted']
    finally:
        store.close()


def test_continuation_preflight_is_readonly_and_deferred_start_retains_contract(tmp_path_factory):
    root, tools, old = fixture(tmp_path_factory)
    args = {'task_description': 'Additional investigation', 'capability_profile_id': 'governance.observe',
        'task_kind': 'answer', 'target_files': [], 'acceptance_criteria': ['New'], 'continue_process_id': old.process_id}
    before = deepcopy(root.delegated_contracts), deepcopy(root.context_snapshot), deepcopy(old.session.messages)
    budget = root.task_budget.snapshot()['used']
    result = tools['delegate_task'].execute(args, validate_only=True)
    assert result['validated'], result
    assert root.delegated_contracts == before[0] and root.context_snapshot == before[1]
    assert old.session.messages == before[2]
    after = root.task_budget.snapshot()['used']
    assert {k: v for k, v in after.items() if k != 'elapsed_seconds'} == {k: v for k, v in budget.items() if k != 'elapsed_seconds'}
    with patch.object(root._manager, 'start') as start:
        result = tools['delegate_task'].execute(args, start_immediately=False)
    assert result['delegated'] and result['execution_state'] == 'prepared'
    start.assert_not_called()
    new = root._manager.get(result['process_id'])
    assert new.runtime_preferences['execution_prepared']
    assert get_task_contract(new)['acceptance_criteria'] == ['Original obligation', 'New']
    assert not new.capability_lease and not new.approval_grants and not new.tool_receipts
    assert new.task_budget is old.task_budget


def test_dag_failure_after_continuation_preflight_cannot_retire_old_obligations(tmp_path_factory):
    root, tools, old = fixture(tmp_path_factory)
    nodes = [{'node_id': 'continued', 'task_description': 'Additional investigation',
        'capability_profile_id': 'governance.observe', 'task_kind': 'answer', 'target_files': [],
        'acceptance_criteria': ['New'], 'continue_process_id': old.process_id},
        {'node_id': 'bad', 'task_description': 'Invalid later node', 'capability_profile_id': 'governance.observe',
        'task_kind': 'answer', 'target_files': [], 'acceptance_criteria': ['New'],
        'required_tool_calls': [{'tool_name': 'read_file', 'min_calls': -1}]}]
    before = deepcopy(root.delegated_contracts)
    used = root.task_budget.snapshot()['used']
    with patch.object(root._manager, 'start') as start:
        result = tools['delegate_task_dag'].execute({'nodes': nodes})
    assert not result['delegated'] and result['execution_state'] == 'not_started'
    start.assert_not_called()
    assert root.delegated_contracts == before and len(root.child_ids) == 1
    after = root.task_budget.snapshot()['used']
    assert {k: v for k, v in after.items() if k != 'elapsed_seconds'} == {k: v for k, v in used.items() if k != 'elapsed_seconds'}


def test_empty_retained_tool_scope_does_not_expand_to_profile_defaults(tmp_path_factory):
    from backend.core.loop.tools import ToolRegistry
    root, tools, old = fixture(tmp_path_factory)
    old.tool_registry = ToolRegistry([])
    root.delegated_contracts[old.process_id]['allowed_tools'] = []
    with patch.object(root._manager, 'start'):
        result = delegate(tools, continue_process_id=old.process_id)
    assert result['delegated'], result
    assert root._manager.get(result['process_id']).tool_registry.list_all() == []


def _direct_user_turn(manager, previous, workspace, *, store=None, task_id='direct-turn'):
    from backend.core.daemon.dispatch import _cmd_task
    emitted = []
    # Exercise real admission/persistence and only suppress provider-thread launch.
    with patch('backend.core.daemon.dispatch.threading.Thread.start'):
        _cmd_task({'action': 'chat', 'process_id': previous.process_id, 'task_id': task_id,
            'context_snapshot': {}, 'instruction': 'Explain the result further', 'fresh_task_budget': True},
            SimpleNamespace(workspace_path=workspace), SimpleNamespace(name='test'),
            {'apm': manager, 'dispatcher': object(), 'llm': object(),
             'session_store': store, 'evq': Queue()}, emitted.append)
    assert emitted[-1].get('result', {}).get('status') == 'pending', emitted
    result = manager.get(emitted[-1]['result']['process_id'])
    result._run_lock.release()
    return result


def _accepted_original(workspace):
    root, tools, old = fixture(workspace)
    old.status = ProcessStatus.COMPLETED
    old.result = {'status': 'completed'}
    root.child_reviews[old.process_id] = {'verdict': 'approved', 'summary': 'Verified original outcome'}
    return root, tools, old


def test_real_dispatch_repeated_independent_user_turns_do_not_reopen_parent_obligations(tmp_path_factory):
    from backend.core.loop.continuation_contract import direct_user_task_origin
    from backend.core.loop.completion_protocol import HostCompletionEvaluator
    root, tools, old = _accepted_original(tmp_path_factory)
    manager = root._manager
    root_contracts, root_children = deepcopy(root.delegated_contracts), list(root.child_ids)
    baseline = HostCompletionEvaluator.evaluate(root, 'original work delivered').reasons
    latest = old
    for index in range(3):
        successor = _direct_user_turn(manager, latest, tmp_path_factory, task_id=f'user-turn-{index}')
        assert successor.session is old.session
        assert successor.parent_id == root.process_id  # presentation identity only
        assert successor.task_budget is not root.task_budget
        assert set(successor.tool_registry.list_all()) <= set(old.tool_registry.list_all())
        assert direct_user_task_origin(successor)['accepted_source_process_id'] == old.process_id
        assert root.delegated_contracts == root_contracts and root.child_ids == root_children
        assert HostCompletionEvaluator.evaluate(root, 'original work delivered').reasons == baseline
        assert not successor.worktree  # no old published worktree is silently reopened
        successor.status = ProcessStatus.COMPLETED
        successor.result = {'status': 'completed'}
        successor.lifecycle_finalized = True
        latest = successor


def test_cold_independent_turn_uses_durable_host_ledger_and_does_not_rebuild_parent_obligation(tmp_path_factory):
    from backend.core.loop.manager import AgentProcessManager, SessionStore
    from backend.core.loop.conversation_continuation import resolve_conversation_process
    from backend.core.loop.continuation_contract import direct_user_task_origin
    root, tools, old = _accepted_original(tmp_path_factory)
    root.status = ProcessStatus.COMPLETED
    root.result = {'status': 'completed'}
    root.lifecycle_finalized = True
    store = SessionStore(str(tmp_path_factory), state_home=tmp_path_factory / 'state')
    try:
        for process in (root, old):
            store.save_process_checkpoint(process)
            store.append_event(process.process_id, 'agent_complete', {'status': 'completed'})
        direct = _direct_user_turn(root._manager, old, tmp_path_factory, store=store)
        direct.status = ProcessStatus.COMPLETED
        direct.result = {'status': 'completed'}
        direct.lifecycle_finalized = True
        store.save_process_checkpoint(direct)
        store.append_event(direct.process_id, 'agent_complete', {'status': 'completed'})
        cold = AgentProcessManager()
        restored = resolve_conversation_process(cold, store, tmp_path_factory, direct.process_id)
        assert direct_user_task_origin(restored)['accepted_source_process_id'] == old.process_id
        owner = cold.get(root.process_id)
        assert restored.task_budget is not owner.task_budget
        assert restored.task_budget.snapshot()['task_id'] == direct.task_budget.snapshot()['task_id']
        assert set(restored.tool_registry.list_all()) <= set(old.tool_registry.list_all())
        assert direct.process_id not in owner.child_ids
        assert direct.process_id not in owner.delegated_contracts
        successor = _direct_user_turn(cold, restored, tmp_path_factory, store=store, task_id='cold-next')
        assert successor.session is restored.session
        assert direct_user_task_origin(successor)['accepted_source_process_id'] == old.process_id
    finally:
        store.close()


def test_plain_runtime_flag_or_unrelated_ledger_record_cannot_release_unaccepted_delegation(tmp_path_factory):
    root, tools, old = fixture(tmp_path_factory)
    old.runtime_preferences['user_direct_continuation'] = True
    old.session.host_ledger.append({'event': 'direct_user_task_admitted',
        'process_id': 'unrelated', 'task_id': old.active_task_id,
        'accepted_source_process_id': old.process_id, 'owner_process_id': root.process_id})
    with pytest.raises(ValueError, match='CONTINUATION_OBLIGATIONS_PENDING'):
        require_direct_followup(old, root._manager)


def test_independent_user_turn_still_requires_reliable_closeout(tmp_path_factory):
    root, tools, old = _accepted_original(tmp_path_factory)
    direct = _direct_user_turn(root._manager, old, tmp_path_factory)
    direct.status = ProcessStatus.COMPLETED
    direct.result = {'status': 'completed'}
    direct.lifecycle_finalized = False
    with pytest.raises(ValueError, match='CONTINUATION_CLOSEOUT_PENDING'):
        require_direct_followup(direct, root._manager)


@pytest.mark.parametrize('cold', [False, True])
def test_latest_explicit_supervisor_review_is_used_for_direct_continuation(tmp_path_factory, cold):
    from backend.core.loop.manager import AgentProcessManager, SessionStore
    from backend.core.loop.models import RingLevel
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.conversation_continuation import resolve_conversation_process
    root, tools, old = fixture(tmp_path_factory)
    manager = root._manager
    old.status = ProcessStatus.COMPLETED
    old.result = {'status': 'completed'}
    root.status = ProcessStatus.COMPLETED
    root.result = {'status': 'completed'}
    root.lifecycle_finalized = True
    successor = manager.fork(None, 'supervisor', ToolRegistry([]), 5, RingLevel.RING_0,
        session=root.session, task_id='next-root', actor_kind='supervisor', workspace_path=str(tmp_path_factory))
    manager.inherit_terminal_coordination(successor, root)
    successor.child_reviews[old.process_id] = {'verdict': 'approved', 'summary': 'Current supervisor verified'}
    successor.status = ProcessStatus.COMPLETED
    successor.result = {'status': 'completed'}
    successor.lifecycle_finalized = True
    store = SessionStore(str(tmp_path_factory), state_home=tmp_path_factory / 'state') if cold else None
    try:
        if cold:
            for process in (root, old, successor):
                store.save_process_checkpoint(process)
                store.append_event(process.process_id, 'agent_complete', {'status': 'completed'})
            manager = AgentProcessManager()
            old = resolve_conversation_process(manager, store, tmp_path_factory, old.process_id)
        result = require_direct_followup(old, manager, store=store, workspace=tmp_path_factory)
        assert result['direct_user_origin']['owner_process_id'] == successor.process_id
        assert manager.get(successor.process_id).child_reviews[old.process_id]['verdict'] == 'approved'
        manager.get(successor.process_id).delegated_contracts[old.process_id]['superseded_by'] = 'newer-child'
        with pytest.raises(ValueError, match='CONTINUATION_ALREADY_SUPERSEDED'):
            require_direct_followup(old, manager, store=store, workspace=tmp_path_factory)
    finally:
        if store:
            store.close()


def test_recovery_budget_root_stops_at_independent_user_task_for_descendants():
    from backend.core.loop.recovery import _root_id
    states = {
        'A': {'process': {'process_id': 'A', 'parent_process_id': None}},
        'user': {'process': {'process_id': 'user', 'parent_process_id': 'A', 'task_id': 'user-task'},
            'session': {'host_ledger': [{'event': 'direct_user_task_admitted',
                'process_id': 'user', 'task_id': 'user-task', 'accepted_source_process_id': 'B',
                'owner_process_id': 'A'}]}},
        'descendant': {'process': {'process_id': 'descendant', 'parent_process_id': 'user'}},
    }
    assert _root_id('user', states) == 'user'
    assert _root_id('descendant', states) == 'user'


@pytest.mark.parametrize('accepted', [False, True])
@pytest.mark.parametrize('finalized', [False, True])
def test_terminal_worker_adoption_requires_explicit_handoff_without_mutating_saved_change(tmp_path_factory, accepted, finalized):
    """B BTW handoff is deliberately unsupported, even after A accepted B.

    This boundary must not silently acquire a new budget, inherit old receipts
    into a fresh independent task, or discard worktree/assignment responsibility.
    """
    from backend.core.daemon.dispatch import _cmd_task
    from backend.core.loop.task_maintenance import KEY
    root, tools, old = _accepted_original(tmp_path_factory) if accepted else fixture(tmp_path_factory)
    old.lifecycle_finalized = finalized
    old.durable_revision = 1  # unfinalized cannot use the legacy synchronous fixture allowance
    change = {'status': 'pending', 'content': 'Also handle this case',
              'target_process_id': old.process_id, 'target_task_id': old.active_task_id}
    old.update_context_snapshot(lambda context: {**context, KEY: {
        'task_id': old.active_task_id, 'process_id': old.process_id, 'epoch': 1,
        'changes': {'saved-change': change}}})
    old.tool_receipts = [{'receipt_id': 'existing-effect', 'task_id': old.active_task_id}]
    before = deepcopy((old.read_context_snapshot()[0], old.session.host_ledger,
        old.tool_receipts, root.delegated_contracts, root.child_ids, root.task_budget.snapshot()['used']))
    emitted = []
    with patch.object(root._manager, 'fork') as fork:
        _cmd_task({'action': 'chat', 'process_id': old.process_id, 'task_id': old.active_task_id,
            'instruction': 'Adopt saved change', 'fresh_task_budget': True,
            'adopted_change_origin': {'process_id': old.process_id,
                'task_id': old.active_task_id, 'change_id': 'saved-change'}},
            SimpleNamespace(workspace_path=tmp_path_factory), SimpleNamespace(name='test'),
            {'apm': root._manager, 'dispatcher': object(), 'evq': Queue()}, emitted.append)
    expected = 'CONTINUATION_ADOPTION_HANDOFF_REQUIRED' if finalized else 'CONTINUATION_CLOSEOUT_PENDING'
    assert emitted[-1]['code'] == expected
    assert emitted[-1]['owner_process_id'] == root.process_id
    fork.assert_not_called()
    after = deepcopy((old.read_context_snapshot()[0], old.session.host_ledger,
        old.tool_receipts, root.delegated_contracts, root.child_ids, root.task_budget.snapshot()['used']))
    assert after[:-1] == before[:-1]
    assert {k: v for k, v in after[-1].items() if k != 'elapsed_seconds'} == {
        k: v for k, v in before[-1].items() if k != 'elapsed_seconds'}
    assert len(root._manager.list_processes()) == 2


def test_root_supervisor_adoption_is_not_subject_to_worker_handoff_block(tmp_path_factory):
    from backend.core.daemon.dispatch import _cmd_task
    from backend.core.loop.task_maintenance import KEY
    root, tools = _supervisor(tmp_path_factory)
    root.status = ProcessStatus.CANCELLED
    root.result = {'status': 'cancelled'}
    root.lifecycle_finalized = True
    root.update_context_snapshot(lambda context: {**context, KEY: {
        'task_id': root.active_task_id, 'process_id': root.process_id, 'epoch': 1,
        'changes': {'saved-change': {'status': 'pending', 'content': 'Also handle this case',
            'target_process_id': root.process_id, 'target_task_id': root.active_task_id}}}})
    emitted = []
    with patch('backend.core.daemon.dispatch.threading.Thread.start'):
        _cmd_task({'action': 'chat', 'process_id': root.process_id, 'task_id': root.active_task_id,
            'instruction': 'Adopt saved change', 'context_snapshot': {}, 'fresh_task_budget': False,
            'adopted_change_origin': {'process_id': root.process_id,
                'task_id': root.active_task_id, 'change_id': 'saved-change'}},
            SimpleNamespace(workspace_path=tmp_path_factory), SimpleNamespace(name='test'),
            {'apm': root._manager, 'dispatcher': object(), 'llm': object(), 'evq': Queue()}, emitted.append)
    assert emitted[-1].get('result', {}).get('status') == 'pending', emitted
    successor = root._manager.get(emitted[-1]['result']['process_id'])
    successor._run_lock.release()
    assert successor.active_task_id == root.active_task_id
    assert successor.session is root.session
    assert successor.read_context_snapshot()[0][KEY]['changes']['saved-change']['status'] == 'pending'
    assert not any(event['event'] == 'direct_user_task_admitted' for event in successor.session.host_ledger)
