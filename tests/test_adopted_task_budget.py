"""Same-task BTW successors retain the Host's cumulative budget, hot and cold."""
from copy import deepcopy
from queue import Queue
from types import SimpleNamespace
from dataclasses import replace

import pytest

from backend.core.daemon.dispatch import _cmd_task
from backend.core.loop.discussion_adoption import adopt_discussion, save_discussion
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.recovery import restore_incomplete_processes
from backend.core.loop.task_contract import get_task_contract
from backend.core.loop.tools import ToolRegistry
from tests.test_task_contract_recovery import _supervisor


@pytest.fixture
def runtime(tmp_path_factory, monkeypatch):
    workspace = tmp_path_factory
    store = SessionStore(str(workspace), migrate_legacy=False)
    root, _ = _supervisor(workspace)
    manager = root._manager
    manager.storage, manager.session_store = store._storage, store
    monkeypatch.setattr('backend.core.daemon.dispatch.threading.Thread.start', lambda self: None)
    try:
        yield manager, root, store, workspace
    finally:
        for process in manager.list_processes():
            if process._run_lock.locked():
                process._run_lock.release()
        store.close()


def interrupt(runtime, process):
    manager, _, store, _ = runtime
    process.status = ProcessStatus.AWAITING_USER
    ref = save_discussion(store._storage, 'discussion-' + process.process_id, 'Include zero',
        {'answer': 'Also verify zero input.', 'status': 'completed'},
        [{'process_id': process.process_id, 'task_id': process.active_task_id,
          'contract_revision': get_task_contract(process).get('revision', 0)}])
    adopted = adopt_discussion(process, store._storage,
        sidecar_id='discussion-' + process.process_id, discussion_ref=ref['discussion_ref'],
        expected_task_id=process.active_task_id,
        expected_contract_revision=get_task_contract(process).get('revision', 0), store=store)
    if process._run_lock.locked(): process._run_lock.release()
    manager.kill(process.process_id)
    assert process.lifecycle_finalized
    store.save_process_checkpoint(process)
    return {'process_id': process.process_id, 'task_id': process.active_task_id,
            'change_id': adopted['change']['id']}


def continue_task(runtime, source, origin=None, **extra):
    manager, _, store, workspace = runtime
    command = {'cmd': 'task', 'action': 'chat', 'process_id': source.process_id,
        'task_id': source.active_task_id if origin else 'independent-user-task',
        'instruction': 'Continue the accepted task.', 'role': 'supervisor',
        'actor_kind': 'supervisor', 'capability_profile_id': 'supervisor.control',
        'task_kind': 'supervisor', 'fresh_task_budget': True, 'max_steps': 8,
        'context_snapshot': {}, 'task_budget': {'max_agents': 99, 'max_provider_calls': 9999,
                                              'max_seconds': 9999}, **extra}
    if origin is not None: command['adopted_change_origin'] = origin
    events = []
    _cmd_task(command, SimpleNamespace(workspace_path=workspace), SimpleNamespace(name='budget-fixture'),
        {'apm': manager, 'dispatcher': object(), 'evq': Queue(), 'storage': store._storage,
         'session_store': store, 'llm': object()}, events.append)
    results = [e for e in events if e.get('event') == 'command_result']
    assert results
    result = results[-1].get('result', results[-1])
    return manager.get(result.get('process_id', '')), result


def test_root_adopted_continuation_preserves_spend_limits_and_old_lease(runtime):
    manager, root, store, _ = runtime
    budget = root.task_budget
    budget.begin_provider_call(root.process_id)
    budget.record_provider_usage({'input_tokens': 21, 'output_tokens': 9})
    origin = interrupt(runtime, root)
    before = budget.snapshot()
    successor, result = continue_task(runtime, root, origin)
    assert result.get('status') == 'pending', result
    assert successor.active_task_id == root.active_task_id and successor.session is root.session
    assert successor.task_budget is budget
    after = budget.snapshot()
    assert after['limits'] == before['limits'] and after['deadline'] == before['deadline']
    for name in ('provider_calls', 'estimated_output_tokens', 'reported_input_tokens',
                 'reported_output_tokens', 'by_purpose'):
        assert after['used'][name] == before['used'][name]
    assert after['used']['elapsed_seconds'] >= before['used']['elapsed_seconds']
    assert after['used']['agents'] == before['used']['agents'] + 1
    assert after['process_leases'][root.process_id] == before['process_leases'][root.process_id]
    assert after['process_leases'][root.process_id]['state'] == 'released'
    assert after['process_leases'][successor.process_id]['state'] == 'active'
    assert store.load_process_state(successor.process_id)['runtime_state']['task_budget']['used']['provider_calls'] == 1


