"""Real State transactions with bounded faults at acknowledgement boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import queue
import json
import sqlite3
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.core.daemon import dispatch
from backend.core.loop.lifecycle import block_uncertain_checkpoint
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import AgentProcess, ProcessStatus, RingLevel
from backend.core.loop.tools import ToolRegistry
from backend.core.storage import StorageCommitUnknown, StorageRuntime
from backend.core.storage.transactions import authoritative_transaction


class ResponseLost(RuntimeError):
    pass


class FaultConnection:
    def __init__(self, actual, mode):
        self.actual, self.mode, self.fired = actual, mode, False

    def execute(self, sql, *args):
        statement = sql.strip().upper()
        if statement == 'COMMIT' and not self.fired and self.mode in {'before', 'after'}:
            self.fired = True
            if self.mode == 'after':
                self.actual.execute(sql, *args)
            raise ResponseLost('fixture response gap')
        if statement == 'ROLLBACK' and self.mode == 'rollback':
            raise OSError('fixture rollback failure')
        if statement == 'BEGIN IMMEDIATE' and self.mode == 'begin_after':
            self.actual.execute(sql, *args)
            raise ResponseLost('fixture begin response gap')
        return self.actual.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.actual, name)


@pytest.fixture
def runtime(tmp_path_factory):
    workspace = tmp_path_factory / 'work'
    workspace.mkdir()
    with StorageRuntime(workspace, state_home=tmp_path_factory / 'state') as storage:
        yield storage


@contextmanager
def inject(runtime, mode):
    actual = runtime._state
    wrapped = FaultConnection(actual, mode)
    runtime._state = wrapped
    try:
        yield wrapped
    finally:
        runtime._state = actual


def checkpoint():
    return {'process_id': 'proc', 'session_id': 'sess', 'task_id': 'task',
            'status': 'running', 'expected_revision': 0, 'messages': [], 'runtime_state': {}}


@pytest.mark.parametrize('mode', ['before', 'after'])
@pytest.mark.parametrize('operation', ['claim', 'result', 'checkpoint'])
def test_commit_response_is_unknown_and_writer_is_quarantined(runtime, mode, operation):
    if operation == 'result':
        runtime.claim_command('call', 'a' * 64)
    operation_fn = {
        'claim': lambda: runtime.claim_command('call', 'a' * 64),
        'result': lambda: runtime.complete_command('call', 'a' * 64, {'sample': 'fixture'}),
        'checkpoint': lambda: runtime.save_agent_checkpoint(checkpoint()),
    }[operation]
    saved_cursor = runtime._state.execute('SELECT 1')
    with inject(runtime, mode) as wrapped:
        with pytest.raises(StorageCommitUnknown) as caught:
            operation_fn()
        assert wrapped.fired and caught.value.stage == 'commit'
        assert type(caught.value.__cause__) is ResponseLost
        assert str(caught.value) == 'STORAGE_COMMIT_UNKNOWN'
        assert runtime._state.quarantined
        for forbidden in (lambda: saved_cursor.fetchone(),
                          lambda: runtime._state.execute('SELECT 1'),
                          lambda: runtime._state.cursor(), runtime._state.commit,
                          runtime._state.rollback,
                          lambda: runtime.put_blob(b'not persisted after quarantine'),
                          lambda: runtime.put_state_ref('fixture', 'key', 'value')):
            with pytest.raises(StorageCommitUnknown):
                forbidden()
        saved_cursor.close()
        health = runtime.check_health(force_publish=True)
        assert health.level.value == 'blocked' and 'state_commit_unknown' in health.reasons
    # A fresh read-only connection still observes committed identity, not a new claim.
    found = runtime.lookup_command('call', 'a' * 64)
    assert found['status'] == ('absent' if operation == 'checkpoint' or (operation == 'claim' and mode == 'before') else 'existing')
    if operation == 'result':
        assert (found['result_ref'] is not None) == (mode == 'after')
    if operation == 'checkpoint':
        reader = sqlite3.connect(runtime.paths.state_db.as_uri() + '?mode=ro', uri=True)
        try:
            assert reader.execute('SELECT COUNT(*) FROM session_processes').fetchone()[0] == (mode == 'after')
            assert reader.execute('SELECT COUNT(*) FROM runtime_outbox').fetchone()[0] == (mode == 'after')
        finally:
            reader.close()
    with pytest.raises(StorageCommitUnknown):
        runtime.claim_command('different-call', 'b' * 64)


def test_body_failure_rolls_back_and_preserves_original_error(runtime):
    runtime._state.execute("CREATE TRIGGER reject_claim BEFORE INSERT ON command_receipts BEGIN SELECT RAISE(ABORT,'fixture rejection'); END")
    with pytest.raises(sqlite3.IntegrityError, match='fixture rejection'):
        runtime.claim_command('call', 'a' * 64)
    assert not runtime._state.quarantined
    assert runtime.lookup_command('call', 'a' * 64) == {'status': 'absent'}
    runtime._state.execute('DROP TRIGGER reject_claim')
    assert runtime.claim_command('call', 'a' * 64)['admitted']


def test_rollback_failure_retains_primary_error_and_blocks_writer(runtime):
    with inject(runtime, 'rollback'):
        with pytest.raises(StorageCommitUnknown) as caught:
            with authoritative_transaction(runtime._state):
                raise ValueError('fixture primary error')
        assert caught.value.stage == 'rollback' and caught.value.rollback_failed
        assert type(caught.value.__cause__) is ValueError
        assert runtime._state.quarantined


def test_lost_begin_response_rolls_back_owned_transaction(runtime):
    with inject(runtime, 'begin_after'):
        with pytest.raises(ResponseLost):
            with authoritative_transaction(runtime._state):
                pytest.fail('body must not run')
    assert not runtime._state.in_transaction and not runtime._state.quarantined


def test_nested_transaction_is_rejected_without_rolling_back_its_owner(runtime):
    runtime._state.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(RuntimeError, match='ALREADY_ACTIVE'):
            with authoritative_transaction(runtime._state):
                pytest.fail('body must not run')
        assert runtime._state.in_transaction
    finally:
        runtime._state.execute('ROLLBACK')


def test_existing_receipt_and_conflict_remain_queryable_without_write_capacity(runtime, monkeypatch):
    runtime.claim_command('call', 'a' * 64)
    runtime.complete_command('call', 'a' * 64, {'sample': 'fixture'})
    def forbidden(*args, **kwargs):
        pytest.fail('read-only lookup attempted a write')
    monkeypatch.setattr(runtime, '_reserve_authoritative_write', forbidden)
    monkeypatch.setattr(runtime, '_write_blob_file', forbidden)
    assert runtime.lookup_command('call', 'a' * 64)['status'] == 'existing'
    assert runtime.lookup_command('absent', 'a' * 64) == {'status': 'absent'}
    assert runtime.lookup_command('call', 'b' * 64) == {'status': 'conflict'}
    assert runtime.claim_command('call', 'a' * 64)['result'] == {'sample': 'fixture'}


def test_lookup_cannot_create_missing_database(runtime, monkeypatch, tmp_path_factory):
    original = runtime.paths
    missing = tmp_path_factory / 'does-not-exist.sqlite3'
    monkeypatch.setattr(runtime, 'paths', SimpleNamespace(state_db=missing))
    assert runtime.lookup_command('call', 'a' * 64) == {'status': 'unknown'}
    assert not missing.exists()
    monkeypatch.setattr(runtime, 'paths', original)


def test_read_snapshot_absence_does_not_bypass_final_atomic_claim(runtime):
    with StorageRuntime(runtime.paths.workspace, state_home=runtime.paths.state_home) as second:
        barrier = threading.Barrier(2, timeout=5)
        def claim(storage):
            assert storage.lookup_command('race', 'a' * 64)['status'] == 'absent'
            barrier.wait()
            return storage.claim_command('race', 'a' * 64)['admitted']
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim, storage) for storage in (runtime, second)]
            assert sum(future.result(timeout=10) for future in futures) == 1


@pytest.mark.parametrize('mode', ['before', 'after'])
def test_dispatch_never_runs_handler_after_unknown_claim(runtime, monkeypatch, mode):
    emitted, effects = [], []
    monkeypatch.setattr(dispatch, '_emit_global', emitted.append)
    monkeypatch.setitem(dispatch.COMMAND_HANDLERS, 'dispatch_tool', lambda *args: effects.append('ran'))
    with inject(runtime, mode):
        dispatch._handle_command({'cmd': 'dispatch_tool', 'command_id': 'call', 'request_id': 'request'},
                                 None, None, {'storage': runtime})
    assert not effects
    assert emitted[-1]['error'] == 'STORAGE_COMMIT_UNKNOWN'
    assert emitted[-1]['request_id'] == 'request'
    assert emitted[-1]['details']['automatic_replay_suppressed']


def test_dispatch_result_unknown_does_not_repeat_effect(runtime, monkeypatch):
    emitted, effects = [], []
    monkeypatch.setattr(dispatch, '_emit_global', emitted.append)
    def handler(cmd, session, project, context, emit):
        effects.append('ran')
        with inject(runtime, 'after'):
            emit({'event': 'command_result', 'cmd': 'dispatch_tool', 'result': {'sample': 'fixture'}})
    monkeypatch.setitem(dispatch.COMMAND_HANDLERS, 'dispatch_tool', handler)
    cmd = {'cmd': 'dispatch_tool', 'command_id': 'call'}
    dispatch._handle_command(cmd, None, None, {'storage': runtime})
    assert emitted[-1]['error'] == 'STORAGE_COMMIT_UNKNOWN'
    dispatch._handle_command(cmd, None, None, {'storage': runtime})
    assert effects == ['ran']
    assert emitted[-1]['result'] == {'sample': 'fixture'}


def test_unknown_checkpoint_blocks_decision_instead_of_republishing_card(tmp_path_factory):
    from backend.core.loop.decision_support import create_user_decision
    manager = AgentProcessManager()
    process = manager.fork(parent_id=None, role='worker', tool_registry=ToolRegistry([]),
                           max_steps=4, ring_level=RingLevel.RING_3,
                           workspace_path=str(tmp_path_factory), task_id='task')
    create_user_decision(process, {'question': 'Fixture choice',
        'why_user_must_decide': 'The user chooses the fixture scope',
        'options': [{key: label for key in ('label', 'principle', 'immediate_effect',
                     'downstream_effect', 'risks', 'reversibility')} for label in ('A', 'B')]})
    process.status = ProcessStatus.AWAITING_USER
    pending = process.pending_decision
    process.approval_grants = [{'fixture': 'stale grant'}]
    class Store:
        def save_process_checkpoint(self, active):
            raise StorageCommitUnknown('commit')
    emitted = []
    command = {'action': 'decision', 'process_id': process.process_id,
        'decision_id': pending['decision_id'], 'instruction': 'Choose option 1: A'}
    context = {'apm': manager, 'session_store': Store(), 'evq': queue.Queue()}
    dispatch._cmd_task(command, SimpleNamespace(workspace_path=tmp_path_factory),
                       SimpleNamespace(name='fixture'), context, emitted.append)
    assert emitted[-1]['error'] == 'DECISION_COMMIT_UNKNOWN'
    assert process.status is ProcessStatus.RECOVERY_REVIEW_REQUIRED
    assert process.recovery['resume_forbidden'] and not manager._threads
    assert not process.approval_grants and process.pending_decision is None
    dispatch._cmd_task(command, None, None, context, emitted.append)
    assert emitted[-1]['error'].startswith('PROCESS_NOT_AWAITING_USER')


def test_session_store_blocks_process_and_retains_old_revision_on_unknown():
    process = AgentProcess('proc', 'worker', RingLevel.RING_3)
    process.session = SimpleNamespace()
    store = SessionStore.__new__(SessionStore)
    def lost(active, *, lifecycle_finalized=None):
        raise StorageCommitUnknown('commit')
    store._save_process_checkpoint = lost
    process.durable_revision = 5
    with pytest.raises(StorageCommitUnknown):
        store.save_process_checkpoint(process)
    assert process.durable_revision == 5 and process.recovery['resume_forbidden']
    assert process.status is ProcessStatus.RECOVERY_REVIEW_REQUIRED


def test_unknown_checkpoint_does_not_reopen_terminal_process():
    process = AgentProcess('proc', 'worker', RingLevel.RING_3)
    process.status = ProcessStatus.COMPLETED
    block_uncertain_checkpoint(process)
    assert process.status is ProcessStatus.COMPLETED
    assert process.recovery['resume_forbidden']


def test_committed_reader_cannot_write_or_claim(runtime):
    with runtime._committed_state_reader() as reader:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            reader.execute("INSERT INTO command_receipts(command_key,fingerprint,created_at) VALUES('fixture','fixture','fixture')")
    assert runtime.lookup_command('fixture', 'a' * 64) == {'status': 'absent'}


@pytest.mark.skipif(sys.platform not in {'win32', 'linux'}, reason='Native read pins required')
@pytest.mark.parametrize('mode', ['before', 'after'])
def test_real_permission_checkpoint_gap_preserves_state_but_revokes_local_pins(runtime, mode):
    from backend.core.loop.agent_tool import ApprovalMode
    from backend.core.loop.permission_broker import create_permission_request
    from backend.core.tools.catalog import build_workspace_tools
    root = runtime.paths.workspace
    (root / 'note.txt').write_text('fictional note', encoding='utf8')
    tool = replace(build_workspace_tools(root)['read_file'], approval=ApprovalMode.ASK)
    manager = AgentProcessManager()
    process = manager.fork(parent_id=None, role='worker', tool_registry=ToolRegistry([tool.name]),
                           max_steps=4, ring_level=RingLevel.RING_3,
                           workspace_path=str(root), task_id='fixture-task')
    create_permission_request(process, {'tool_name': tool.name,
        'arguments': {'path': 'note.txt'}, 'resource': 'capability://read_file',
        'purpose': 'Read the fictional note'}, {tool.name: tool}, str(root))
    process.status = ProcessStatus.AWAITING_USER
    decision_id = process.pending_decision['decision_id']
    pin = next(iter(process._read_approval_pins.values()))
    store = SessionStore(str(root), storage=runtime, migrate_legacy=False)
    store.save_process_checkpoint(process)
    emitted = []
    with inject(runtime, mode):
        dispatch._cmd_task({'action': 'decision', 'process_id': process.process_id,
            'decision_id': decision_id, 'instruction': 'Choose option 1: Allow once'},
            SimpleNamespace(workspace_path=root), SimpleNamespace(name='fixture'),
            {'apm': manager, 'session_store': store, 'evq': queue.Queue()}, emitted.append)
    assert emitted[-1]['error'] == 'DECISION_COMMIT_UNKNOWN'
    assert process.recovery['resume_forbidden'] and not manager._threads
    assert not process.approval_grants and not process._read_approval_pins and not pin.live()
    assert process.durable_revision == 1
    with runtime._committed_state_reader() as reader:
        revision, state_ref = reader.execute(
            'SELECT state_revision,runtime_state_ref FROM session_processes WHERE process_id=?',
            (process.process_id,)).fetchone()
    assert revision == (2 if mode == 'after' else 1)
    state = json.loads(runtime.read_blob(state_ref))
    assert len(state['approval_grants']) == (1 if mode == 'after' else 0)
    assert (state['pending_decision'] is None) == (mode == 'after')
    # The local blocked projection never rewrites either durable outcome.
    assert process.status is ProcessStatus.RECOVERY_REVIEW_REQUIRED
