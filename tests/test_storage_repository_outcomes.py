"""Exercise repository APIs against real SQLite at uncertain commit boundaries."""
from contextlib import contextmanager
import hashlib
import os
import sqlite3

import pytest

from backend.core.storage import StorageCommitUnknown, StorageRuntime


class ResponseLost(RuntimeError):
    pass


class FaultConnection:
    def __init__(self, actual, mode, error_type=ResponseLost, before_begin=None):
        self.actual, self.mode = actual, mode
        self.error_type, self.before_begin = error_type, before_begin
        self.fired = False

    def execute(self, sql, *args):
        statement = sql.strip().upper()
        if statement == 'BEGIN IMMEDIATE' and self.before_begin is not None:
            hook, self.before_begin = self.before_begin, None
            hook(self.actual)
        if statement == 'COMMIT' and not self.fired and self.mode in {'before', 'after'}:
            self.fired = True
            if self.mode == 'after':
                self.actual.execute(sql, *args)
            raise self.error_type('fixture commit response gap')
        if statement == 'ROLLBACK' and self.mode == 'rollback':
            self.fired = True
            raise OSError('fixture rollback failure')
        return self.actual.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.actual, name)


@pytest.fixture
def runtime(tmp_path_factory):
    workspace = tmp_path_factory / 'work'
    workspace.mkdir()
    with StorageRuntime(workspace, state_home=tmp_path_factory / 'state',
                        exclusive_maintenance=True) as storage:
        yield storage


@contextmanager
def inject(runtime, database, mode, **kwargs):
    attribute = '_' + database
    actual = getattr(runtime, attribute)
    wrapped = FaultConnection(actual, mode, **kwargs)
    setattr(runtime, attribute, wrapped)
    try:
        yield wrapped
    finally:
        setattr(runtime, attribute, actual)


def committed(runtime, database, query):
    path = getattr(runtime.paths, database + '_db')
    reader = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        return reader.execute(query).fetchone()[0]
    finally:
        reader.close()


def seed_processes(runtime):
    for process_id in ('root', 'child'):
        role = 'supervisor' if process_id == 'root' else 'worker'
        runtime.save_agent_checkpoint({
            'process_id': process_id, 'parent_id': '' if process_id == 'root' else 'root',
            'session_id': 'session-' + process_id, 'task_id': 'task-' + process_id,
            'status': 'completed', 'role': role, 'actor_kind': role,
            'messages': [], 'runtime_state': {},
        })


def seed_orphan(runtime):
    content = b'fixture orphan'
    runtime.put_blob(content)
    digest = hashlib.sha256(content).hexdigest()
    path = runtime.paths.cas_dir / digest[:2] / digest[2:]
    os.utime(path, (0, 0))
    return path


CASES = [
    'cas_maintenance', 'custom_tool', 'history_append', 'history_replace',
    'lesson', 'harvest', 'lineage_create', 'lineage_undo', 'tool_result',
    'trace_append', 'trace_import', 'observation_retention', 'presentation',
    'session_delete', 'session_purge', 'session_event', 'worktree',
]


