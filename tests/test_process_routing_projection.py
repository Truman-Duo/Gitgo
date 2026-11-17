"""Project counts use one execution population before and after disconnect."""
from contextlib import closing
import io
import sqlite3
from types import SimpleNamespace

from backend.core.native_host import NativeHost
from backend.core.storage.runtime import read_project_list_status


def test_cold_projection_counts_every_execution_without_a_writer(tmp_path_factory, monkeypatch):
    db = tmp_path_factory / 'state.sqlite3'
    # The sqlite context commits transactions; closing also releases the OS handle.
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute('CREATE TABLE session_processes (process_id TEXT, status TEXT, updated_at TEXT, created_at TEXT, actor_kind TEXT, parent_process_id TEXT)')
        conn.executemany('INSERT INTO session_processes VALUES (?,?,?,?,?,?)', [
            ('a0','completed','1','1','supervisor',None),
            ('a1','completed','4','3','supervisor',None),
            ('b1','completed','2','2','worker','a0'),
            ('b2','waiting','5','4','worker','a1'),
        ])
    monkeypatch.setattr('backend.core.storage.runtime.resolve_existing_storage_paths', lambda _: SimpleNamespace(state_db=db))
    before = db.read_bytes()
    result = read_project_list_status(tmp_path_factory)
    assert result['process_id'] == 'a1'
    assert result['count_scope'] == 'all_executions'
    assert result['process_statuses'] == {'a0':'completed','a1':'completed','b1':'completed','b2':'waiting'}
    assert db.read_bytes() == before


def test_live_and_disconnected_summary_keep_history_and_overlay_live(monkeypatch):
    host = NativeHost(stdout=io.StringIO())
    item = {'name':'demo', 'workspace':'/unused/projection-fixture'}
    durable = {'process_id':'a1','status':'completed','updated_at':'1',
               'process_statuses':{'a0':'completed','a1':'completed','b1':'completed','b2':'waiting'}}
    monkeypatch.setattr('backend.core.storage.read_project_list_status', lambda _: durable)
    daemon = SimpleNamespace(send_command=lambda *a, **k: {'processes':{
        'a1':{'status':'completed'},'b1':{'status':'completed'},'b2':{'status':'waiting'}}})
    monkeypatch.setattr(host, '_get_daemon', lambda *a, **k: daemon)
    try:
        live = host._read_project_summary(item)[1]
        monkeypatch.setattr(host, '_get_daemon', lambda *a, **k: None)
        cold = host._read_project_summary(item)[1]
        for row in (live,cold):
            assert row['finished_process_count'] == 3
            assert row['waiting_process_count'] == 1
            assert row['count_scope'] == 'all_executions'
    finally:
        host.close()


def test_live_history_failure_is_explicitly_partial_not_offline(monkeypatch):
    host = NativeHost(stdout=io.StringIO())
    monkeypatch.setattr(host, '_get_daemon', lambda *a, **k: SimpleNamespace(
        send_command=lambda *a, **k: {'processes':{'b':{'status':'running'}}}))
    def unavailable(_):
        raise OSError('history unreadable')
    monkeypatch.setattr('backend.core.storage.read_project_list_status', unavailable)
    try:
        row = host._read_project_summary({'name':'demo','workspace':'/unused/fixture'})[1]
        assert row['daemon_online'] and row['state_available']
        assert row['active_process_count'] == 1
        assert row['count_scope'] == 'live_executions'
        assert row['stale'] and row['summary_error'] == 'history unreadable'
    finally:
        host.close()