@pytest.mark.parametrize('selected', ['latest', 'old-child'])
def test_cold_restore_joins_adopted_root_chain_and_historical_child(runtime, selected):
    manager, root, store, workspace = runtime
    child = manager.fork(root.process_id, 'worker', ToolRegistry([]), 4, RingLevel.RING_3,
        workspace_path=str(workspace), task_id='child', actor_kind='worker')
    root.child_ids.append(child.process_id)
    root.delegated_contracts[child.process_id] = {'task_id': 'child', 'required_for_parent_completion': True}
    child.status = ProcessStatus.AWAITING_USER
    root.task_budget.begin_provider_call(child.process_id)
    origin = interrupt(runtime, root)
    store.save_process_checkpoint(child)
    second, result = continue_task(runtime, root, origin)
    assert result.get('status') == 'pending', result
    second.task_budget.begin_provider_call(second.process_id)
    origin2 = interrupt(runtime, second)
    third, result = continue_task(runtime, second, origin2)
    assert result.get('status') == 'pending', result
    third.task_budget.begin_provider_call(third.process_id)
    store.save_process_checkpoint(third)
    cold = AgentProcessManager()
    restore_incomplete_processes(store, cold, workspace,
        include_process_ids=[third.process_id if selected == 'latest' else child.process_id])
    restored = [cold.get(p.process_id) for p in (root, child, second, third)]
    assert all(restored)
    assert all(p.task_budget is restored[0].task_budget for p in restored)
    snapshot = restored[0].task_budget.snapshot()
    assert snapshot['used']['provider_calls'] == 3
    assert snapshot['used']['agents'] == 4
    assert all(snapshot['process_leases'][p.process_id]['state'] == 'released' for p in (root, child, second))
    assert snapshot['process_leases'][third.process_id]['state'] == 'active'
    assert snapshot['limits'] == root.task_budget.snapshot()['limits']
    assert snapshot['deadline'] == root.task_budget.snapshot()['deadline']


def test_ordinary_new_user_task_keeps_fresh_budget(runtime):
    _, root, _, _ = runtime
    root.task_budget.begin_provider_call(root.process_id)
    interrupt(runtime, root)
    successor, result = continue_task(runtime, root)
    assert result.get('status') == 'pending', result
    assert successor.task_budget is not root.task_budget
    assert successor.task_budget.snapshot()['used']['provider_calls'] == 0


@pytest.mark.parametrize('damage', ['unfinalized', 'unknown', 'wrong-task', 'wrong-change', 'not-user-adopted', 'unregistered-source'])
def test_unverified_adoption_cannot_allocate_or_reset_budget(runtime, monkeypatch, damage):
    manager, root, store, _ = runtime
    root.task_budget.begin_provider_call(root.process_id)
    origin = interrupt(runtime, root)
    if damage == 'unfinalized': root.lifecycle_finalized = False
    elif damage == 'unknown': root.recovery = {'resume_forbidden': True, 'checkpoint_commit': 'unknown'}
    elif damage == 'wrong-task': origin['task_id'] = 'other'
    elif damage == 'wrong-change': origin['change_id'] = 'made-up'
    elif damage == 'not-user-adopted':
        root.context_snapshot['task_maintenance']['changes'][origin['change_id']]['user_adopted'] = False
    elif damage == 'unregistered-source':
        monkeypatch.setattr(store._storage, 'get_state_ref', lambda *args, **kwargs: None)
    before = root.task_budget.snapshot()
    successor, result = continue_task(runtime, root, origin)
    assert successor is None and result.get('error'), result
    after = root.task_budget.snapshot()
    assert len(manager.list_processes()) == 1
    assert after['process_leases'] == before['process_leases']
    assert after['used']['agents'] == before['used']['agents']
    assert after['used']['provider_calls'] == 1


@pytest.mark.parametrize('exhausted', ['agents', 'deadline'])
def test_old_limit_still_blocks_new_admission(runtime, exhausted):
    from backend.core.loop.budget import TaskBudgetExceeded
    manager, root, _, _ = runtime
    origin = interrupt(runtime, root)
    if exhausted == 'agents': root.task_budget.limits = replace(root.task_budget.limits, max_agents=1)
    else: root.task_budget.deadline_monotonic = root.task_budget.started_monotonic - 1
    with pytest.raises(TaskBudgetExceeded):
        continue_task(runtime, root, origin)
    assert len(manager.list_processes()) == 1
    assert root.task_budget.snapshot()['used']['agents'] == 1


