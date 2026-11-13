"""DAG validation/registration must finish before the first worker can run."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.core.loop.executor import _build_internal_tools
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.tools import ToolRegistry
from tests.test_task_contract_recovery import _supervisor
from tests.test_process_closeout import _finish


def _nodes():
    return [{'node_id': name, 'task_description': 'Inspect file ' + name,
             'target_files': [], 'acceptance_criteria': ['Report facts'],
             'capability_profile_id': 'governance.observe', 'task_kind': 'answer',
             'depends_on': []} for name in ['a', 'b']]


@pytest.mark.parametrize('invalid', [
    {'required_tool_calls': [{'tool_name': 'read_file', 'min_calls': -1}]},
    {'required_tool_calls': [{'tool_name': 'read_file', 'min_calls': 2, 'max_calls': 1}]},
    {'required_tool_calls': [{'tool_name': 'not_an_authorized_tool'}]},
    {'required_test_ids': ['test-needing-run-test-authority']},
    {'continue_process_id': 'missing-owner'},
    {'supersedes_process_id': 'missing-owner'},
    {'budget_request': {'provider_calls': 'invalid'}},
])
def test_all_direct_admission_checks_run_before_any_dag_child_is_created(tmp_path_factory, invalid):
    root, tools = _supervisor(tmp_path_factory)
    nodes = _nodes()
    nodes[1].update(invalid)
    before = root.task_budget.snapshot()['used']
    with patch.object(root._manager, 'start') as start:
        result = tools['delegate_task_dag'].execute({'nodes': nodes})
    assert result['delegated'] is False
    assert result['execution_state'] == 'not_started'
    start.assert_not_called()
    assert not root.child_ids
    after = root.task_budget.snapshot()['used']
    assert {k: v for k, v in after.items() if k != 'elapsed_seconds'} == {k: v for k, v in before.items() if k != 'elapsed_seconds'}


def test_complete_prepared_graph_is_observed_before_any_child_starts(tmp_path_factory):
    root, _ = _supervisor(tmp_path_factory)
    manager = root._manager
    events = []

    def observe(event):
        events.append(event)
        if event['event'] == 'agent_dag_admitted':
            assert event['execution_state'] == 'prepared'
            assert len(root.child_ids) == 2
            assert not manager._threads
            assert all(manager.get(pid).status == ProcessStatus.WAITING for pid in root.child_ids)
            attempts = root.read_context_snapshot()[0]['task_contract']['delegation_attempts']
            assert [item['state'] for item in attempts] == ['prepared', 'prepared']

    tools = _build_internal_tools(root, {}, str(tmp_path_factory), object(), SimpleNamespace(_executors={}), observe)
    nodes = _nodes()
    nodes[1]['depends_on'] = ['a']
    # Duplicate semantic intent must still track two distinct child executions.
    nodes[0]['task_description'] = nodes[1]['task_description'] = 'Report shared facts'
    with patch('backend.core.loop.executor.agent_step', lambda **kw: _finish(kw['process']).result):
        result = tools['delegate_task_dag'].execute({'nodes': nodes})
        assert result['delegated'], result
        for child_id in root.child_ids:
            manager._threads[child_id].join(3)
        assert all(manager.get(pid).status == ProcessStatus.COMPLETED for pid in root.child_ids)
    assert next(i for i, e in enumerate(events) if e['event'] == 'agent_dag_admitted') < next(
        i for i, e in enumerate(events) if e['event'] == 'agent_started')
    attempts = root.read_context_snapshot()[0]['task_contract']['delegation_attempts']
    assert [item['state'] for item in attempts] == ['running', 'running']


def test_runtime_admission_failure_cancels_only_unstarted_allocations(tmp_path_factory):
    root, tools = _supervisor(tmp_path_factory)
    manager = root._manager
    original = manager.fork
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError('concurrent admission consumed remaining capacity')
        return original(*args, **kwargs)

    with patch.object(manager, 'fork', fail_second), patch.object(manager, 'start') as start:
        result = tools['delegate_task_dag'].execute({'nodes': _nodes()})
    assert not result['delegated'] and result['execution_state'] == 'not_started'
    start.assert_not_called()
    assert not result['started_process_ids']
    assert len(result['cancelled_before_start_process_ids']) == 1
    child = manager.get(result['cancelled_before_start_process_ids'][0])
    assert child.status == ProcessStatus.CANCELLED
    assert child.budget_lease['state'] == 'released'
    assert child.budget_lease['used_provider_calls'] == 0
    assert child.budget_lease['refunded_provider_calls'] == child.budget_lease['reserved_provider_calls']
    assert child.lifecycle_finalized
    # The admitted identity remains in history; no usage is silently reset.
    assert root.task_budget.snapshot()['used']['agents'] == 2
    assert root.task_budget.snapshot()['used']['provider_calls'] == 0


@pytest.mark.parametrize('failure_boundary', ['second_before_launch', 'first_after_launch'])
def test_start_failure_preserves_already_started_work_and_required_outcome(tmp_path_factory, failure_boundary):
    root, tools = _supervisor(tmp_path_factory)
    manager = root._manager
    original = manager.start
    starts = []

    def start(*args, **kwargs):
        if starts:
            raise RuntimeError('injected scheduler start failure')
        starts.append(args[0])
        original(*args, **kwargs)
        manager._threads[args[0]].join(3)
        if failure_boundary == 'first_after_launch':
            raise RuntimeError('injected adapter failure after real thread launch')

    def model(**kwargs):
        child = kwargs['process']
        child.task_budget.begin_provider_call(child.process_id)
        return _finish(child).result

    with patch.object(manager, 'start', start), patch('backend.core.loop.executor.agent_step', model):
        result = tools['delegate_task_dag'].execute({'nodes': _nodes()})
    assert result['execution_state'] == 'partially_started'
    assert result['started_process_ids'] == starts
    assert len(result['cancelled_before_start_process_ids']) == 1
    first = manager.get(starts[0])
    assert first.status == ProcessStatus.COMPLETED
    assert root.delegated_contracts[first.process_id]['required_for_parent_completion']
    assert not root.delegated_contracts[first.process_id].get('admission_rolled_back')
    assert root.task_budget.snapshot()['used']['provider_calls'] == 1
    second_id = result['cancelled_before_start_process_ids'][0]
    assert root.delegated_contracts[second_id]['required_for_parent_completion']
    assert not root.delegated_contracts[second_id]['admission_rolled_back']
    assert tools['review_child_outcome'].execute({'process_id': first.process_id,
        'verdict': 'approved', 'summary': 'First requested outcome is correct'})['accepted']
    # The first success cannot erase the unstarted second obligation.
    from backend.core.loop.completion_protocol import HostCompletionEvaluator
    evaluation = HostCompletionEvaluator.evaluate(root, 'All requested work completed')
    assert not evaluation.allowed
    assert any(second_id in reason for reason in evaluation.reasons)
    assert _finish(root).retry
    assert root.status != ProcessStatus.COMPLETED


def test_prestart_checkpoint_failure_never_launches_workers(tmp_path_factory):
    root, _ = _supervisor(tmp_path_factory)
    def observe(event):
        if event['event'] == 'agent_dag_admitted':
            raise RuntimeError('injected complete graph checkpoint failure')
    tools = _build_internal_tools(root, {}, str(tmp_path_factory), object(), SimpleNamespace(_executors={}), observe)
    with patch.object(root._manager, 'start') as start:
        result = tools['delegate_task_dag'].execute({'nodes': _nodes()})
    assert result['execution_state'] == 'not_started'
    assert len(result['cancelled_before_start_process_ids']) == 2
    start.assert_not_called()


def test_aborted_continuation_restores_original_completion_obligation(tmp_path_factory):
    root, tools = _supervisor(tmp_path_factory)
    manager = root._manager
    previous = manager.fork(root.process_id, 'executor', ToolRegistry([]), 4, RingLevel.RING_3,
                            capability_profile_id='governance.observe')
    previous.status = ProcessStatus.COMPLETED
    previous.lifecycle_finalized = True
    root.register_child_contract(previous.process_id, {'required_for_parent_completion': True, 'task_kind': 'answer'})
    nodes = _nodes()
    nodes[0]['continue_process_id'] = previous.process_id
    original = manager.fork
    count = 0
    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError('injected allocation failure after continuation admission')
        return original(*args, **kwargs)
    with patch.object(manager, 'fork', fail_second):
        result = tools['delegate_task_dag'].execute({'nodes': nodes})
    assert result['execution_state'] == 'not_started'
    assert len(result['cancelled_before_start_process_ids']) == 1
    assert root.delegated_contracts[previous.process_id]['required_for_parent_completion']
    assert not root.delegated_contracts[previous.process_id].get('superseded_by')


def test_dag_checkpoint_contains_all_siblings_before_first_model_call(tmp_path_factory):
    from queue import Queue
    from backend.core.daemon.task_observations import TaskObservations
    from backend.core.loop.manager import SessionStore
    root, _ = _supervisor(tmp_path_factory)
    manager = root._manager
    store = SessionStore(str(tmp_path_factory), state_home=tmp_path_factory / 'state')
    observer = TaskObservations(root, manager, store,
        SimpleNamespace(append=lambda event, **kw: event), Queue())
    tools = _build_internal_tools(root, {}, str(tmp_path_factory), object(),
        SimpleNamespace(_executors={}), observer.persist)
    inspected = []
    def model(**kwargs):
        child = kwargs['process']
        assert len(root.child_ids) == 2
        for child_id in root.child_ids:
            assert store.load_process_state(child_id) is not None
        assert not child.runtime_preferences.get('execution_prepared')
        inspected.append(child.process_id)
        return _finish(child).result
    try:
        with patch('backend.core.loop.executor.agent_step', model):
            result = tools['delegate_task_dag'].execute({'nodes': _nodes()})
            assert result['delegated'], result
            for child_id in root.child_ids:
                manager._threads[child_id].join(3)
            assert len(inspected) == 2
            assert all(manager.get(child_id).status == ProcessStatus.COMPLETED for child_id in root.child_ids)
    finally:
        for thread in manager._threads.values():
            thread.join(3)
        store.close()


def test_failed_cancellation_checkpoint_is_reported_and_other_allocations_are_released(tmp_path_factory):
    root, _ = _supervisor(tmp_path_factory)
    def observe(event):
        if event['event'] in {'agent_dag_admitted', 'agent_terminal', 'agent_dag_admission_failed'}:
            raise RuntimeError('injected persistent storage outage')
    tools = _build_internal_tools(root, {}, str(tmp_path_factory), object(), SimpleNamespace(_executors={}), observe)
    with patch.object(root._manager, 'start') as start:
        result = tools['delegate_task_dag'].execute({'nodes': _nodes()})
    assert result['execution_state'] == 'not_started'
    assert len(result['cleanup_errors']) == 2
    assert result['persistence_error']
    start.assert_not_called()
    for child_id in root.child_ids:
        child = root._manager.get(child_id)
        assert not child.lifecycle_finalized
        assert child.result['code'] == 'SESSION_CHECKPOINT_FAILED'
        assert child.budget_lease['state'] == 'released'


def test_duplicate_continuation_in_same_dag_is_rejected_without_admission(tmp_path_factory):
    root, tools = _supervisor(tmp_path_factory)
    nodes = _nodes()
    for node in nodes:
        node['continue_process_id'] = 'same-predecessor'
    with patch.object(root._manager, 'fork') as fork:
        result = tools['delegate_task_dag'].execute({'nodes': nodes})
    assert result['execution_state'] == 'not_started'
    fork.assert_not_called()


def test_failure_before_any_thread_starts_rolls_back_only_new_obligations(tmp_path_factory):
    root, tools = _supervisor(tmp_path_factory)
    with patch.object(root._manager, 'start', side_effect=RuntimeError('launch unavailable')):
        result = tools['delegate_task_dag'].execute({'nodes': _nodes()})
    assert result['execution_state'] == 'not_started'
    assert not result['started_process_ids']
    assert len(result['cancelled_before_start_process_ids']) == 2
    assert all(not contract['required_for_parent_completion'] and contract['admission_rolled_back']
               for contract in root.delegated_contracts.values())


def test_partial_start_preserves_unstarted_continuation_as_latest_required_owner(tmp_path_factory):
    root, tools = _supervisor(tmp_path_factory)
    manager = root._manager
    previous = manager.fork(root.process_id, 'executor', ToolRegistry([]), 4, RingLevel.RING_3,
                            capability_profile_id='governance.observe')
    previous.status = ProcessStatus.COMPLETED
    previous.lifecycle_finalized = True
    root.register_child_contract(previous.process_id, {'required_for_parent_completion': True,
        'task_kind': 'answer', 'acceptance_criteria': ['Report remaining facts']})
    nodes = _nodes()
    nodes[1]['continue_process_id'] = previous.process_id
    original = manager.start
    starts = []
    def start(*args, **kwargs):
        if starts:
            raise RuntimeError('second launch unavailable')
        starts.append(args[0])
        original(*args, **kwargs)
        manager._threads[args[0]].join(3)
    with patch.object(manager, 'start', start), patch('backend.core.loop.executor.agent_step',
                                                      lambda **kw: _finish(kw['process']).result):
        result = tools['delegate_task_dag'].execute({'nodes': nodes})
    assert result['execution_state'] == 'partially_started'
    successor_id = result['cancelled_before_start_process_ids'][0]
    assert root.delegated_contracts[previous.process_id]['superseded_by'] == successor_id
    assert root.delegated_contracts[successor_id]['required_for_parent_completion']
    assert manager.get(successor_id).session is previous.session
