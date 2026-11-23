"""Remaining work keeps logical dependencies when their execution is replaced."""
from copy import deepcopy
from threading import Event
from unittest.mock import patch

import pytest

from backend.core.loop.models import ProcessStatus
from tests.test_dag_admission import _nodes, _supervisor, _finish


def _request(description, **extra):
    return {'task_description': description, 'capability_profile_id': 'governance.observe',
            'task_kind': 'answer', 'acceptance_criteria': ['Report facts'], **extra}


def _closed_pair(workspace):
    root, tools = _supervisor(workspace)
    manager = root._manager
    with patch.object(manager, 'start'):
        b = manager.get(tools['delegate_task'].execute(_request('Produce facts'))['process_id'])
        c = manager.get(tools['delegate_task'].execute(_request('Consume facts', depends_on=[b.process_id]))['process_id'])
    for child in (b, c):
        child.status = ProcessStatus.FAILED
        child.result = {'status': 'failed'}
        child.lifecycle_finalized = True
        root.task_budget.release_process(child.process_id, status='failed')
    return root, tools, b, c


def _join(manager, pid):
    thread = manager._threads[pid]
    thread.join(3)
    assert not thread.is_alive()


def test_partial_dag_retries_upstream_then_downstream_and_completes_original_obligations(tmp_path_factory):
    root, tools = _supervisor(tmp_path_factory)
    manager = root._manager
    nodes = _nodes()
    nodes.append({**nodes[1], 'node_id': 'c', 'task_description': 'Consume b', 'depends_on': ['b']})
    original_start = manager.start
    starts = []
    def fail_second(*args, **kwargs):
        if starts:
            raise RuntimeError('second launch unavailable')
        starts.append(args[0])
        original_start(*args, **kwargs)
        _join(manager, args[0])
    with patch.object(manager, 'start', fail_second), patch('backend.core.loop.executor.agent_step', lambda **kw: _finish(kw['process']).result):
        admitted = tools['delegate_task_dag'].execute({'nodes': nodes})
    assert admitted['execution_state'] == 'partially_started'
    b, c = [manager.get(admitted['nodes'][key]) for key in ('b', 'c')]
    assert all(child.status == ProcessStatus.CANCELLED for child in (b, c))
    original_c = deepcopy(root.delegated_contracts[c.process_id])
    with patch('backend.core.loop.executor.agent_step', lambda **kw: _finish(kw['process']).result):
        b2 = tools['delegate_task'].execute(_request('Retry b', continue_process_id=b.process_id))
        assert b2['delegated'], b2
        _join(manager, b2['process_id'])
        c2 = tools['delegate_task'].execute(_request('Retry c', continue_process_id=c.process_id,
                                                    depends_on=[b2['process_id']]))
        assert c2['delegated'], c2
        _join(manager, c2['process_id'])
    child = manager.get(c2['process_id'])
    assert child.status == ProcessStatus.COMPLETED
    assert child.depends_on == [b2['process_id']]
    assert root.delegated_contracts[c.process_id]['depends_on'] == original_c['depends_on']
    assert child.context_snapshot['task_contract']['continuation']['dependency_rebinding'] == {
        'previous': [b.process_id], 'effective': [b2['process_id']]}
    for pid in (admitted['nodes']['a'], b2['process_id'], c2['process_id']):
        assert tools['review_child_outcome'].execute({'process_id': pid, 'verdict': 'approved',
            'summary': 'Verified requested facts'})['accepted']
    assert _finish(root).result['status'] == 'completed'