def test_pending_closeout_is_counted_waiting_not_finished(monkeypatch):
    host = NativeHost(stdout=io.StringIO())
    monkeypatch.setattr(host, '_get_daemon', lambda *a, **kw: SimpleNamespace(send_command=lambda *a, **kw: {
        'processes': {'b1': {'status': 'completed', 'lifecycle_finalized': False, 'closeout_state': 'pending'}}}))
    monkeypatch.setattr('backend.core.storage.read_project_list_status', lambda *a, **kw: None)
    try:
        summary = host._read_project_summary({'name': 'demo', 'workspace': '/unused/fixture'})[1]
        assert summary['finished_process_count'] == 0
        assert summary['waiting_process_count'] == 1
    finally:
        host.close()


def test_recovery_and_stopped_executions_are_not_missing_from_counts():
    counts = NativeHost._counts_from_statuses([
        'recovery_review_required', 'orphaned', 'killed',
        {'status': 'failed', 'closeout_state': 'failed'},
    ])
    assert counts == {'active_process_count': 0, 'waiting_process_count': 2, 'finished_process_count': 2}


def test_live_cold_and_project_summary_share_failed_barrier_facts(tmp_path_factory, monkeypatch):
    from backend.core.loop.lifecycle import execution_closeout
    from backend.core.loop.manager import SessionStore
    from backend.core.loop.models import ProcessStatus, RingLevel
    from backend.core.loop.tools import ToolRegistry
    from tests.test_task_contract_recovery import _supervisor
    root, _ = _supervisor(tmp_path_factory)
    child = root._manager.fork(root.process_id, 'executor', ToolRegistry([]), 4, RingLevel.RING_3,
                              workspace_path=str(tmp_path_factory), task_id='child')
    store = SessionStore(str(tmp_path_factory), state_home=tmp_path_factory / 'state')
    try:
        for process in (root, child):
            process.status = ProcessStatus.FAILED
            process.result = {'status': 'failed', 'code': 'SESSION_CHECKPOINT_FAILED'}
            process.lifecycle_finalized = False
            store.save_process_checkpoint(process)
        monkeypatch.setattr('backend.core.storage.runtime.resolve_existing_storage_paths', lambda _: store._storage.paths)
        monkeypatch.setattr('backend.core.storage.runtime.StorageRuntime.__init__',
                            lambda *a, **kw: (_ for _ in ()).throw(AssertionError('read created writer')))
        project = read_project_list_status(tmp_path_factory)
        assert execution_closeout(child)['closeout_state'] == 'failed'
        assert store._storage.read_project_b_processes()[child.process_id]['closeout_state'] == 'failed'
        assert store._storage.read_latest_root_process()['closeout_state'] == 'failed'
        assert all(state['closeout_state'] == 'failed' for state in project['process_states'].values())
        assert NativeHost._counts_from_statuses(project['process_states'].values())['finished_process_count'] == 2
    finally:
        store.close()


def test_cold_summary_unfinalized_success_is_waiting_and_does_not_mutate_storage(tmp_path_factory, monkeypatch):
    from backend.core.loop.manager import SessionStore
    from backend.core.loop.models import ProcessStatus
    from tests.test_task_contract_recovery import _supervisor
    root, _ = _supervisor(tmp_path_factory)
    root.status = ProcessStatus.COMPLETED
    root.result = {'status': 'completed', 'response': 'provisional'}
    store = SessionStore(str(tmp_path_factory), state_home=tmp_path_factory / 'state')
    try:
        store.save_process_checkpoint(root)
        paths = store._storage.paths
        monkeypatch.setattr('backend.core.storage.runtime.resolve_existing_storage_paths', lambda _: paths)
        revision = store.process_revision(root.process_id)
        before = {str(path): path.read_bytes() for path in paths.cas_dir.rglob('*') if path.is_file()}
        result = read_project_list_status(tmp_path_factory)
        assert result['process_states'][root.process_id]['closeout_state'] == 'pending'
        assert NativeHost._counts_from_statuses(result['process_states'].values())['waiting_process_count'] == 1
        assert store.process_revision(root.process_id) == revision
        assert before == {str(path): path.read_bytes() for path in paths.cas_dir.rglob('*') if path.is_file()}
    finally:
        store.close()