def operation(runtime, case):
    """Return a public operation and its independently observed durable change."""
    database, before, after = 'state', 0, 1
    if case == 'cas_maintenance':
        seed_orphan(runtime)
        call, query = lambda: runtime.maintain_cas(force=True), 'SELECT COUNT(*) FROM objects'
        before, after = 1, 0
    elif case == 'custom_tool':
        source = 'def run(args): return {}\n'
        spec = {'name': 'fixture_tool', 'digest': 'a' * 64,
                'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
                'execution_mode': 'authored_pure_python'}
        call = lambda: runtime.save_custom_tool(spec, source)
        query = 'SELECT COUNT(*) FROM custom_tool_versions'
    elif case == 'history_append':
        call = lambda: runtime.append_history_record({'operation': 'fixture'}, event_id='fixture')
        query = 'SELECT COUNT(*) FROM history_events'
    elif case == 'history_replace':
        runtime.append_history_record({'operation': 'old'}, event_id='old')
        call = lambda: runtime.replace_history_records([{'operation': 'fixture'}])
        query = 'SELECT operation FROM history_events'
        before, after = 'old', 'fixture'
    elif case == 'lesson':
        call = lambda: runtime.upsert_lesson_record({'id': 'lesson', 'project_name': 'fixture'},
                                                   scope='pending', content_hash='hash')
        query = 'SELECT COUNT(*) FROM lessons'
    elif case == 'harvest':
        call = lambda: runtime.save_harvest_signal_records([{'signal_id': 'hs_fixture'}])
        query = 'SELECT COUNT(*) FROM governance_signals'
    elif case in {'lineage_create', 'lineage_undo'}:
        seed_processes(runtime)
        def create():
            return runtime.create_session_lineage_checkpoint({}, session_id='session-child',
                                                            process_id='child', turn_id='turn')
        if case == 'lineage_create':
            call, query = create, 'SELECT COUNT(*) FROM session_lineage'
        else:
            checkpoint_id = create()['checkpoint_id']
            call = lambda: runtime.commit_session_undo('session-child', checkpoint_id)
            query, before, after = 'SELECT state FROM session_lineage', 'retained', 'rewound'
    elif case == 'tool_result':
        call = lambda: runtime.put_tool_result('fixture result', process_id='process')
        query = 'SELECT COUNT(*) FROM tool_result_objects'
    elif case in {'trace_append', 'trace_import'}:
        database, query = 'observability', 'SELECT COUNT(*) FROM trace_events'
        if case == 'trace_append':
            call = lambda: runtime.append_trace_record('trace', {'seq': 1, 'event': 'fixture'})
        else:
            call = lambda: runtime.import_trace_records('trace', [({'seq': 1, 'event': 'fixture'}, None)])
    elif case == 'observation_retention':
        assert runtime.append_event(event_type='fixture', summary='fixture',
                                    occurred_at='2000-01-01T00:00:00+00:00')
        database, before, after = 'observability', 1, 0
        call, query = lambda: runtime.maintain_observability(force=True), 'SELECT COUNT(*) FROM events'
    elif case in {'presentation', 'session_delete', 'session_purge'}:
        seed_processes(runtime)
        if case == 'presentation':
            call = lambda: runtime.update_process_presentation('child', display_name='fixture')
            query = 'SELECT COUNT(*) FROM process_presentation'
        else:
            before, after = 1, 0
            query = "SELECT COUNT(*) FROM session_processes WHERE process_id='child'"
            if case == 'session_delete':
                call = lambda: runtime.delete_process_session('child')
            else:
                call = lambda: runtime.purge_b_session('session-child', 'plan')
    elif case == 'session_event':
        call = lambda: runtime.append_session_event('process', 'fixture', {'value': 1})
        query = 'SELECT COUNT(*) FROM session_events'
    elif case == 'worktree':
        call = lambda: runtime.save_worktree_state({'worktree_id': 'worktree', 'process_id': 'process'})
        query = 'SELECT COUNT(*) FROM worktrees'
    else:
        raise AssertionError(case)
    return call, database, query, before, after


@pytest.mark.parametrize('case', CASES)
@pytest.mark.parametrize('mode', ['before', 'after'])
def test_repository_commit_gap_never_reports_success(runtime, case, mode):
    call, database, query, before, after = operation(runtime, case)
    assert committed(runtime, database, query) == before
    with inject(runtime, database, mode) as wrapped:
        with pytest.raises(StorageCommitUnknown) as caught:
            call()
        assert wrapped.fired and wrapped.quarantined
        assert caught.value.stage == 'commit' and not caught.value.rollback_failed
        assert type(caught.value.__cause__) is ResponseLost
        with pytest.raises(StorageCommitUnknown):
            wrapped.execute('SELECT 1')
    assert committed(runtime, database, query) == (after if mode == 'after' else before)
    health = runtime.check_health(force_publish=True)
    assert database + '_commit_unknown' in health.reasons
    assert not any('integrity_failed' in reason for reason in health.reasons)


@pytest.mark.parametrize('database,table,call', [
    ('state', 'tool_result_objects', lambda r: r.put_tool_result('fixture')),
    ('observability', 'trace_events', lambda r: r.append_trace_record('trace', {'event': 'fixture'})),
])
@pytest.mark.parametrize('rollback_fails', [False, True])
def test_repository_body_failure_preserves_primary_error(runtime, database, table, call, rollback_fails):
    connection = getattr(runtime, '_' + database)
    connection.execute(f"CREATE TRIGGER reject_fixture BEFORE INSERT ON {table} "
                       "BEGIN SELECT RAISE(ABORT,'fixture rejected'); END")
    with inject(runtime, database, 'rollback' if rollback_fails else 'none'):
        with pytest.raises(StorageCommitUnknown if rollback_fails else sqlite3.IntegrityError) as caught:
            call(runtime)
        if rollback_fails:
            assert caught.value.stage == 'rollback' and caught.value.rollback_failed
            assert type(caught.value.__cause__) is sqlite3.IntegrityError
            assert 'fixture rejected' in str(caught.value.__cause__)
        else:
            assert 'fixture rejected' in str(caught.value)
    assert connection.quarantined == rollback_fails
    assert committed(runtime, database, f'SELECT COUNT(*) FROM {table}') == 0
    if not rollback_fails:
        connection.execute('DROP TRIGGER reject_fixture')
        call(runtime)
        assert committed(runtime, database, f'SELECT COUNT(*) FROM {table}') == 1


@pytest.mark.parametrize('database', ['state', 'observability'])
def test_repository_commit_cancellation_keeps_type_and_quarantine(runtime, database):
    call = (lambda: runtime.put_tool_result('fixture')) if database == 'state' else (
        lambda: runtime.append_trace_record('trace', {'event': 'fixture'}))
    with inject(runtime, database, 'after', error_type=KeyboardInterrupt) as wrapped:
        with pytest.raises(KeyboardInterrupt, match='fixture commit response gap'):
            call()
        assert wrapped.quarantined


