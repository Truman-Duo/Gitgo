"""Consumers share the durable child closeout boundary, including failure."""
import threading
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.core.daemon.task_observations import TaskObservations
from backend.core.loop.completion_protocol import HostCompletionEvaluator
from backend.core.loop.delivery_runtime import DeliveryRuntime
from backend.core.loop.executor import _build_internal_tools
from backend.core.loop.lifecycle import execution_closeout, execution_closeout_from_state
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.tools import ToolRegistry
from tests.test_task_contract_recovery import _supervisor


def _delivery(process):
    return DeliveryRuntime(process, process.session, None, None,
        emit=lambda event: None, apply_mailbox=lambda: 0,
        pause_blocked=lambda *args: None, capture_recovery=lambda p: None,
        increment_nudge=lambda *args: None, nudge_count=lambda *args: 0)


def _finish(process):
    return _delivery(process).finish('The requested facts are present.', 0,
        {'action': 'continue', 'usage_ratio': 0}, source='test_provider')


@pytest.mark.parametrize('checkpoint_fails', [False, True])
def test_real_child_cannot_be_consumed_until_terminal_checkpoint(tmp_path_factory, checkpoint_fails):
    root, _ = _supervisor(tmp_path_factory)
    manager = root._manager
    entered, release = threading.Event(), threading.Event()

    class Store:
        def save_process_checkpoint(self, process, **kwargs):
            if kwargs.get('lifecycle_finalized'):
                entered.set()
                assert release.wait(5)
                if checkpoint_fails:
                    raise RuntimeError('injected terminal checkpoint failure')

    observer = TaskObservations(root, manager, Store(),
        SimpleNamespace(append=lambda event, **kwargs: event), Queue())
    tools = _build_internal_tools(root, {}, str(tmp_path_factory), object(),
        SimpleNamespace(_executors={}), observer.persist)
    with patch('backend.core.loop.executor.agent_step', lambda **kw: _finish(kw['process']).result):
        delegated = tools['delegate_task'].execute({
            'task_description': 'Inspect file facts', 'target_files': [],
            'acceptance_criteria': ['Report facts'],
            'capability_profile_id': 'governance.observe', 'task_kind': 'answer'})
        assert delegated['delegated'], delegated
        child = manager.get(delegated['process_id'])
        try:
            assert entered.wait(5)
            listing = tools['list_agents'].execute({})['agents'][0]
            assert listing['status'] == 'completed'
            assert listing['closeout_state'] == 'pending'
            assert listing['lifecycle_finalized'] is False
            waited = tools['wait_agents'].execute({'process_ids': [child.process_id], 'timeout': 0.01})
            assert not waited['all_terminal']
            assert waited['agents'][child.process_id]['closeout_state'] == 'pending'
            assert not tools['review_child_outcome'].execute({
                'process_id': child.process_id, 'verdict': 'approved', 'summary': 'Facts present'})['accepted']
            # An approval copied from history cannot bypass the completion or
            # promotion boundary either.
            root.child_reviews[child.process_id] = {'verdict': 'approved', 'summary': 'old approval'}
            assert not HostCompletionEvaluator.evaluate(root, 'Facts present').allowed
            assert _finish(root).retry
            assert root.status != ProcessStatus.COMPLETED
            with patch.object(manager, 'worktree_manager', SimpleNamespace()):
                with pytest.raises(ValueError, match='closeout is not finalized'):
                    manager.promote_process_results(root, [child.process_id])
        finally:
            release.set()
            manager._threads[child.process_id].join(5)
        assert not manager._threads[child.process_id].is_alive()
        settled = manager.wait_many([child.process_id], timeout=0)
        assert settled['all_terminal']
        assert settled['processes'][child.process_id]['closeout_state'] == ('failed' if checkpoint_fails else 'finalized')
        if checkpoint_fails:
            assert child.result['code'] == 'SESSION_CHECKPOINT_FAILED'
            assert not child.lifecycle_finalized
            assert not HostCompletionEvaluator.evaluate(root, 'Facts present').allowed
        else:
            assert tools['review_child_outcome'].execute({
                'process_id': child.process_id, 'verdict': 'approved', 'summary': 'Facts present'})['accepted']
            assert _finish(root).result['status'] == 'completed'


def test_failed_checkpoint_blocks_dependency_without_hanging(tmp_path_factory):
    root, _ = _supervisor(tmp_path_factory)
    manager = root._manager
    upstream = manager.fork(root.process_id, 'executor', ToolRegistry([]), 4, RingLevel.RING_3)
    downstream = manager.fork(root.process_id, 'executor', ToolRegistry([]), 4, RingLevel.RING_3,
                              depends_on=[upstream.process_id])
    called = threading.Event()

    def target():
        upstream.status = ProcessStatus.COMPLETED
        return {'status': 'completed'}

    def fail_checkpoint():
        raise RuntimeError('injected checkpoint failure')

    manager.start(upstream.process_id, target, on_terminal=fail_checkpoint)
    manager._threads[upstream.process_id].join(3)
    manager.start(downstream.process_id, lambda: called.set())
    manager._threads[downstream.process_id].join(3)
    try:
        assert not manager._threads[downstream.process_id].is_alive()
        assert not called.is_set()
        assert downstream.result['code'] == 'UPSTREAM_DEPENDENCY_FAILED'
        assert 'closeout failed' in downstream.result['error']
        assert manager.wait_many([upstream.process_id, downstream.process_id], timeout=0)['all_terminal']
        assert not upstream.lifecycle_finalized
    finally:
        manager.kill(downstream.process_id, reason='test_cleanup')
        manager._threads[downstream.process_id].join(3)


@pytest.mark.parametrize('runtime, result, expected', [
    ({}, {}, 'pending'),
    ({'lifecycle_finalized': False}, {}, 'pending'),
    ({'lifecycle_finalized': True}, {}, 'finalized'),
    ({'lifecycle_finalized': False}, {'code': 'SESSION_CHECKPOINT_FAILED'}, 'failed'),
])
def test_durable_projection_never_invents_closeout(runtime, result, expected):
    assert execution_closeout_from_state('completed', runtime, result)['closeout_state'] == expected


def test_restored_unfinalized_checkpoint_is_not_legacy_synchronous_completion(tmp_path_factory):
    root, _ = _supervisor(tmp_path_factory)
    root.status = ProcessStatus.COMPLETED
    assert execution_closeout(root)['closeout_state'] == 'finalized'
    root.durable_revision = 1
    assert execution_closeout(root)['closeout_state'] == 'pending'
