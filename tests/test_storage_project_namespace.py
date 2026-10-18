"""Internal object-relative fixtures, not production admission qualification."""
import ctypes as C
from dataclasses import replace
from pathlib import Path,PurePosixPath
import sqlite3,uuid

import pytest
from backend.core.storage import project_schema as s
from backend.core.storage.project_namespace import ProjectNamespace
from backend.core.storage.project_records import ProjectIdentity,ProjectRecord,DIRECTORIES,FILES
from backend.core.storage.native_windows_tree import MetadataBootstrap,TreePurpose
from backend.core.storage.native_windows_sqlite import WindowsSqliteLibrary,SqliteFileLimits
from backend.core.storage.protocol import require_legacy_storage_format,StorageProtocolUnsupported
from backend.core.storage.models import StorageBlocked,StorageCommitUnknown
from tests.test_storage_runtime_control import runtime_case,reopen
from tests.test_storage_windows_tree import native

LIMITS=(SqliteFileLimits(8*1024**2,32*1024**2,8*1024**2,32768),)*2

def sqlite_library(binary):
    import _sqlite3
    kernel=C.WinDLL("kernel32",use_last_error=True)
    kernel.GetModuleHandleW.argtypes=[C.c_wchar_p];kernel.GetModuleHandleW.restype=C.c_void_p
    kernel.GetModuleFileNameW.argtypes=[C.c_void_p,C.c_wchar_p,C.c_uint32];kernel.GetModuleFileNameW.restype=C.c_uint32
    buffer=C.create_unicode_buffer(32768)
    assert 0<kernel.GetModuleFileNameW(kernel.GetModuleHandleW("sqlite3.dll"),buffer,len(buffer))<len(buffer)
    return WindowsSqliteLibrary(C.CDLL(str(binary)),
        C.CDLL(_sqlite3.__file__,winmode=0x100|0x800),C.CDLL(buffer.value,winmode=0x100|0x800))

def pristine():
    result=[]
    for role in s.ROLES:
        db=sqlite3.connect(":memory:",isolation_level=None);db.row_factory=sqlite3.Row
        try:
            s.StorageRuntime._apply_migrations(db,s.MIGRATIONS[role])
            for sql in s.GUARD:db.execute(sql)
            result.append(s.schema_digest(db))
        finally:db.close()
    return tuple(result)

def structure(owner):
    project,store=str(uuid.uuid4()),str(uuid.uuid4())
    anchor=owner.anchor
    creator=owner._new(MetadataBootstrap,owner._registry.trees,TreePurpose.BOOTSTRAP,anchor.account.sid)
    creator.begin(owner._members["candidates"],dict(anchor.members)["candidates"],project)
    root_id=creator.identity(0);indices={"":0}
    for name in DIRECTORIES+FILES:
        path=PurePosixPath(name);parent="" if str(path.parent)=="." else str(path.parent)
        index=creator.add(indices[parent],path.name,directory=name in DIRECTORIES)
        indices[name]=index
        if name in FILES:creator.write(index,b"")
    record=ProjectRecord("STRUCTURED",anchor.layout.content.storage_profile,ProjectIdentity(store,root_id,
        tuple(sorted((name,creator.identity(index)) for name,index in indices.items() if name))))
    creator.finish();assert owner._release(creator)
    return project,record

def test_actual_state_observation_schemas_hold_identity_and_reject_legacy_writers(native,tmp_path_factory):
    library=sqlite_library(native[2]/"gitgo-storage-sqlite.dll");expected=pristine()
    with runtime_case(native,tmp_path_factory) as (registry,host,account,source):
        project,record=structure(host)
        ns=ProjectNamespace(host,record,project,LIMITS).pin()
        dbs=s.ProjectDatabases(ns,library,0,expected);dbs.initialize()
        paths=[ns.database_path(role) for role in s.ROLES]
        for role,db in dbs.databases.items():
            with pytest.raises(StorageProtocolUnsupported):require_legacy_storage_format(db,s.MIGRATIONS[role])
            with pytest.raises(sqlite3.OperationalError,match="gitgo_storage_legacy_refused"):
                db.execute("SELECT version,checksum FROM schema_migrations").fetchall()
            assert tuple(db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())==(0,0,0)
            assert dbs.owners[role].extent("wal")==0
            with pytest.raises(PermissionError):ns.database_path(role).rename(ns.path/"other")
        assert host.close() and library.retained_owners==0
        observer=reopen(registry,account)
        later=ProjectNamespace(observer,replace(record,phase="MATERIALIZED"),project,LIMITS).pin()
        verified=s.ProjectDatabases(later,library,0,expected);verified.verify_initialized()
        assert observer.close() and library.retained_owners==0
        for path in paths:
            db=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)
            try:
                with pytest.raises(sqlite3.OperationalError):db.execute("SELECT * FROM schema_migrations").fetchall()
            finally:db.close()


@pytest.mark.parametrize("case",["extra","wrong-id","nonempty-cas","oversized-sidecar"])
def test_project_namespace_rejects_unqualified_members_before_sqlite(native,tmp_path_factory,case):
    with runtime_case(native,tmp_path_factory) as (_,host,_,_):
        project,record=structure(host);path=Path(record.root_path(host.anchor,project))
        if case=="extra":(path/"data/unowned").write_bytes(b"x")
        if case=="wrong-id":
            members=tuple((name,replace(value,file_id=value.file_id^1) if name=="data/state.sqlite3" else value)
                          for name,value in record.identity.members)
            record=replace(record,identity=replace(record.identity,members=members))
        if case=="nonempty-cas":(path/"data/cas/unowned").write_bytes(b"x")
        if case=="oversized-sidecar":(path/"data/state.sqlite3-shm").write_bytes(bytes(32769))
        ns=ProjectNamespace(host,record,project,LIMITS)
        with pytest.raises(StorageBlocked):ns.pin().require_empty_cas()
        assert (path/"data/state.sqlite3").stat().st_size==0


def test_lost_first_database_binding_commit_blocks_second_schema_and_never_replays(native,tmp_path_factory,monkeypatch):
    library=sqlite_library(native[2]/"gitgo-storage-sqlite.dll");expected=pristine()
    from backend.core.storage.native_windows_sqlite import _OwnedStorageConnection
    original=_OwnedStorageConnection.execute
    armed=[True]
    def lost(self,sql,*args,**kwargs):
        result=original(self,sql,*args,**kwargs)
        if sql=="COMMIT" and armed[0]:
            armed[0]=False;result.close();raise sqlite3.OperationalError("lost binding COMMIT")
        return result
    with runtime_case(native,tmp_path_factory) as (registry,host,account,_):
        project,record=structure(host);ns=ProjectNamespace(host,record,project,LIMITS).pin()
        dbs=s.ProjectDatabases(ns,library,0,expected)
        with monkeypatch.context() as patch:
            patch.setattr(_OwnedStorageConnection,"execute",lost)
            with pytest.raises(StorageCommitUnknown):dbs.initialize()
        assert set(dbs.owners)=={"state"} and dbs.owners["state"]._sealed
        assert host.close()
        observer=reopen(registry,account)
        later=ProjectNamespace(observer,record,project,LIMITS).pin()
        assert (later.path/"data/observability.sqlite3").stat().st_size==0
        with pytest.raises(StorageBlocked):s.ProjectDatabases(later,library,0,expected).verify_initialized()
        assert observer.close()
        assert (later.path/"data/observability.sqlite3").stat().st_size==0