def test_reliably_finalized_legacy_root_lease_is_closed_not_reopened(runtime):
    _, root, _, _ = runtime
    origin = interrupt(runtime, root)
    # Old dispatch roots finalized without releasing their root lease.
    root.task_budget._process_leases[root.process_id]['state'] = 'active'
    root.task_budget.begin_provider_call(root.process_id)
    successor, result = continue_task(runtime, root, origin)
    assert result.get('status') == 'pending', result
    snapshot = successor.task_budget.snapshot()
    assert snapshot['used']['provider_calls'] == 1
    assert snapshot['process_leases'][root.process_id]['state'] == 'released'
    assert snapshot['process_leases'][successor.process_id]['state'] == 'active'


@pytest.mark.parametrize('unknown', [False, True])
def test_checkpoint_failure_retains_admission_and_prevents_duplicate_execution(runtime, monkeypatch, unknown):
    from backend.core.storage import StorageCommitUnknown
    manager, root, store, workspace = runtime
    root.task_budget.begin_provider_call(root.process_id)
    origin = interrupt(runtime, root)
    original = store.checkpoint_before_user_turn
    def fail(process, **kwargs):
        # Unknown may mean the durable commit succeeded. Never undo this ledger.
        if unknown: original(process, **kwargs)
        raise StorageCommitUnknown('fixture unknown') if unknown else OSError('fixture disk unavailable')
    monkeypatch.setattr(store, 'checkpoint_before_user_turn', fail)
    successor, result = continue_task(runtime, root, origin)
    assert result['admission_retained'] and successor is not None
    assert successor.status == ProcessStatus.RECOVERY_REVIEW_REQUIRED
    assert successor.recovery['resume_forbidden'] and not successor.lifecycle_finalized
    assert successor.task_budget is root.task_budget
    before = successor.task_budget.snapshot()
    assert before['used']['provider_calls'] == 1 and before['used']['agents'] == 2
    assert successor.process_id not in manager._threads
    again, rejected = continue_task(runtime, root, origin)
    assert again is None and rejected.get('error')
    assert len(manager.list_processes()) == 2
    assert root.task_budget.snapshot()['used']['agents'] == 2
    assert root.task_budget.snapshot()['used']['provider_calls'] == 1
    if unknown:
        cold = AgentProcessManager()
        restore_incomplete_processes(store, cold, workspace, include_process_ids=[successor.process_id])
        restored = cold.get(successor.process_id)
        assert restored.task_budget is cold.get(root.process_id).task_budget
        assert restored.task_budget.snapshot()['used']['provider_calls'] == 1


def test_completed_successor_does_not_allow_reusing_old_adoption(runtime):
    manager, root, _, _ = runtime
    origin = interrupt(runtime, root)
    successor, _ = continue_task(runtime, root, origin)
    interrupt(runtime, successor)
    before = root.task_budget.snapshot()
    again, result = continue_task(runtime, root, origin)
    assert again is None and result['error'] == 'BTW_ADOPTION_ALREADY_CONTINUED'
    assert len(manager.list_processes()) == 2
    assert root.task_budget.snapshot()['used']['agents'] == before['used']['agents']


def test_failed_execution_keeps_predecessor_usage_and_new_execution_usage(runtime, monkeypatch):
    manager, root, store, workspace = runtime
    root.task_budget.begin_provider_call(root.process_id)
    origin = interrupt(runtime, root)
    def fail(process, *args, **kwargs):
        process.task_budget.begin_provider_call(process.process_id)
        raise RuntimeError('deterministic task crash after admitted call')
    monkeypatch.setattr('backend.core.loop.executor.agent_step', fail)
    successor, result = continue_task(runtime, root, origin)
    assert result.get('status') == 'pending'
    manager._threads[successor.process_id]._target()
    assert successor.status == ProcessStatus.FAILED and successor.lifecycle_finalized
    assert successor.task_budget.snapshot()['used']['provider_calls'] == 2
    assert root.task_budget.snapshot()['used']['provider_calls'] == 2
    assert store.load_process_state(successor.process_id)['runtime_state']['task_budget']['used']['provider_calls'] == 2


