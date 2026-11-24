"""Bounded product renewals share the original budget and checkpoint authority."""
from types import SimpleNamespace
import pytest

from backend.core.loop.budget import TaskTreeBudget, TaskBudgetExceeded
from backend.core.loop.execution_allowance import request_extension, notify_boundary
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import RingLevel
from backend.core.loop.tools import ToolRegistry


def account():
    budget = TaskTreeBudget.create('bounded', {'max_provider_calls':800, 'max_seconds':300})
    budget.register_process('root', parent_id=None, actor_kind='supervisor', max_steps=150)
    budget.configure_step_schedule('root', [150,250,150,50])
    return budget


def test_four_segments_are_additional_and_exhaust_after_three_renewals():
    budget = account()
    budget.begin_provider_call('root')
    budget.consume_output('already used', 'root')
    original_used = budget.snapshot()['used']
    for used, limit in [(150,400),(400,550),(550,600)]:
        result = budget.extend_step_allowance('root', steps_used=used,
            reason='Complete remaining verification', remaining_work='Verify and deliver')
        assert result['limit'] == limit
    assert budget.step_allowance('root')['renewals_used'] == 3
    assert budget.snapshot()['used']['provider_calls'] == original_used['provider_calls']
    assert budget.snapshot()['used']['estimated_output_tokens'] == original_used['estimated_output_tokens']
    with pytest.raises(TaskBudgetExceeded) as error:
        budget.extend_step_allowance('root', steps_used=600, reason='again', remaining_work='again')
    assert error.value.code == 'EXECUTION_STEP_RENEWALS_EXHAUSTED'


def test_snapshot_restore_preserves_segments_and_consumed_counts():
    budget = account()
    budget.begin_provider_call('root')
    budget.extend_step_allowance('root', steps_used=140, reason='work', remaining_work='verify')
    restored = TaskTreeBudget.from_snapshot(budget.snapshot())
    assert restored.step_allowance('root') == budget.step_allowance('root')
    assert restored.snapshot()['used']['provider_calls'] == 1
    with pytest.raises(ValueError, match='replaced'):
        restored.configure_step_schedule('root', [400,50])


def test_fixed_tests_children_early_requests_and_hard_limits_are_separate():
    budget = account()
    with pytest.raises(TaskBudgetExceeded) as early:
        budget.extend_step_allowance('root', steps_used=20, reason='work', remaining_work='verify')
    assert early.value.code == 'EXECUTION_STEP_RENEWAL_TOO_EARLY'
    budget.register_process('child', parent_id='root', actor_kind='worker', max_steps=50)
    with pytest.raises(TaskBudgetExceeded) as child:
        budget.extend_step_allowance('child', steps_used=49, reason='work', remaining_work='verify')
    assert child.value.code == 'EXECUTION_STEP_RENEWAL_DISABLED'
    fixed = TaskTreeBudget.create('fixed')
    fixed.register_process('test', parent_id=None, actor_kind='supervisor', max_steps=50)
    with pytest.raises(TaskBudgetExceeded) as test:
        fixed.extend_step_allowance('test', steps_used=49, reason='work', remaining_work='verify')
    assert test.value.code == 'EXECUTION_STEP_RENEWAL_DISABLED'
    budget._provider_calls = budget.limits.max_provider_calls
    with pytest.raises(TaskBudgetExceeded) as calls:
        budget.extend_step_allowance('root', steps_used=149, reason='work', remaining_work='verify')
    assert calls.value.code == 'TASK_TREE_PROVIDER_BUDGET_EXHAUSTED'


def process():
    manager = AgentProcessManager()
    root = manager.fork(parent_id=None, role='supervisor', actor_kind='supervisor',
        ring_level=RingLevel.RING_0, tool_registry=ToolRegistry([]), max_steps=150,
        task_id='renewal', task_budget_limits={'max_provider_calls':800},
        runtime_preferences={'execution_step_schedule':[150,250,150,50]})
    root.steps_used = 145
    return manager, root


def test_renewal_commits_same_process_and_known_storage_failure_rolls_back():
    manager, root = process()
    saved = []
    manager.session_store = SimpleNamespace(save_process_checkpoint=lambda p: saved.append(p.max_steps))
    result = request_extension(root, {'reason':'Tests remain', 'remaining_work':'Run existing tests'})
    assert result['accepted'] and root.max_steps == 400 and saved == [400]
    root.steps_used = 390
    def fail(_):
        raise OSError('checkpoint refused')
    manager.session_store = SimpleNamespace(save_process_checkpoint=fail)
    with pytest.raises(OSError, match='checkpoint refused'):
        request_extension(root, {'reason':'Verification', 'remaining_work':'Inspect the remaining result'})
    assert root.max_steps == 400
    assert root.task_budget.step_allowance(root.process_id)['renewals_used'] == 1


