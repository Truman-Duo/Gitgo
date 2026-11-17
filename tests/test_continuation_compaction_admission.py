"""Shared context changes only after the single active-session lease is admitted."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from queue import Queue
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from backend.core.daemon.dispatch import _cmd_task
from backend.core.loop.session import AgentSession
from backend.core.loop.trace import TraceJournal
from tests.test_continuation_obligations import _accepted_original, _direct_user_turn


class _ParkedThread:
    """Keep real admission/thread registration, without running a provider."""
    ident = None

    def __init__(self, **kwargs):
        self.name = kwargs.get('name', '')

    def start(self):
        pass

    def is_alive(self):
        return False


def _large_context(session):
    session.append_user('Public intent ' + 'x' * 280000)
    session.append_assistant('Existing answer', continuation_state={'reasoning_content': 'active-private-state'})
    session.provider_context_cache = {'existing': {'value': 'cache'}}
    session.context_flags.add('keep-flag')


def _context(session):
    return deepcopy({key: getattr(session, key) for key in (
        'messages', 'provider_state', 'context_epoch', 'epoch_archive',
        'context_flags', 'provider_context_cache', 'compiled_prompt_hash', 'compiled_prompt_sections')})


def _command(manager, old, workspace, task_id):
    events = []
    _cmd_task({'action': 'chat', 'process_id': old.process_id, 'task_id': task_id,
        'context_snapshot': {}, 'instruction': 'Continue this conversation', 'fresh_task_budget': True},
        SimpleNamespace(workspace_path=workspace), SimpleNamespace(name='test'),
        {'apm': manager, 'dispatcher': object(), 'llm': object(), 'evq': Queue()}, events.append)
    return events


def test_rejected_old_process_request_does_not_compact_active_successor_context(tmp_path_factory):
    root, _, old = _accepted_original(tmp_path_factory)
    active = _direct_user_turn(root._manager, old, tmp_path_factory)
    _large_context(active.session)
    before = _context(active.session)
    with patch.object(AgentSession, 'compact_completed_task_boundary', autospec=True) as compact:
        events = _command(root._manager, old, tmp_path_factory, 'rejected-repetition')
    assert 'SESSION_EXECUTION_ACTIVE' in events[-1]['error']
    compact.assert_not_called()
    assert _context(active.session) == before
    assert len(root._manager.list_processes()) == 3


def test_legal_compaction_occurs_after_fork_before_thread_registration(tmp_path_factory):
    root, _, old = _accepted_original(tmp_path_factory)
    _large_context(old.session)
    method = AgentSession.compact_completed_task_boundary
    seen = []
    def compact(session, **kwargs):
        successors = [p for p in root._manager.list_processes()
                      if p.session is old.session and p.process_id != old.process_id]
        assert len(successors) == 1
        assert successors[0].process_id not in root._manager._threads
        seen.append(successors[0].process_id)
        return method(session, **kwargs)
    with patch('backend.core.daemon.dispatch.threading', SimpleNamespace(Thread=_ParkedThread)), \
            patch.object(AgentSession, 'compact_completed_task_boundary', compact):
        events = _command(root._manager, old, tmp_path_factory, 'valid-boundary')
    assert events[-1]['result']['status'] == 'pending'
    new = root._manager.get(events[-1]['result']['process_id'])
    new._run_lock.release()
    assert seen == [new.process_id]
    assert old.session.context_epoch == 1
    assert old.session.provider_state == {}
    assert len(old.session.epoch_archive) == 1
    assert any(event['event'] == 'context_compaction_completed' for event in events)


def test_mid_compaction_failure_keeps_context_and_one_admitted_budget_then_starts(tmp_path_factory):
    root, _, old = _accepted_original(tmp_path_factory)
    _large_context(old.session)
    before = _context(old.session)
    original = AgentSession.begin_context_epoch
    def fail_after_epoch(session, **kwargs):
        original(session, **kwargs)
        assert session.context_epoch == before['context_epoch'] + 1
        raise RuntimeError('injected after context containers were changed')
    with patch('backend.core.daemon.dispatch.threading', SimpleNamespace(Thread=_ParkedThread)), \
            patch.object(AgentSession, 'begin_context_epoch', fail_after_epoch):
        events = _command(root._manager, old, tmp_path_factory, 'failed-compaction')
    assert events[-1]['result']['status'] == 'pending'
    new = root._manager.get(events[-1]['result']['process_id'])
    new._run_lock.release()
    assert _context(old.session) == before
    assert len(root._manager.list_processes()) == 3
    assert new.process_id in root._manager._threads
    assert new.task_budget is not root.task_budget
    assert new.task_budget.snapshot()['used']['provider_calls'] == 0
    assert len(new.task_budget.snapshot()['process_leases']) == 1
    failure = next(event for event in events if event['event'] == 'context_compaction_failed')
    assert failure['error']['details']['continuing_without_compaction'] is True
    assert not any(event['event'] == 'context_compaction_completed' for event in events)
    records = TraceJournal(tmp_path_factory, 'failed-compaction').read()['events']
    assert any(row['event'] == 'context_compaction_failed' for row in records)


def test_two_concurrent_requests_admit_and_compact_exactly_once(tmp_path_factory):
    root, _, old = _accepted_original(tmp_path_factory)
    manager = root._manager
    _large_context(old.session)
    enter = Barrier(2)
    real_fork = manager.fork
    def simultaneous_fork(*args, **kwargs):
        enter.wait(timeout=5)
        return real_fork(*args, **kwargs)  # real manager lock decides admission
    with patch('backend.core.daemon.dispatch.threading', SimpleNamespace(Thread=_ParkedThread)), \
            patch.object(manager, 'fork', simultaneous_fork):
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda i: _command(manager, old, tmp_path_factory, f'concurrent-{i}'), range(2)))
    accepted = [events for events in outcomes if events[-1].get('result', {}).get('status') == 'pending']
    rejected = [events for events in outcomes if 'SESSION_EXECUTION_ACTIVE' in events[-1].get('error', '')]
    assert len(accepted) == len(rejected) == 1
    new = manager.get(accepted[0][-1]['result']['process_id'])
    new._run_lock.release()
    assert len(manager.list_processes()) == 3
    assert old.session.context_epoch == 1 and len(old.session.epoch_archive) == 1
    assert sum(event['event'] == 'context_compaction_completed' for events in outcomes for event in events) == 1