def test_failed_factory_refunds_only_unpublished_agent_reservation(runtime, monkeypatch):
    from backend.core.loop.runtime import AgentRuntimeFactory
    manager, root, _, _ = runtime
    root.task_budget.begin_provider_call(root.process_id)
    origin = interrupt(runtime, root)
    before = root.task_budget.snapshot()
    def fail(*args, **kwargs): raise RuntimeError('fixture construction failure')
    monkeypatch.setattr(AgentRuntimeFactory, 'create', fail)
    with pytest.raises(RuntimeError, match='construction failure'):
        continue_task(runtime, root, origin)
    after = root.task_budget.snapshot()
    assert after['used']['agents'] == before['used']['agents']
    assert after['used']['provider_calls'] == before['used']['provider_calls']
    assert after['process_leases'] == before['process_leases']
    assert len(manager.list_processes()) == 1
    assert not any(e['event'] == 'adopted_task_execution_continued' for e in root.session.host_ledger)


def test_same_session_plain_new_task_is_not_joined_on_cold_restore(runtime):
    manager, root, store, workspace = runtime
    root.task_budget.begin_provider_call(root.process_id)
    origin = interrupt(runtime, root)
    continued, _ = continue_task(runtime, root, origin)
    continued.task_budget.begin_provider_call(continued.process_id)
    interrupt(runtime, continued)
    fresh, _ = continue_task(runtime, continued)
    cold = AgentProcessManager()
    restore_incomplete_processes(store, cold, workspace,
        include_process_ids=[root.process_id, continued.process_id, fresh.process_id])
    first, second, third = [cold.get(p.process_id) for p in (root, continued, fresh)]
    assert first.task_budget is second.task_budget
    assert third.task_budget is not first.task_budget
    assert first.task_budget.snapshot()['used']['provider_calls'] == 2
    assert third.task_budget.snapshot()['used']['provider_calls'] == 0


def test_native_host_generated_adopt_interrupt_command_keeps_root_budget(runtime, monkeypatch):
    import io
    from backend.core.native_host import NativeHost
    from backend.core.daemon.client import DaemonClient
    from backend.core.daemon.dispatch import _cmd_loop_status
    manager, root, store, workspace = runtime
    root.status = ProcessStatus.AWAITING_USER
    root.task_budget.begin_provider_call(root.process_id)
    ref = save_discussion(store._storage, 'host-discussion', 'Additional acceptance',
        {'answer': 'Also cover zero.', 'status': 'completed'},
        [{'process_id': root.process_id, 'task_id': root.active_task_id, 'contract_revision': 0}])
    host = NativeHost(stdout=io.StringIO())
    client = DaemonClient('budget-fixture')
    context = {'apm': manager, 'dispatcher': object(), 'evq': Queue(),
        'storage': store._storage, 'session_store': store, 'llm': object()}
    session, project = SimpleNamespace(workspace_path=workspace), SimpleNamespace(name='budget-fixture')
    commands, admissions = [], []
    class Admitted(Exception): pass
    def deliver(command):
        commands.append(deepcopy(command))
        def emit(event):
            if event.get('event') == 'command_result':
                with client._lock:
                    client._cmd_results[command['request_id']] = event
                    client._cmd_events[command['request_id']].set()
        (_cmd_loop_status if command['cmd'] == 'loop_status' else _cmd_task)(command, session, project, context, emit)
    def send(command, **kwargs):
        admissions.append(client.send_command({**command, 'context_snapshot': {}}))
        raise Admitted()
    monkeypatch.setattr(client, '_write_cmd', deliver)
    monkeypatch.setattr(client, 'send_task', send)
    monkeypatch.setattr(host, '_get_daemon', lambda *args, **kwargs: client)
    monkeypatch.setattr(host.services, 'config_get', lambda: {})
    try:
        with pytest.raises(Admitted):
            host._runtime_btw_adopt(project.name, {'process_id': root.process_id,
                'task_id': root.active_task_id, 'expected_revision': 0, 'sidecar_id': 'host-discussion',
                'discussion_ref': ref['discussion_ref'], 'mode': 'interrupt'})
        assert admissions[0]['status'] == 'pending'
        successor = manager.get(admissions[0]['process_id'])
        assert successor.task_budget is root.task_budget
        assert successor.task_budget.snapshot()['used']['provider_calls'] == 1
        assert commands[-1]['fresh_task_budget'] is True
    finally:
        host.close()


