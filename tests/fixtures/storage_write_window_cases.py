"""Actual native limit and COMMIT faults; Host journal admission is not proved."""
import pytest
from backend.core.storage import project_schema as schemas,StorageBlocked
from backend.core.storage.native_windows_sqlite import SqliteFileLimits
from backend.core.storage.project_namespace import ProjectNamespace
from backend.core.storage.wal_capacity import WalFrameBound
from tests.test_storage_project_namespace import structure,pristine,sqlite_library
from tests.test_storage_runtime_control import runtime_case,reopen
from tests.test_storage_windows_tree import native
from backend.core.storage.write_window import WriteRange,WriteWindow


@pytest.mark.parametrize('fault',['none','limit','before','after','extent'])
def test_native_write_range_is_consumed_before_effects_and_never_refunded(native,tmp_path_factory,monkeypatch,fault):
    library=sqlite_library(native[2]/'gitgo-storage-sqlite.dll')
    limits=(SqliteFileLimits(1024**2,32*1024**2,1024**2,32768),)*2
    with runtime_case(native,tmp_path_factory) as (registry,host,account,_):
        project,record=structure(host)
        ns=ProjectNamespace(host,record,project,limits).pin()
        dbs=schemas.ProjectDatabases(ns,library,0,pristine());dbs.initialize()
        db=dbs.databases['state'];owner=dbs.owners['state']
        assert tuple(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())==(0,0,0)
        assert owner.extent('wal')==0
        window=WriteWindow(db,owner,WriteRange(WalFrameBound(4096,256,65536),0,2))
        original=db.execute;native_limit=owner.limit
        calls=[]
        def execute(sql,*args,**kwargs):
            calls.append(sql)
            if sql=='COMMIT' and fault=='before':raise OSError('lost COMMIT before')
            result=original(sql,*args,**kwargs)
            if sql=='COMMIT' and fault=='after':
                result.close();raise OSError('lost COMMIT after')
            return result
        def limit(role,ceiling):
            native_limit(role,ceiling)
            raise OSError('lost native write limit')
        with monkeypatch.context() as patch:
            patch.setattr(db,'execute',execute)
            if fault=='limit':patch.setattr(owner,'limit',limit)
            if fault=='extent':patch.setattr(owner,'extent',lambda role:None)
            if fault=='none':
                with window.transaction():db.execute('INSERT INTO projects VALUES(?,?,?,?)',(project,'hint','time','time')).close()
                with window.transaction():db.execute("UPDATE projects SET updated_at='next'").close()
                with pytest.raises(StorageBlocked,match='exhausted'):
                    with window.transaction():pytest.fail('exhausted range entered SQL')
                assert window.spent==2 and calls.count('BEGIN IMMEDIATE')==2
                with pytest.raises(StorageBlocked,match='already consumed'):
                    WriteWindow(db,owner,WriteRange(WalFrameBound(4096,256,65536),2,1))
            else:
                with pytest.raises((StorageBlocked,OSError)):
                    with window.transaction():db.execute('INSERT INTO projects VALUES(?,?,?,?)',(project,'hint','time','time')).close()
                assert window.spent==1 and db.quarantined and owner._sealed
                if fault=='limit':assert 'BEGIN IMMEDIATE' not in calls
                with pytest.raises(StorageBlocked):
                    with window.transaction():pytest.fail('unknown owner entered SQL')
                assert window.spent==1
        assert host.close()
        observer=reopen(registry,account)
        fresh_ns=ProjectNamespace(observer,record,project,limits).pin()
        fresh_dbs=schemas.ProjectDatabases(fresh_ns,library,0,pristine())
        fresh=fresh_dbs._open('state');fresh.execute('PRAGMA query_only=ON')
        assert fresh.execute('SELECT COUNT(*) FROM projects').fetchone()[0]==int(fault in {'none','after','extent'})
        assert fresh.total_changes==0
        assert observer.close() and library.retained_owners==0