@pytest.mark.parametrize('outcome', ['completed', 'failed'])
def test_running_successor_uses_existing_dependency_wait_and_failure_boundary(tmp_path_factory, outcome):
    root, tools, b, c = _closed_pair(tmp_path_factory)
    manager = root._manager
    b_running, release_b, c_ran = Event(), Event(), Event()
    def model(**kwargs):
        child = kwargs['process']
        if child.task_description.startswith('Retry b'):
            b_running.set()
            assert release_b.wait(3)
            if outcome == 'failed':
                child.status = ProcessStatus.FAILED
                child.result = {'status': 'failed'}
                return child.result
        else:
            c_ran.set()
        return _finish(child).result
    b2 = c2 = None
    try:
        with patch('backend.core.loop.executor.agent_step', model):
            b2 = tools['delegate_task'].execute(_request('Retry b', continue_process_id=b.process_id))
            assert b_running.wait(3)
            c2 = tools['delegate_task'].execute(_request('Retry c', continue_process_id=c.process_id))
            assert c2['delegated'], c2
            assert manager.get(c2['process_id']).depends_on == [b2['process_id']]
            assert not c_ran.is_set()
            release_b.set()
            _join(manager, b2['process_id'])
            _join(manager, c2['process_id'])
        assert c_ran.is_set() is (outcome == 'completed')
        assert manager.get(c2['process_id']).status.value == outcome
    finally:
        release_b.set()
        for response in (b2, c2):
            if response and response.get('process_id') in manager._threads:
                _join(manager, response['process_id'])


@pytest.mark.parametrize('requested', ['omit', 'empty', 'unrelated'])
def test_omitting_or_replacing_request_does_not_remove_failed_dependency(tmp_path_factory, requested):
    root, tools, b, c = _closed_pair(tmp_path_factory)
    manager = root._manager
    args = {}
    if requested == 'empty':
        args['depends_on'] = []
    elif requested == 'unrelated':
        with patch.object(manager, 'start'):
            other = tools['delegate_task'].execute(_request('Unrelated facts'))
        other_child = manager.get(other['process_id'])
        other_child.status = ProcessStatus.COMPLETED
        other_child.lifecycle_finalized = True
        args['depends_on'] = [other_child.process_id]
    with patch.object(manager, 'start'):
        resumed = tools['delegate_task'].execute(_request('Retry c', continue_process_id=c.process_id, **args))
    assert resumed['delegated'], resumed
    child = manager.get(resumed['process_id'])
    assert b.process_id in child.depends_on
    assert 'upstream dependency did not complete' in manager._wait_for_dependencies(child)


@pytest.mark.parametrize('invalid', ['cross_owner', 'backlink', 'task_identity', 'cycle'])
def test_invalid_successor_chain_cannot_replace_retained_dependency(tmp_path_factory, invalid):
    root, tools, b, c = _closed_pair(tmp_path_factory)
    manager = root._manager
    with patch.object(manager, 'start'):
        response = tools['delegate_task'].execute(_request('Retry b', continue_process_id=b.process_id))
    b2 = manager.get(response['process_id'])
    contract = root.delegated_contracts[b2.process_id]
    if invalid == 'cross_owner':
        b2.parent_id = 'another-supervisor'
        root.child_ids.remove(b2.process_id)
    elif invalid == 'backlink':
        contract['supersedes_process_id'] = 'unrelated'
    elif invalid == 'task_identity':
        contract['continuation']['source_task_id'] = 'unrelated'
    else:
        b2.status = ProcessStatus.FAILED
        b2.lifecycle_finalized = True
        contract['superseded_by'] = b.process_id
        root.delegated_contracts[b.process_id].update(supersedes_process_id=b2.process_id,
            continuation={'source_process_id': b2.process_id, 'source_task_id': b2.active_task_id,
                          'mode': 'remaining', 'retained_obligations': True})
    before = len(manager.list_processes())
    result = tools['delegate_task'].execute(_request('Retry c', continue_process_id=c.process_id))
    assert not result['delegated']
    assert 'CONTINUATION_DEPENDENCY_INVALID' in result['error_info']['message']
    assert len(manager.list_processes()) == before