@pytest.mark.parametrize('live_calls', [1, 8])
def test_later_lazy_restore_reuses_live_ledger_without_erasing_new_usage(runtime, live_calls):
    manager, root, store, workspace = runtime
    origin = interrupt(runtime, root)
    successor, _ = continue_task(runtime, root, origin)
    cold = AgentProcessManager()
    restore_incomplete_processes(store, cold, workspace, include_process_ids=[successor.process_id])
    live = cold.get(successor.process_id)
    for _ in range(live_calls): live.task_budget.begin_provider_call(live.process_id)
    # A retained child row can be loaded after the first group restore.
    child = manager.fork(root.process_id, 'worker', ToolRegistry([]), 3, RingLevel.RING_3,
        workspace_path=str(workspace), task_id='late-child', actor_kind='worker')
    store.save_process_checkpoint(child)
    # The live ledger lacks this newer admission; do not guess how to merge it.
    with pytest.raises(ValueError, match='complete cold restore'):
        restore_incomplete_processes(store, cold, workspace, include_process_ids=[child.process_id])
    assert live.task_budget.snapshot()['used']['provider_calls'] == live_calls
    assert cold.get(child.process_id) is None


def test_lazy_restore_of_historical_record_keeps_current_shared_object(runtime):
    manager, root, store, workspace = runtime
    origin = interrupt(runtime, root)
    successor, _ = continue_task(runtime, root, origin)
    cold = AgentProcessManager()
    restore_incomplete_processes(store, cold, workspace, include_process_ids=[successor.process_id])
    live = cold.get(successor.process_id)
    live.task_budget.begin_provider_call(live.process_id)
    # Simulate only the historical identity being absent from the live index.
    cold._processes.pop(root.process_id)
    restore_incomplete_processes(store, cold, workspace, include_process_ids=[root.process_id])
    assert cold.get(root.process_id).task_budget is live.task_budget
    assert live.task_budget.snapshot()['used']['provider_calls'] == 1


@pytest.mark.parametrize('field', ['parent_id', 'purpose', 'reserved_provider_calls', 'state'])
def test_lazy_restore_rejects_conflicting_lease_even_when_live_revision_is_newer(runtime, field):
    manager, root, store, workspace = runtime
    origin = interrupt(runtime, root)
    successor, _ = continue_task(runtime, root, origin)
    cold = AgentProcessManager()
    restore_incomplete_processes(store, cold, workspace, include_process_ids=[successor.process_id])
    live = cold.get(successor.process_id)
    for _ in range(8): live.task_budget.begin_provider_call(live.process_id)
    lease = live.task_budget._process_leases[root.process_id]
    lease[field] = {'parent_id': 'unrelated', 'purpose': 'verification',
                    'reserved_provider_calls': 123, 'state': 'active'}[field]
    cold._processes.pop(root.process_id)
    before = set(cold._processes)
    with pytest.raises(ValueError, match='complete cold restore'):
        restore_incomplete_processes(store, cold, workspace, include_process_ids=[root.process_id])
    assert set(cold._processes) == before
    assert live.task_budget.snapshot()['used']['provider_calls'] == 8


@pytest.mark.parametrize('damage', ['session', 'task', 'lease', 'finality'])
def test_cold_invalid_shared_budget_lineage_fails_closed(runtime, monkeypatch, damage):
    manager, root, store, workspace = runtime
    origin = interrupt(runtime, root)
    successor, _ = continue_task(runtime, root, origin)
    load = store.load_process_state
    def changed(pid):
        state = deepcopy(load(pid))
        if pid == root.process_id:
            if damage == 'session': state['session']['session_id'] = 'other-session'
            elif damage == 'task': state['process']['task_id'] = 'other-task'
            elif damage == 'finality': state['runtime_state']['lifecycle_finalized'] = False
        if pid == successor.process_id and damage == 'lease':
            state['runtime_state']['task_budget']['process_leases'][root.process_id]['state'] = 'active'
        return state
    monkeypatch.setattr(store, 'load_process_state', changed)
    cold = AgentProcessManager()
    with pytest.raises(ValueError, match='adopted budget lineage changed'):
        restore_incomplete_processes(store, cold, workspace, include_process_ids=[successor.process_id])
    assert not cold.list_processes()


def test_legacy_already_reset_successor_cannot_claim_a_complete_original_budget(runtime):
    manager, root, _, _ = runtime
    origin = interrupt(runtime, root)
    successor, _ = continue_task(runtime, root, origin)
    next_origin = interrupt(runtime, successor)
    receipt = next(e for e in successor.session.host_ledger
        if e.get('event') == 'adopted_task_execution_continued' and e.get('process_id') == successor.process_id)
    receipt.pop('budget_task_id')
    again, result = continue_task(runtime, successor, next_origin)
    assert again is None and result['error'] == 'BTW_ADOPTION_BUDGET_UNAVAILABLE'
    assert len(manager.list_processes()) == 2


