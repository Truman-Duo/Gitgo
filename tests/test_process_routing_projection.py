"""Project counts use one execution population before and after disconnect."""
import io
import sqlite3
from types import SimpleNamespace

from backend.core.native_host import NativeHost
from backend.core.storage.runtime import read_project_list_status


def test_cold_projection_counts_every_execution_without_a_writer(tmp_path_factory, monkeypatch):
    db = tmp_path_factory / 'state.sqlite3'
    with sqlite3.connect(db) as conn:
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