def test_project_list_rejects_invalid_cas_reference_without_reading_external_path(tmp_path_factory, monkeypatch):
    import pytest
    from backend.core.loop.manager import SessionStore
    from backend.core.loop.models import ProcessStatus
    from backend.core.storage.models import StorageBlocked
    from tests.test_task_contract_recovery import _supervisor
    root, _ = _supervisor(tmp_path_factory)
    root.status = ProcessStatus.COMPLETED
    store = SessionStore(str(tmp_path_factory), state_home=tmp_path_factory / 'state')
    try:
        store.save_process_checkpoint(root)
        paths = store._storage.paths
        monkeypatch.setattr('backend.core.storage.runtime.resolve_existing_storage_paths', lambda _: paths)
        with closing(sqlite3.connect(paths.state_db)) as connection, connection:
            connection.execute('UPDATE session_processes SET runtime_state_ref=? WHERE process_id=?',
                               ('sha256:../../outside.json', root.process_id))
        with pytest.raises(StorageBlocked, match='STATE_REFERENCE_INVALID'):
            read_project_list_status(tmp_path_factory)
    finally:
        store.close()


def test_repeated_finalized_refs_are_read_once_and_outcomes_are_not_loaded(tmp_path_factory, monkeypatch):
    import backend.core.storage.runtime as runtime
    from backend.core.loop.manager import SessionStore
    from backend.core.loop.models import ProcessStatus, RingLevel
    from backend.core.loop.tools import ToolRegistry
    from tests.test_task_contract_recovery import _supervisor
    root, _ = _supervisor(tmp_path_factory)
    store = SessionStore(str(tmp_path_factory), state_home=tmp_path_factory / 'state')
    try:
        children = [root._manager.fork(root.process_id, 'executor', ToolRegistry([]), 4, RingLevel.RING_3,
                                      workspace_path=str(tmp_path_factory), task_id='child-' + str(i)) for i in range(3)]
        for process in [root, *children]:
            process.status = ProcessStatus.COMPLETED
            process.lifecycle_finalized = True
            process.result = {'status': 'completed', 'response': 'Final result'}
            store.save_process_checkpoint(process)
        paths = store._storage.paths
        with closing(sqlite3.connect(paths.state_db)) as connection, connection:
            reference = connection.execute('SELECT runtime_state_ref FROM session_processes WHERE process_id=?',
                                           (root.process_id,)).fetchone()[0]
            connection.execute('UPDATE session_processes SET runtime_state_ref=?', (reference,))
        reads = []
        original = runtime._read_owned_state_object
        def read(*args, **kwargs):
            reads.append(args[2])
            return original(*args, **kwargs)
        monkeypatch.setattr(runtime, '_read_owned_state_object', read)
        monkeypatch.setattr(runtime, 'resolve_existing_storage_paths', lambda _: paths)
        result = read_project_list_status(tmp_path_factory)
        assert len(result['process_states']) == 4
        assert reads == [reference]
        assert NativeHost._counts_from_statuses(result['process_states'].values())['finished_process_count'] == 4
    finally:
        store.close()


