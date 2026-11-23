"""Exercise the real daemon root runner and its durable budget closeout."""
from queue import Queue
from types import SimpleNamespace

import pytest

from backend.core.daemon.dispatch import _cmd_task
from backend.core.loop.lifecycle import transition
from backend.core.loop.models import ProcessStatus
from tests.test_adopted_task_budget import runtime


def admitted_root(runtime, monkeypatch, target, *, durable=True):
    manager, _, store, workspace = runtime
    monkeypatch.setattr('backend.core.loop.executor.agent_step', target)
    events = []
    _cmd_task({'action': 'chat', 'task_id': 'root-closeout-task', 'instruction': 'Bounded fixture',
        'context_snapshot': {}, 'role': 'supervisor', 'actor_kind': 'supervisor',
        'capability_profile_id': 'supervisor.control', 'max_steps': 4},
        SimpleNamespace(workspace_path=workspace), SimpleNamespace(name='root-closeout'),
        {'apm': manager, 'dispatcher': object(), 'llm': object(), 'evq': Queue(),
         'storage': store._storage, 'session_store': store if durable else None}, events.append)
    ack = events[-1]['result']
    assert ack['status'] == 'pending'
    process = manager.get(ack['process_id'])
    assert process._run_lock.locked() and not process.lifecycle_finalized
    assert process.task_budget.snapshot()['process_leases'][process.process_id]['state'] == 'active'
    return process, manager._threads[process.process_id]._target


def result_for(process, status):
    process.task_budget.begin_provider_call(process.process_id)
    process.task_budget.consume_output_estimate(9, process.process_id)
    transition(process, status)
    outcome_status = {ProcessStatus.WAITING: 'awaiting_user', ProcessStatus.KILLED: 'cancelled'}.get(status, status.value)
    process.result = {'status': outcome_status,
                      'process_id': process.process_id,
                      'task_id': process.active_task_id, 'response': 'fixture result'}
    return process.result


@pytest.mark.parametrize('status', [ProcessStatus.COMPLETED, ProcessStatus.CANCELLED,
    ProcessStatus.FAILED, ProcessStatus.KILLED, ProcessStatus.TIMED_OUT])
def test_terminal_root_releases_lease_in_its_durable_checkpoint(runtime, monkeypatch, status):
    _, _, store, _ = runtime
    def target(process, *args, **kwargs):
        assert process._run_lock.locked() and not process.lifecycle_finalized
        assert process.task_budget.snapshot()['process_leases'][process.process_id]['state'] == 'active'
        return result_for(process, status)
    process, run = admitted_root(runtime, monkeypatch, target)
    run()
    assert process.status == status
    assert not process._run_lock.locked() and process.lifecycle_finalized
    snapshot = process.task_budget.snapshot()
    assert snapshot['process_leases'][process.process_id]['state'] == 'released'
    assert snapshot['used']['provider_calls'] == 1 and snapshot['used']['estimated_output_tokens'] == 9
    stored = store.load_process_state(process.process_id)['runtime_state']
    assert stored['lifecycle_finalized'] is True
    assert stored['task_budget']['process_leases'][process.process_id]['state'] == 'released'
    assert stored['task_budget']['used']['provider_calls'] == 1


@pytest.mark.parametrize('status', [ProcessStatus.AWAITING_USER, ProcessStatus.WAITING])
def test_resumable_root_pause_keeps_the_same_active_lease(runtime, monkeypatch, status):
    _, _, store, _ = runtime
    process, run = admitted_root(runtime, monkeypatch, lambda process, *a, **kw: result_for(process, status))
    run()
    assert process.status == status
    assert process.task_budget.snapshot()['process_leases'][process.process_id]['state'] == 'active'
    stored = store.load_process_state(process.process_id)['runtime_state']['task_budget']
    assert stored['process_leases'][process.process_id]['state'] == 'active'
    assert stored['used']['provider_calls'] == 1


@pytest.mark.parametrize('unknown', [False, True])
def test_failed_terminal_checkpoint_does_not_claim_reliable_root_finality(runtime, monkeypatch, unknown):
    from backend.core.storage import StorageCommitUnknown
    _, _, store, _ = runtime
    process, run = admitted_root(runtime, monkeypatch, lambda process, *a, **kw: result_for(process, ProcessStatus.CANCELLED))
    original = store.save_process_checkpoint
    def fail(candidate, **kwargs):
        if candidate is process:
            raise StorageCommitUnknown('fixture terminal checkpoint') if unknown else OSError('fixture disk failure')
        return original(candidate, **kwargs)
    monkeypatch.setattr(store, 'save_process_checkpoint', fail)
    run()
    assert not process._run_lock.locked()
    assert process.lifecycle_finalized is False
    assert process.result['error']['code'] == 'SESSION_CHECKPOINT_FAILED'
    # The earlier admission is the last confirmed durable record, not a claim
    # that the terminal budget release committed.
    stored = store.load_process_state(process.process_id)['runtime_state']
    assert stored['lifecycle_finalized'] is False
    assert stored['task_budget']['process_leases'][process.process_id]['state'] == 'active'
    assert process.task_budget.snapshot()['used']['provider_calls'] == 1


def test_in_memory_root_closeout_retains_existing_finality_without_claiming_a_checkpoint(runtime, monkeypatch):
    _, _, store, _ = runtime
    process, run = admitted_root(runtime, monkeypatch,
        lambda process, *a, **kw: result_for(process, ProcessStatus.COMPLETED), durable=False)
    run()
    assert process.lifecycle_finalized and not process._run_lock.locked()
    assert process.task_budget.snapshot()['process_leases'][process.process_id]['state'] == 'released'
    assert store.load_process_state(process.process_id) is None