@pytest.mark.parametrize('mode', ['none', 'before', 'after'])
def test_duplicate_session_return_still_requires_confirmed_commit(runtime, mode):
    assert runtime.append_session_event('process', 'fixture', {'v': 1}, sequence=1) == 1
    with inject(runtime, 'state', mode) as wrapped:
        if mode == 'none':
            assert runtime.append_session_event('process', 'fixture', {'v': 1}, sequence=1) == 1
            assert not wrapped.in_transaction
        else:
            with pytest.raises(StorageCommitUnknown):
                runtime.append_session_event('process', 'fixture', {'v': 1}, sequence=1)
            assert wrapped.fired and wrapped.quarantined
    assert committed(runtime, 'state', 'SELECT COUNT(*) FROM session_events') == 1
    assert committed(runtime, 'state', 'SELECT COUNT(*) FROM runtime_outbox') == 1


@pytest.mark.parametrize('rollback_fails', [False, True])
def test_lesson_race_deduplicates_only_after_confirmed_rollback(runtime, rollback_fails):
    runtime.upsert_lesson_record({'id': 'existing', 'project_name': 'fixture'},
                                scope='pending', content_hash='other')
    def competitor_commit(connection):
        connection.execute("UPDATE lessons SET content_hash='hash' WHERE lesson_id='existing'")
    with inject(runtime, 'state', 'rollback' if rollback_fails else 'none',
                before_begin=competitor_commit):
        call = lambda: runtime.upsert_lesson_record({'id': 'new', 'project_name': 'fixture'},
                                                    scope='pending', content_hash='hash')
        if rollback_fails:
            with pytest.raises(StorageCommitUnknown) as caught:
                call()
            assert caught.value.stage == 'rollback'
        else:
            assert call() == 'existing'
            assert not runtime._state.in_transaction and not runtime._state.quarantined
    assert committed(runtime, 'state', 'SELECT COUNT(*) FROM lessons') == 1


def test_lesson_commit_integrity_error_is_not_deduplicated(runtime):
    with inject(runtime, 'state', 'after', error_type=sqlite3.IntegrityError):
        with pytest.raises(StorageCommitUnknown) as caught:
            runtime.upsert_lesson_record({'id': 'lesson'}, scope='pending', content_hash='hash')
        assert caught.value.stage == 'commit'
        assert type(caught.value.__cause__) is sqlite3.IntegrityError
    assert committed(runtime, 'state', 'SELECT COUNT(*) FROM lessons') == 1


def test_observation_quarantine_blocks_optional_effects_but_preserves_state(runtime, monkeypatch):
    orphan = seed_orphan(runtime)
    with inject(runtime, 'observability', 'after'):
        with pytest.raises(StorageCommitUnknown):
            runtime.append_trace_record('trace', {'event': 'fixture'})
    diagnostics = runtime.diagnostics()
    assert diagnostics['health']['level'] == 'degraded'
    assert 'observability_commit_unknown' in diagnostics['health']['reasons']
    assert diagnostics['observability_pragmas'] == {} and diagnostics['state_pragmas']
    runtime.put_state_ref('fixture', 'key', 'value')
    assert runtime.get_state_ref('fixture', 'key') == 'value'
    def forbidden(*args, **kwargs):
        pytest.fail('optional write consumed capacity or created a CAS file after quarantine')
    monkeypatch.setattr(runtime, '_write_blob_file', forbidden)
    monkeypatch.setattr(runtime._global_sqlite_budget, 'reserve', forbidden)
    for call in (
        lambda: runtime.append_trace_record('new', {'event': 'fixture'}, detail={'new': 'body'}),
        lambda: runtime.import_trace_records('new', [({'seq': 1}, {'new': 'body'})]),
        lambda: runtime.append_event(event_type='fixture', summary='fixture'),
        lambda: runtime.maintain_cas(force=True),
    ):
        with pytest.raises(StorageCommitUnknown):
            call()
    assert orphan.exists()
    # Closing still retires both handles; it must not run maintenance on sealed writers.
    monkeypatch.setattr(runtime, 'record_storage_metric', forbidden)
    monkeypatch.setattr(runtime, 'maintain_observability', forbidden)
    monkeypatch.setattr(runtime, 'maintain_cas', forbidden)
    runtime.close()
    assert runtime._closed_databases == {'state', 'observability'}


def test_state_quarantine_keeps_diagnostics_available(runtime):
    with inject(runtime, 'state', 'after'):
        with pytest.raises(StorageCommitUnknown):
            runtime.put_tool_result('fixture')
    result = runtime.diagnostics()
    assert result['health']['level'] == 'blocked'
    assert result['state_pragmas'] == {} and result['observability_pragmas']