def test_boundary_reminder_is_once_per_segment_and_does_not_spend_a_call():
    _, root = process()
    emitted = []
    before = root.task_budget.snapshot()['used']['provider_calls']
    notify_boundary(root, root.session, emitted.append)
    notify_boundary(root, root.session, emitted.append)
    assert len(emitted) == 1 and emitted[0]['renewal_available']
    assert root.task_budget.snapshot()['used']['provider_calls'] == before


def test_child_does_not_inherit_root_renewal_schedule():
    manager, root = process()
    child = manager.fork(parent_id=root.process_id, role='worker', actor_kind='worker',
        ring_level=RingLevel.RING_3, tool_registry=ToolRegistry([]), max_steps=50)
    assert 'execution_step_schedule' not in child.runtime_preferences
    assert child.task_budget is root.task_budget
    assert child.task_budget.step_allowance(child.process_id)['maximum_renewals'] == 0


def test_uncertain_checkpoint_preserves_staged_allowance_but_blocks_more_work():
    from backend.core.storage import StorageCommitUnknown
    manager, root = process()
    def unknown(_):
        raise StorageCommitUnknown('execution-renewal')
    manager.session_store = SimpleNamespace(save_process_checkpoint=unknown)
    with pytest.raises(StorageCommitUnknown):
        request_extension(root, {'reason':'Verify', 'remaining_work':'Run tests'})
    assert root.max_steps == 400 and root.session.mechanism_activation_blocked
    root.steps_used = 390
    assert request_extension(root, {'reason':'Again', 'remaining_work':'Run tests'})['error'] == 'EXECUTION_STEP_RENEWAL_PERSISTENCE_BLOCKED'


def test_custom_counts_or_foreign_identity_are_not_accepted():
    _, root = process()
    result = request_extension(root, {'reason':'Work', 'remaining_work':'Tests', 'steps':900, 'process_id':'foreign'})
    assert result['error'] == 'EXECUTION_STEP_RENEWAL_INVALID'
    assert root.max_steps == 150


def test_normal_executor_continues_after_model_requested_renewal():
    import json
    from backend.core.loop.executor import agent_step
    manager = AgentProcessManager()
    root = manager.fork(parent_id=None, role='supervisor', actor_kind='supervisor',
        ring_level=RingLevel.RING_0, tool_registry=ToolRegistry(['capability_status','task_mechanism']),
        capability_profile_id='supervisor.control', max_steps=5, task_kind='answer',
        task_id='loop-renewal', task_budget_limits={'max_provider_calls':50},
        runtime_preferences={'execution_step_schedule':[5,4,3,1]})
    class Provider:
        calls = 0
        def stream_chat(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls <= 5:
                name = 'task_mechanism' if self.calls in {1,2,4} else 'capability_status'
                arguments = ({'operation':'extend_execution','reason':'The current check remains useful',
                              'remaining_work':'Return the inspected result'} if self.calls == 4
                             else {'operation':'status'} if self.calls in {1,2} else {})
                yield {'choices':[{'delta':{'tool_calls':[{'index':0,'id':f'call-{self.calls}',
                    'function':{'name':name,'arguments':json.dumps(arguments)}}]}}]}
            else:
                yield {'choices':[{'delta':{'content':'The inspected result is ready.'}}]}
    provider = Provider()
    outcome = agent_step(root, provider, instruction='Inspect current capability state and reply.')
    assert outcome['status'] == 'completed', [
        (m.get('data') or {}).get('error_info', {}).get('details')
        for m in root.session.messages if m.get('_tool_name') == 'task_mechanism']
    assert root.steps_used == 6 and root.max_steps == 9 and provider.calls == 6
    assert root.task_budget.step_allowance(root.process_id)['renewals_used'] == 1


def test_real_checkpoint_restores_renewal_without_new_allocation(tmp_path_factory):
    from backend.core.loop.manager import SessionStore
    _, root = process()
    workspace = tmp_path_factory / 'workspace'
    workspace.mkdir()
    store = SessionStore(str(workspace), state_home=tmp_path_factory / 'private-state')
    try:
        root._manager.session_store = store
        result = request_extension(root, {'reason':'Verify remaining work', 'remaining_work':'Run tests'})
        assert result['accepted']
        saved = store.load_process_state(root.process_id)
        assert saved['process']['max_steps'] == 400
        restored = TaskTreeBudget.from_snapshot(saved['runtime_state']['task_budget'])
        assert restored.step_allowance(root.process_id)['renewals_used'] == 1
        assert restored.step_allowance(root.process_id)['limit'] == 400
        assert restored.step_allowance(root.process_id)['maximum_steps'] == 600
    finally:
        store.close()