@pytest.mark.parametrize('checkpoint_fails', [False, True])
def test_successor_provider_completion_cannot_skip_terminal_barrier(tmp_path_factory, checkpoint_fails):
    root, tools, b, c = _closed_pair(tmp_path_factory)
    manager = root._manager
    at_barrier, release, consumer_ran = Event(), Event(), Event()
    original_start = manager.start
    def barrier():
        at_barrier.set()
        assert release.wait(3)
        if checkpoint_fails:
            raise RuntimeError('injected terminal checkpoint failure')
    def start(pid, target, **kwargs):
        if manager.get(pid).task_description.startswith('Retry b'):
            kwargs['on_terminal'] = barrier
        original_start(pid, target, **kwargs)
    def model(**kwargs):
        if kwargs['process'].task_description.startswith('Retry c'):
            consumer_ran.set()
        return _finish(kwargs['process']).result
    responses = []
    try:
        with patch.object(manager, 'start', start), patch('backend.core.loop.executor.agent_step', model):
            b2 = tools['delegate_task'].execute(_request('Retry b', continue_process_id=b.process_id))
            responses.append(b2)
            assert at_barrier.wait(3)
            assert manager.get(b2['process_id']).status == ProcessStatus.COMPLETED
            assert not manager.get(b2['process_id']).lifecycle_finalized
            c2 = tools['delegate_task'].execute(_request('Retry c', continue_process_id=c.process_id))
            responses.append(c2)
            assert c2['delegated'], c2
            assert not consumer_ran.is_set()
            release.set()
            for response in responses:
                _join(manager, response['process_id'])
        assert consumer_ran.is_set() is (not checkpoint_fails)
        assert manager.get(c2['process_id']).status.value == ('failed' if checkpoint_fails else 'completed')
    finally:
        release.set()
        for response in responses:
            _join(manager, response['process_id'])


def _closed_successor(root, tools, old, *, completed=False):
    with patch.object(root._manager, 'start'):
        response = tools['delegate_task'].execute(_request('Retry producer', continue_process_id=old.process_id))
    assert response['delegated'], response
    child = root._manager.get(response['process_id'])
    child.status = ProcessStatus.COMPLETED if completed else ProcessStatus.FAILED
    child.result = {'status': child.status.value}
    child.lifecycle_finalized = True
    root.task_budget.release_process(child.process_id, status=child.status.value)
    return child


def test_remaining_chain_does_not_redirect_to_unrelated_followup(tmp_path_factory):
    root, tools, b, c = _closed_pair(tmp_path_factory)
    b2 = _closed_successor(root, tools, b)
    b3 = _closed_successor(root, tools, b2, completed=True)
    root.child_reviews[b3.process_id] = {'verdict': 'approved', 'summary': 'Verified complete facts'}
    with patch.object(root._manager, 'start'):
        followup = tools['delegate_task'].execute(_request('Unrelated new question', continue_process_id=b3.process_id))
        resumed = tools['delegate_task'].execute(_request('Retry consumer', continue_process_id=c.process_id))
    assert followup['continuation']['mode'] == 'follow_up'
    assert resumed['delegated'], resumed
    child = root._manager.get(resumed['process_id'])
    assert child.depends_on == [b3.process_id]
    assert root._manager._wait_for_dependencies(child) == ''


def test_cold_chain_keeps_effective_dependency_and_frozen_interface(tmp_path_factory):
    from backend.core.loop.manager import AgentProcessManager, SessionStore
    from backend.core.loop.recovery import restore_incomplete_processes
    from backend.core.loop.interface_contract import capture_declared_contract, verify_declared_contract
    root, tools, b, c = _closed_pair(tmp_path_factory)
    source = tmp_path_factory / 'api.py'
    source.write_text('def fetch(value):\n    return value\n')
    frozen = capture_declared_contract(tmp_path_factory, [{'input_interfaces': ['api.py:fetch']}])
    root.delegated_contracts[c.process_id].update(input_interfaces=['api.py:fetch'], interface_contract=frozen)
    b2 = _closed_successor(root, tools, b)
    b3 = _closed_successor(root, tools, b2, completed=True)
    with patch.object(root._manager, 'start'):
        resumed = tools['delegate_task'].execute(_request('Retry consumer', continue_process_id=c.process_id))
    assert resumed['delegated'], resumed
    child = root._manager.get(resumed['process_id'])
    contract = child.context_snapshot['task_contract']
    assert contract['interface_contract']['interfaces'] == frozen['interfaces']
    source.write_text('def fetch(value, incompatible):\n    return value\n')
    assert verify_declared_contract(contract['interface_contract'], tmp_path_factory, ['api.py:fetch'])
    store = SessionStore(str(tmp_path_factory), state_home=tmp_path_factory / 'state')
    try:
        for process in root._manager.list_processes():
            if process.status in (ProcessStatus.RUNNING, ProcessStatus.WAITING):
                process.status = ProcessStatus.CANCELLED
                process.result = {'status': 'cancelled'}
                process.lifecycle_finalized = True
            store.save_process_checkpoint(process)
            store.append_event(process.process_id, 'agent_complete', {'status': process.status.value})
        cold = AgentProcessManager()
        restore_incomplete_processes(store, cold, tmp_path_factory, include_process_ids=[root.process_id])
        restored_root, restored_child = cold.get(root.process_id), cold.get(child.process_id)
        assert cold.resolve_retained_dependencies(restored_root, [b.process_id]) == [b3.process_id]
        assert restored_child.depends_on == [b3.process_id]
        saved = restored_child.context_snapshot['task_contract']
        assert saved['continuation']['dependency_rebinding'] == contract['continuation']['dependency_rebinding']
        assert saved['interface_contract']['interfaces'] == frozen['interfaces']
    finally:
        store.close()