@pytest.mark.parametrize('unknown', [False, True])
def test_history_failure_retains_visible_new_lease_without_execution(runtime, monkeypatch, unknown):
    from backend.core.storage import StorageCommitUnknown
    manager, root, store, workspace = runtime
    root.task_budget.begin_provider_call(root.process_id)
    origin = interrupt(runtime, root)
    def fail(*args, **kwargs):
        raise StorageCommitUnknown('fixture history unknown') if unknown else OSError('fixture history failure')
    monkeypatch.setattr('backend.core.history.HistoryManager.add_operation', fail)
    successor, result = continue_task(runtime, root, origin)
    assert result['code'] == 'ADOPTED_CONTINUATION_ADMISSION_FAILED'
    assert result['admission_retained'] and result['recovery_checkpointed']
    assert len(manager.list_processes()) == 2
    successor = next(p for p in manager.list_processes() if p is not root)
    assert successor.status == ProcessStatus.RECOVERY_REVIEW_REQUIRED
    assert successor.recovery['resume_forbidden']
    assert (successor.recovery.get('checkpoint_commit') == 'unknown') == unknown
    assert successor.process_id not in manager._threads and not successor._run_lock.locked()
    assert successor.task_budget is root.task_budget
    snapshot = successor.task_budget.snapshot()
    assert snapshot['used']['provider_calls'] == 1 and snapshot['used']['agents'] == 2
    assert snapshot['process_leases'][root.process_id]['state'] == 'released'
    assert snapshot['process_leases'][successor.process_id]['state'] == 'active'
    assert sum(e.get('event') == 'adopted_task_execution_continued'
               and e.get('process_id') == successor.process_id for e in successor.session.host_ledger) == 1
    again, result = continue_task(runtime, root, origin)
    assert again is None and result.get('error')
    assert len(manager.list_processes()) == 2
    # Reopen the actual storage connection, not only a new in-memory manager.
    from backend.core.storage import close_owned_storage
    store.close()
    close_owned_storage()
    reopened = SessionStore(str(workspace), migrate_legacy=False)
    cold = AgentProcessManager()
    cold.storage, cold.session_store = reopened._storage, reopened
    try:
        restore_incomplete_processes(reopened, cold, workspace, include_process_ids=[root.process_id])
        restored = cold.get(root.process_id)
        retained = cold.get(successor.process_id)
        assert retained is not None and retained.recovery['resume_forbidden']
        assert retained.task_budget is restored.task_budget
        assert restored.task_budget.snapshot()['used']['agents'] == 2
        assert restored.task_budget.snapshot()['used']['provider_calls'] == 1
        retry, rejected = continue_task((cold, restored, reopened, workspace), restored, origin)
        assert retry is None and rejected.get('error')
        assert len(cold.list_processes()) == 2
    finally:
        reopened.close()


