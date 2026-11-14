"""Construction/history failures must not strand runnable or invisible allocations."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from backend.core.history import HistoryManager
from backend.core.loop.models import ProcessStatus
from backend.core.storage import StorageCommitUnknown
from tests.test_continuation_obligations import delegate, fixture
from tests.test_dag_admission import _nodes
from tests.test_task_contract_recovery import _supervisor


def _history_fault(*, at=1, unknown=False, commit_first=False):
    original = HistoryManager.add_operation
    calls = []
    def write(*args, **kwargs):
        if args[1] == 'agent_forked':
            calls.append(dict(args[3]))
            if len(calls) == at:
                if commit_first:
                    original(*args, **kwargs)
                raise (StorageCommitUnknown('history commit acknowledgement lost') if unknown
                       else OSError('history write unavailable'))
        return original(*args, **kwargs)
    return write, calls


@pytest.mark.parametrize('dag', [False, True])
def test_known_history_failure_releases_only_unpublished_reservation(tmp_path_factory, dag):
    root, tools = _supervisor(tmp_path_factory)
    manager = root._manager
    root.task_budget.begin_provider_call(root.process_id)
    before = root.task_budget.snapshot()
    root_lease = deepcopy(before['process_leases'][root.process_id])
    fault, calls = _history_fault(at=2 if dag else 1)
    with patch.object(HistoryManager, 'add_operation', fault), patch.object(manager, 'start') as start:
        result = tools['delegate_task_dag'].execute({'nodes': _nodes()}) if dag else delegate(tools)
    assert not result['delegated']
    start.assert_not_called()
    assert 'FORK_ADMISSION_FAILED' in str(result)
    assert calls[-1]['process_id'] not in {p.process_id for p in manager.list_processes()}
    assert len(manager.children_of(root.process_id)) == int(dag)
    if dag:
        assert result['execution_state'] == 'not_started'
        assert result['cancelled_before_start_process_ids'] == root.child_ids
        assert manager.get(root.child_ids[0]).status == ProcessStatus.CANCELLED
    after = root.task_budget.snapshot()
    assert after['used']['agents'] == before['used']['agents'] + int(dag)
    assert after['used']['provider_calls'] == before['used']['provider_calls']
    assert after['process_leases'][root.process_id] == root_lease
    for pid, lease in after['process_leases'].items():
        if pid != root.process_id:
            assert lease['state'] == 'released' and lease['used_provider_calls'] == 0
            assert lease['reserved_provider_calls'] == lease['refunded_provider_calls']
    assert len(manager._sessions) == 1 + int(dag)


def test_known_continuation_failure_keeps_previous_session_and_obligation(tmp_path_factory):
    root, tools, old = fixture(tmp_path_factory)
    manager = root._manager
    root.task_budget.begin_provider_call(old.process_id)
    root.task_budget.release_process(old.process_id, status='failed')
    ledger = deepcopy(old.session.host_ledger)
    old_contract = deepcopy(root.delegated_contracts[old.process_id])
    old_budget = deepcopy(root.task_budget.snapshot()['process_leases'][old.process_id])
    fault, _ = _history_fault()
    with patch.object(HistoryManager, 'add_operation', fault), patch.object(manager, 'start'):
        result = delegate(tools, continue_process_id=old.process_id)
    assert not result['delegated']
    assert old.session.host_ledger == ledger
    assert root.delegated_contracts[old.process_id] == old_contract
    assert root.task_budget.snapshot()['process_leases'][old.process_id] == old_budget
    assert manager.children_of(root.process_id) == [old]
    with patch.object(manager, 'start'):
        retried = delegate(tools, continue_process_id=old.process_id)
    assert retried['delegated']  # No invisible execution prevents legitimate retry.


@pytest.mark.parametrize('dag', [False, True])
@pytest.mark.parametrize('commit_first', [False, True])
def test_unknown_history_retains_visible_identity_and_prevents_automatic_allocation(tmp_path_factory, dag, commit_first):
    root, tools = _supervisor(tmp_path_factory)
    manager = root._manager
    fault, calls = _history_fault(at=2 if dag else 1, unknown=True, commit_first=commit_first)
    with patch.object(HistoryManager, 'add_operation', fault), patch.object(manager, 'start') as start:
        result = tools['delegate_task_dag'].execute({'nodes': _nodes()}) if dag else delegate(tools)
    assert not result['delegated'] and result['automatic_replay_suppressed']
    start.assert_not_called()
    retained_id = calls[-1]['process_id']
    assert result['retained_process_ids'] == [retained_id]
    retained = manager.get(retained_id)
    assert retained_id in root.child_ids and retained_id in root.delegated_contracts
    assert retained.status == ProcessStatus.RECOVERY_REVIEW_REQUIRED
    assert retained.recovery['checkpoint_commit'] == 'unknown'
    assert retained.task_budget.snapshot()['process_leases'][retained_id]['state'] == 'active'
    assert root.delegated_contracts[retained_id]['required_for_parent_completion']
    assert root.recovery['fork_admission_unknown_process_ids'] == [retained_id]
    assert retained_id not in manager._threads
    if dag:
        cancelled_id, = result['cancelled_before_start_process_ids']
        assert cancelled_id != retained_id
        assert manager.get(cancelled_id).budget_lease['state'] == 'released'
    records = HistoryManager.load()
    assert any(row.detail.get('process_id') == retained_id for row in records) is commit_first
    before = root.task_budget.snapshot()['used']['agents']
    retry = delegate(tools)
    assert not retry['delegated'] and retry['retained_process_ids'] == [retained_id]
    assert root.task_budget.snapshot()['used']['agents'] == before


def test_unknown_continuation_retains_original_contract_without_claiming_replacement(tmp_path_factory):
    root, tools, old = fixture(tmp_path_factory)
    original = deepcopy(root.delegated_contracts[old.process_id])
    fault, _ = _history_fault(unknown=True)
    with patch.object(HistoryManager, 'add_operation', fault), patch.object(root._manager, 'start'):
        result = delegate(tools, continue_process_id=old.process_id)
    assert root.delegated_contracts[old.process_id] == original
    retained = root._manager.get(result['retained_process_ids'][0])
    assert retained.session is old.session
    assert retained.context_snapshot['task_contract']['continuation']['source_process_id'] == old.process_id
    assert retained.process_id in root.child_ids
    assert not root.delegated_contracts[old.process_id].get('superseded_by')


def test_unexpected_prepare_exception_still_cleans_existing_prepared_siblings(tmp_path_factory):
    root, tools = _supervisor(tmp_path_factory)
    manager = root._manager
    original = manager.fork
    calls = 0
    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise LookupError('unexpected prepare adapter failure')
        return original(*args, **kwargs)
    with patch.object(manager, 'fork', fail_second), patch.object(manager, 'start') as start:
        result = tools['delegate_task_dag'].execute({'nodes': _nodes()})
    start.assert_not_called()
    assert not result['delegated'] and result['execution_state'] == 'not_started'
    assert result['cancelled_before_start_process_ids'] == root.child_ids
    assert all(manager.get(pid).budget_lease['state'] == 'released' for pid in root.child_ids)


def test_failed_new_dag_does_not_release_an_already_running_worker(tmp_path_factory):
    from threading import Event
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.models import RingLevel
    root, tools = _supervisor(tmp_path_factory)
    manager = root._manager
    live = manager.fork(root.process_id, 'executor', ToolRegistry([]), 5, RingLevel.RING_3)
    entered, release = Event(), Event()
    def run():
        live.status = ProcessStatus.RUNNING
        live.task_budget.begin_provider_call(live.process_id)
        entered.set()
        assert release.wait(5)
        live.status = ProcessStatus.COMPLETED
        return {'status': 'completed'}
    manager.start(live.process_id, run)
    try:
        assert entered.wait(3)
        before = deepcopy(root.task_budget.snapshot()['process_leases'][live.process_id])
        fault, _ = _history_fault(at=2)
        with patch.object(HistoryManager, 'add_operation', fault):
            result = tools['delegate_task_dag'].execute({'nodes': _nodes()})
        assert not result['delegated']
        assert manager._threads[live.process_id].is_alive()
        assert live.status == ProcessStatus.RUNNING
        assert root.task_budget.snapshot()['process_leases'][live.process_id] == before
        assert before['state'] == 'active' and before['used_provider_calls'] == 1
    finally:
        release.set()
        manager._threads[live.process_id].join(3)


def test_unknown_continuation_recovery_checkpoint_preserves_cold_identity_and_budget(tmp_path_factory):
    from contextlib import closing
    from backend.core.loop.manager import AgentProcessManager, ForkAdmissionError, SessionStore
    from backend.core.loop.recovery import restore_incomplete_processes
    from backend.core.loop.models import RingLevel
    from backend.core.loop.tools import ToolRegistry
    root, tools, old = fixture(tmp_path_factory)
    manager = root._manager
    old.task_budget.begin_provider_call(old.process_id)
    old.task_budget.release_process(old.process_id, status='failed')
    old_contract = deepcopy(root.delegated_contracts[old.process_id])
    old_ledger = deepcopy(old.session.host_ledger)
    with closing(SessionStore(tmp_path_factory, state_home=tmp_path_factory / 'state')) as store:
        manager.session_store = store
        for process in (root, old):
            store.save_process_checkpoint(process)
        fault, _ = _history_fault(unknown=True, commit_first=True)
        with patch.object(HistoryManager, 'add_operation', fault):
            result = delegate(tools, continue_process_id=old.process_id)
        assert not result['delegated'] and result['recovery_checkpointed']
        retained_id, = result['retained_process_ids']
        retained = manager.get(retained_id)
        before = deepcopy(root.task_budget.snapshot())
        cold = AgentProcessManager()
        restore_incomplete_processes(store, cold, tmp_path_factory,
            include_process_ids=[root.process_id, old.process_id, retained_id])
        owner, source, recovered = (cold.get(pid) for pid in (root.process_id, old.process_id, retained_id))
        assert recovered.status == ProcessStatus.RECOVERY_REVIEW_REQUIRED
        assert recovered.recovery['checkpoint_commit'] == 'unknown'
        assert recovered.session is source.session
        assert all(entry in source.session.host_ledger for entry in old_ledger)
        assert owner.delegated_contracts[old.process_id] == old_contract
        assert owner.delegated_contracts[retained_id]['admission_commit'] == 'unknown'
        assert owner.recovery['fork_admission_unknown_process_ids'] == [retained_id]
        assert recovered.task_budget is owner.task_budget
        after = owner.task_budget.snapshot()
        assert after['used']['agents'] == before['used']['agents']
        assert after['used']['provider_calls'] == before['used']['provider_calls'] == 1
        assert after['process_leases'][old.process_id] == before['process_leases'][old.process_id]
        assert after['process_leases'][retained_id] == before['process_leases'][retained_id]
        with pytest.raises(ForkAdmissionError, match='COMMIT_UNKNOWN'):
            cold.fork(owner.process_id, 'executor', ToolRegistry([]), 5, RingLevel.RING_3)
        assert not cold._threads


def test_failed_recovery_checkpoint_reports_live_only_retention_and_blocks_retry(tmp_path_factory):
    from contextlib import closing
    from backend.core.loop.manager import SessionStore
    root, tools = _supervisor(tmp_path_factory)
    manager = root._manager
    with closing(SessionStore(tmp_path_factory, state_home=tmp_path_factory / 'state')) as store:
        manager.session_store = store
        store.save_process_checkpoint(root)
        fault, calls = _history_fault(unknown=True, commit_first=True)
        with patch.object(HistoryManager, 'add_operation', fault), patch.object(
                store, 'save_process_checkpoint', side_effect=OSError('recovery storage still unavailable')):
            result = delegate(tools)
            retained_id, = result['retained_process_ids']
            assert not result['recovery_checkpointed']
            assert result['persistence_error'] == 'recovery storage still unavailable'
            before = root.task_budget.snapshot()['used']['agents']
            retry = delegate(tools)
        assert not retry['delegated'] and not retry['recovery_checkpointed']
        assert retry['automatic_replay_suppressed']
        assert retry['retained_process_ids'] == [retained_id]
        assert len(calls) == 1  # The uncertain write itself was never replayed.
        assert root.task_budget.snapshot()['used']['agents'] == before
        assert manager.get(retained_id).status == ProcessStatus.RECOVERY_REVIEW_REQUIRED
        assert manager.get(retained_id).budget_lease['state'] == 'active'
        assert store.load_process_state(retained_id) is None  # No invented cold guarantee.