def test_retained_checkout_cannot_silently_change_dependency_base(tmp_path_factory):
    root, tools, b, _ = _closed_pair(tmp_path_factory)
    manager = root._manager
    with patch.object(manager, 'start'):
        response = tools['delegate_task'].execute({'task_description': 'Partial implementation',
            'capability_profile_id': 'development.workspace', 'task_kind': 'action',
            'target_files': ['feature.py'], 'acceptance_criteria': ['Keep partial result'], 'depends_on': [b.process_id]})
    child = manager.get(response['process_id'])
    child.status = ProcessStatus.FAILED
    child.result = {'status': 'failed'}
    child.lifecycle_finalized = True
    root.task_budget.release_process(child.process_id, status='failed')
    root.worktree = {'snapshot_commit': 'original-snapshot'}
    child.worktree = {'isolated': True, 'mode': 'write', 'state': 'leased',
        'path': str(tmp_path_factory), 'snapshot_commit': 'original-snapshot'}
    _closed_successor(root, tools, b, completed=True)
    with patch.object(manager, 'fork') as fork:
        resumed = tools['delegate_task'].execute({'task_description': 'Continue partial implementation',
            'capability_profile_id': 'development.workspace', 'task_kind': 'action',
            'continue_process_id': child.process_id, 'acceptance_criteria': ['Keep partial result']})
    assert not resumed['delegated']
    assert 'CONTINUATION_BASE_MISMATCH' in resumed['error_info']['message']
    fork.assert_not_called()


def test_replacement_dag_preflight_and_prepared_admission_share_dependency_resolution(tmp_path_factory):
    root, tools, b, c = _closed_pair(tmp_path_factory)
    manager = root._manager
    with patch('backend.core.loop.executor.agent_step', lambda **kw: _finish(kw['process']).result):
        result = tools['delegate_task_dag'].execute({'nodes': [
            _request('Retry producer', node_id='producer', continue_process_id=b.process_id),
            _request('Retry consumer', node_id='consumer', continue_process_id=c.process_id, depends_on=['producer']),
        ]})
        assert result['delegated'], result
        for pid in result['nodes'].values():
            _join(manager, pid)
    child = manager.get(result['nodes']['consumer'])
    assert child.status == ProcessStatus.COMPLETED
    assert child.depends_on == [result['nodes']['producer']]
    assert root.delegated_contracts[child.process_id]['required_for_parent_completion']


def test_dependency_rebinding_preflight_is_read_only(tmp_path_factory):
    root, tools, b, c = _closed_pair(tmp_path_factory)
    b2 = _closed_successor(root, tools, b, completed=True)
    before = deepcopy((root.delegated_contracts, root.task_budget.snapshot()['used'],
                       root.child_ids, root.context_snapshot))
    with patch.object(root._manager, 'fork') as fork:
        result = tools['delegate_task'].execute(_request('Retry consumer', continue_process_id=c.process_id), validate_only=True)
    assert result['validated'], result
    fork.assert_not_called()
    after = deepcopy((root.delegated_contracts, root.task_budget.snapshot()['used'],
                      root.child_ids, root.context_snapshot))
    for state in (before, after):
        state[1].pop('elapsed_seconds')
    assert before == after
    assert root._manager.resolve_retained_dependencies(root, [b.process_id]) == [b2.process_id]