def test_closeout_read_limit_is_explicitly_partial_or_keeps_stale_counts(tmp_path_factory, monkeypatch):
    import backend.core.storage.runtime as runtime
    from backend.core.loop.manager import SessionStore
    from backend.core.loop.models import ProcessStatus
    from tests.test_task_contract_recovery import _supervisor
    root, _ = _supervisor(tmp_path_factory)
    root.status = ProcessStatus.COMPLETED
    root.lifecycle_finalized = True
    store = SessionStore(str(tmp_path_factory), state_home=tmp_path_factory / 'state')
    host = NativeHost(stdout=io.StringIO())
    try:
        store.save_process_checkpoint(root)
        monkeypatch.setattr(runtime, 'resolve_existing_storage_paths', lambda _: store._storage.paths)
        monkeypatch.setattr(runtime, 'PROJECT_LIST_CLOSEOUT_READ_BYTES', 8)
        monkeypatch.setattr(host, '_get_daemon', lambda *a, **kw: SimpleNamespace(
            send_command=lambda *a, **kw: {'processes': {'live': {'status': 'running', 'closeout_state': 'active'}}}))
        item = {'name': 'demo', 'workspace': str(tmp_path_factory)}
        live = host._read_project_summary(item)[1]
        assert live['active_process_count'] == 1
        assert live['count_scope'] == 'live_executions'
        assert live['stale'] and 'PROJECT_LIST_CLOSEOUT_LIMIT' in live['summary_error']
        host._summary_cache['demo'] = {'finished_process_count': 7, 'observed_at': 'earlier', 'count_scope': 'all_executions'}
        monkeypatch.setattr(host, '_get_daemon', lambda *a, **kw: None)
        cold = host._read_project_summary(item)[1]
        assert cold['finished_process_count'] == 7
        assert cold['stale'] and not cold['state_available']
        assert 'PROJECT_LIST_CLOSEOUT_LIMIT' in cold['summary_error']
    finally:
        host.close()
        store.close()


def test_existing_state_reader_reuses_verified_owned_object_path(tmp_path_factory, monkeypatch):
    import backend.core.storage.runtime as runtime
    from backend.core.loop.manager import SessionStore
    store = SessionStore(str(tmp_path_factory), state_home=tmp_path_factory / 'state')
    try:
        reference = store._storage.put_blob(b'{"kept":true}')
        store._storage.put_state_ref('fixture', 'head', reference)
        monkeypatch.setattr(runtime, 'resolve_existing_storage_paths', lambda _: store._storage.paths)
        read = runtime.read_existing_state_blob(tmp_path_factory, 'fixture', 'head')
        assert read['reference'] == reference and read['payload'] == b'{"kept":true}'
    finally:
        store.close()



def test_project_projection_closes_read_connection_on_success_empty_and_error(tmp_path_factory, monkeypatch):
    """Real close is deterministic on every OS, independent of GC or unlink semantics."""
    import pytest
    import backend.core.storage.runtime as runtime
    from backend.core.storage.models import StorageBlocked
    db = tmp_path_factory / 'connection-lifetime.sqlite3'
    with closing(sqlite3.connect(db)) as setup, setup:
        setup.execute('CREATE TABLE session_processes (process_id TEXT, status TEXT, updated_at TEXT, created_at TEXT, actor_kind TEXT, parent_process_id TEXT, runtime_state_ref TEXT)')
    monkeypatch.setattr(runtime, 'resolve_existing_storage_paths', lambda _: SimpleNamespace(state_db=db))
    original_connect = sqlite3.connect
    observed = []
    def connect(database, *args, **kwargs):
        connection = original_connect(database, *args, **kwargs)
        if str(database) == db.resolve().as_uri() + '?mode=ro':
            observed.append(connection)  # retain handles: GC cannot hide an unclosed reader
        return connection
    monkeypatch.setattr(sqlite3, 'connect', connect)
    assert read_project_list_status(tmp_path_factory) is None
    with closing(original_connect(db)) as setup, setup:
        setup.execute('INSERT INTO session_processes VALUES (?,?,?,?,?,?,?)',
                      ('root', 'completed', '1', '1', 'supervisor', None, None))
    assert read_project_list_status(tmp_path_factory)['process_id'] == 'root'
    with closing(original_connect(db)) as setup, setup:
        setup.execute('UPDATE session_processes SET runtime_state_ref=?', ('sha256:../../outside.json',))
    with pytest.raises(StorageBlocked, match='STATE_REFERENCE_INVALID'):
        read_project_list_status(tmp_path_factory)
    assert len(observed) == 3
    for connection in observed:
        with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
            connection.execute('SELECT 1')