def test_adopted_continuation_resumes_clock_without_resetting_original_allowance(runtime, monkeypatch):
    from backend.core.loop import budget as budget_module
    from backend.core.loop.budget import TaskBudgetExceeded
    _, root, _, _ = runtime
    now = [root.task_budget.started_monotonic + 10]
    monkeypatch.setattr(budget_module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    root.task_budget.begin_provider_call(root.process_id)
    root.task_budget.consume_output_estimate(9, root.process_id)
    root.task_budget.record_provider_usage({'input_tokens': 21, 'output_tokens': 9})
    root.task_budget.pause_for_user()
    origin = interrupt(runtime, root)
    before = root.task_budget.snapshot()
    assert before['used']['awaiting_user'] is True
    now[0] += 1000  # Waiting longer than the hard allowance is not active work.
    successor, result = continue_task(runtime, root, origin)
    assert result.get('status') == 'pending'
    after = successor.task_budget.snapshot()
    assert after['used']['awaiting_user'] is False
    assert after['used']['elapsed_seconds'] >= before['used']['elapsed_seconds']
    assert after['used']['provider_calls'] == 1
    assert after['used']['estimated_output_tokens'] == before['used']['estimated_output_tokens'] == 9
    assert after['used']['reported_input_tokens'] == before['used']['reported_input_tokens'] == 21
    assert after['used']['reported_output_tokens'] == before['used']['reported_output_tokens'] == 9
    assert after['deadline']['allowance_seconds'] == pytest.approx(before['deadline']['allowance_seconds'])
    assert after['used']['elapsed_seconds'] == pytest.approx(10)
    now[0] += 291
    with pytest.raises(TaskBudgetExceeded): successor.task_budget.check_deadline()


@pytest.mark.parametrize('mode', ['before', 'after'])
def test_real_history_commit_quarantine_keeps_admission_after_sqlite_reopen(runtime, monkeypatch, mode):
    from backend.core.history import HistoryManager
    from backend.core.storage import close_owned_storage
    from tests.test_storage_repository_outcomes import inject
    manager, root, store, workspace = runtime
    root.task_budget.begin_provider_call(root.process_id)
    origin = interrupt(runtime, root)
    original = HistoryManager.add_operation
    attempted = []
    monkeypatch.setattr(HistoryManager, '_storage', lambda: store._storage)
    def history(*args, **kwargs):
        pid = kwargs['correlation_id']
        committed = store.load_process_state(pid)
        assert committed is not None
        assert committed['runtime_state']['task_budget']['used']['agents'] == 2
        assert committed['runtime_state']['task_budget']['used']['provider_calls'] == 1
        attempted.append(pid)
        # Execute the real History SQL transaction, lose its COMMIT response,
        # and let production authoritative_transaction quarantine the writer.
        with inject(store._storage, 'state', mode):
            original(*args, **kwargs)
    monkeypatch.setattr(HistoryManager, 'add_operation', history)
    successor, result = continue_task(runtime, root, origin)
    assert result['code'] == 'ADOPTED_CONTINUATION_ADMISSION_FAILED'
    assert result['admission_checkpointed'] is True and result['recovery_checkpointed'] is False
    assert result['admission_retained'] and result['persistence_error']
    assert store._storage._state.quarantined
    assert attempted == [successor.process_id]
    assert successor.process_id not in manager._threads
    assert successor.recovery['checkpoint_commit'] == 'unknown'
    store.close()
    close_owned_storage()
    reopened = SessionStore(str(workspace), migrate_legacy=False)
    cold = AgentProcessManager()
    cold.storage, cold.session_store = reopened._storage, reopened
    try:
        restore_incomplete_processes(reopened, cold, workspace, include_process_ids=[root.process_id])
        restored, retained = cold.get(root.process_id), cold.get(successor.process_id)
        assert retained is not None and retained.task_budget is restored.task_budget
        assert retained.status in {ProcessStatus.RESUME_AVAILABLE, ProcessStatus.RECOVERY_REVIEW_REQUIRED}
        assert retained.task_budget.snapshot()['used']['agents'] == 2
        assert retained.task_budget.snapshot()['used']['provider_calls'] == 1
        retry, rejected = continue_task((cold, restored, reopened, workspace), restored, origin)
        assert retry is None and rejected.get('error')
        assert len(cold.list_processes()) == 2
        assert attempted == [successor.process_id]
    finally:
        reopened.close()


@pytest.mark.parametrize('mode', ['before', 'after'])
def test_initial_checkpoint_unknown_never_claims_unconfirmed_durability(runtime, monkeypatch, mode):
    from backend.core.storage import close_owned_storage
    from tests.test_storage_repository_outcomes import inject
    manager, root, store, workspace = runtime
    root.task_budget.begin_provider_call(root.process_id)
    origin = interrupt(runtime, root)
    checkpoint = store.checkpoint_before_user_turn
    def uncertain(process, **kwargs):
        with inject(store._storage, 'state', mode):
            return checkpoint(process, **kwargs)
    monkeypatch.setattr(store, 'checkpoint_before_user_turn', uncertain)
    history = []
    monkeypatch.setattr(manager, '_record_fork_history', lambda *args: history.append(args))
    successor, result = continue_task(runtime, root, origin)
    assert result['code'] == 'ADOPTED_CONTINUATION_ADMISSION_FAILED'
    assert result['admission_checkpointed'] is False and result['recovery_checkpointed'] is False
    assert result['admission_retained'] and result['persistence_error']
    assert successor.recovery['checkpoint_commit'] == 'unknown'
    assert root.task_budget.snapshot()['used']['agents'] == 2
    assert root.task_budget.snapshot()['used']['provider_calls'] == 1
    assert not history and successor.process_id not in manager._threads
    store.close()
    close_owned_storage()
    reopened = SessionStore(str(workspace), migrate_legacy=False)
    cold = AgentProcessManager()
    try:
        restore_incomplete_processes(reopened, cold, workspace, include_process_ids=[root.process_id])
        # A first checkpoint that never committed cannot promise the new ID
        # survives. No History write or execution occurred; original spend does.
        assert (cold.get(successor.process_id) is not None) == (mode == 'after')
        assert cold.get(root.process_id).task_budget.snapshot()['used']['provider_calls'] == 1
    finally:
        reopened.close()


@pytest.mark.parametrize('fault', ['known', 'quarantine'])
def test_retained_budget_identity_reaches_real_client_and_native_host_response(runtime, monkeypatch, fault):
    import io
    import json
    from backend.core.history import HistoryManager
    from backend.core.native_host import NativeHost
    from backend.core.daemon.client import DaemonClient
    from backend.core.daemon.dispatch import _cmd_loop_status
    from tests.test_storage_repository_outcomes import inject
    manager, root, store, workspace = runtime
    root.status = ProcessStatus.AWAITING_USER
    root.task_budget.begin_provider_call(root.process_id)
    ref = save_discussion(store._storage, 'wire-discussion', 'Additional acceptance',
        {'answer': 'Also cover zero.', 'status': 'completed'},
        [{'process_id': root.process_id, 'task_id': root.active_task_id, 'contract_revision': 0}])
    output = io.StringIO()
    host, client = NativeHost(stdout=output), DaemonClient('budget-fixture')
    context = {'apm': manager, 'dispatcher': object(), 'evq': Queue(),
        'storage': store._storage, 'session_store': store, 'llm': object()}
    session, project = SimpleNamespace(workspace_path=workspace), SimpleNamespace(name='budget-fixture')
    daemon_errors = []
    def deliver(command):
        def emit(event):
            if event.get('event') == 'command_result':
                if event.get('error'): daemon_errors.append(event)
                with client._lock:
                    client._cmd_results[command['request_id']] = event
                    client._cmd_events[command['request_id']].set()
        effective = {**command, 'context_snapshot': {}} if command.get('action') == 'chat' else command
        (_cmd_loop_status if command['cmd'] == 'loop_status' else _cmd_task)(effective, session, project, context, emit)
    monkeypatch.setattr(client, '_write_cmd', deliver)
    monkeypatch.setattr(host, '_get_daemon', lambda *args, **kwargs: client)
    monkeypatch.setattr(host.services, 'config_get', lambda: {})
    original = HistoryManager.add_operation
    monkeypatch.setattr(HistoryManager, '_storage', lambda: store._storage)
    def fail_new_fork(*args, **kwargs):
        if args[1] != 'agent_forked': return original(*args, **kwargs)
        if fault == 'known': raise OSError('fixture History unavailable')
        with inject(store._storage, 'state', 'after'):
            original(*args, **kwargs)
    monkeypatch.setattr(HistoryManager, 'add_operation', fail_new_fork)
    try:
        host._handle_request({'protocol_version': 1, 'type': 'request', 'request_id': 'budget-error-wire',
            'operation': 'runtime.btw.adopt', 'arguments': {'project': project.name,
                'process_id': root.process_id, 'task_id': root.active_task_id,
                'expected_revision': 0, 'sidecar_id': 'wire-discussion',
                'discussion_ref': ref['discussion_ref'], 'mode': 'interrupt'}})
        response = next(json.loads(line) for line in output.getvalue().splitlines()
                        if json.loads(line).get('type') == 'response')
        assert response['ok'] is False
        assert response['error']['code'] == 'ADOPTED_CONTINUATION_ADMISSION_FAILED'
        assert len(daemon_errors) == 1
        raw = daemon_errors[0]
        details = response['error']['details']
        for key in ('process_id', 'task_id', 'admission_retained', 'admission_checkpointed', 'recovery_checkpointed'):
            assert details[key] == raw[key]
        assert details['process_id'] != root.process_id and details['task_id'] == root.active_task_id
        assert details['admission_retained'] is True and details['admission_checkpointed'] is True
        assert details['recovery_checkpointed'] == (fault == 'known')
        assert response['error']['message'] == raw['detail']
        if fault == 'quarantine': assert details['persistence_error'] == raw['persistence_error']
        assert 'event' not in details and 'cmd' not in details
        assert manager.get(details['process_id']).task_budget.snapshot()['used']['provider_calls'] == 1
    finally:
        host.close()
