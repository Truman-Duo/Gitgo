"""Isolated actual SQLite VFS acceptance, one finite native pool per process."""
import ctypes
import _sqlite3
from pathlib import Path
import shutil
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.storage.native_windows_sqlite import (
    SqliteFileLimits, StorageSqliteError, WindowsSqliteLibrary,
)


def repository_case(library, path, case):
    from backend.core.storage.migrations import STATE_MIGRATIONS, OBSERVABILITY_MIGRATIONS
    from backend.core.storage.models import StorageCommitUnknown
    from backend.core.storage.runtime import StorageRuntime
    from backend.core.storage.transactions import authoritative_transaction
    from backend.core.storage.support import _StorageConnection
    limits = SqliteFileLimits(8*1024*1024, 32*1024*1024, 8*1024*1024, 32768)
    path.write_bytes(b"")
    owner = library.open(path, limits)
    slot = owner.connect(); db = slot.database; db.row_factory = sqlite3.Row
    assert isinstance(db, _StorageConnection)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    StorageRuntime._apply_migrations(db, STATE_MIGRATIONS)
    if case == "repository_schemas":
        assert db.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        owner.close()
        obs_path = path.with_name("obs.sqlite3"); obs_path.write_bytes(b"")
        obs = library.open(obs_path,limits)
        connection = obs.connect().database
        connection.execute("PRAGMA journal_mode=WAL")
        StorageRuntime._apply_migrations(connection,OBSERVABILITY_MIGRATIONS)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        obs.close()
    else:
        execute = db.execute
        deferred = db.execute("SELECT 1")
        def lost_commit(sql,*args,**kwargs):
            result = execute(sql,*args,**kwargs)
            if sql == "COMMIT":
                result.close()
                raise sqlite3.OperationalError("injected after actual COMMIT")
            return result
        db.execute = lost_commit
        try:
            with authoritative_transaction(db):
                db.execute("INSERT INTO projects VALUES(?,?,?,?)",("one","fixture","now","now"))
        except StorageCommitUnknown: pass
        else: raise AssertionError("Unknown commit was acknowledged")
        assert db.quarantined and owner._sealed
        for action in (owner.connect, lambda: slot.database, lambda: db.execute("SELECT 1"), deferred.fetchone):
            try: action()
            except (StorageCommitUnknown,StorageSqliteError): pass
            else: raise AssertionError("Unknown commit permitted further work")
        deferred.close(); owner.close()
        # Independent stored-fact assertion, not production recovery authority:
        # the fixture has not established an enclosing native worker retirement.
        observer = library.open(path,limits)
        assert observer.connect().database.execute("SELECT project_id FROM projects").fetchall() == [("one",)]
        observer.close()
    assert library.retained_owners == 0
    print(case,"passed")


def main():
    binary, root, case = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    # Test-only discovery; the binding then proves every actual CPython import.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes, kernel.GetModuleHandleW.restype = [ctypes.c_wchar_p], ctypes.c_void_p
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
    kernel.GetModuleFileNameW.restype = ctypes.c_uint32
    handle = kernel.GetModuleHandleW("sqlite3.dll")
    path = ctypes.create_unicode_buffer(32768)
    assert handle and 0 < kernel.GetModuleFileNameW(handle,path,len(path)) < len(path)
    selected = ctypes.CDLL(path.value, winmode=0x100|0x800)
    extension = ctypes.CDLL(_sqlite3.__file__, winmode=0x100|0x800)
    native = ctypes.CDLL(str(binary), winmode=0x100|0x800)
    if case == "same_bytes_foreign_module":
        duplicate = root/"duplicate-sqlite.dll"
        shutil.copyfile(path.value, duplicate)
        foreign = ctypes.CDLL(str(duplicate), winmode=0x100|0x800)
        assert foreign._handle != selected._handle
        try: WindowsSqliteLibrary(native, extension, foreign)
        except StorageSqliteError as error: assert error.operation == "actual_sqlite_imports"
        else: raise AssertionError("Identical bytes do not prove the actual imported module")
        print(case,"passed");return
    library = WindowsSqliteLibrary(native, extension, selected)
    path = root/"state.sqlite3"
    if case.startswith("repository_"):
        repository_case(library,path,case);return
    path.write_bytes(b"")
    limits = SqliteFileLimits(1024*1024, 262144, 1024*1024, 32768)
    owner = library.open(path,limits)
    if case == "oversized_sidecar":
        path.with_name(path.name+"-wal").write_bytes(b"x"*(limits.wal+1))
        try: owner.connect()
        except sqlite3.DatabaseError: pass
        else: raise AssertionError("Oversized sidecar admitted")
        assert owner.snapshot().lifetime_file_opens == 0
        for _ in range(2):
            try: owner.close()
            except StorageSqliteError: pass
            else: raise AssertionError("Ambiguous connection was forgotten")
        assert library.retained_owners == 1
        print(case,"passed");return
    slot = owner.connect()
    db = slot.database
    assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    db.execute("CREATE TABLE data(id INTEGER PRIMARY KEY,body BLOB)")
    db.execute("INSERT INTO data VALUES(1,?)",(b"old",))
    if case == "roundtrip":
        assert db.execute("SELECT * FROM data").fetchall() == [(1,b"old")]
        assert owner.extent("wal") > 0 and owner.extent("journal") is None
    elif case == "wal_ceiling":
        assert owner.extent("wal") < 32768
        owner.limit("wal",32768)
        try: db.execute("INSERT INTO data VALUES(2,?)",(b"x"*131072,))
        except sqlite3.DatabaseError as error: assert error.sqlite_errorcode == sqlite3.SQLITE_FULL
        else: raise AssertionError("WAL ceiling bypassed")
        assert db.execute("SELECT * FROM data").fetchall() == [(1,b"old")]
        assert owner.snapshot().wal_highwater <= 32768
    elif case == "pinned_reader":
        reader = owner.connect()
        reader.database.execute("BEGIN")
        assert reader.database.execute("SELECT count(*) FROM data").fetchone()[0] == 1
        db.execute("INSERT INTO data VALUES(2,?)",(b"new",))
        assert reader.database.execute("SELECT count(*) FROM data").fetchone()[0] == 1
        assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0
        reader.database.rollback();reader.close()
        assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0,0,0)
    elif case == "outside_attach":
        foreign = root/"outside.sqlite3"
        try: db.execute("ATTACH DATABASE ? AS other",(str(foreign),))
        except sqlite3.DatabaseError: pass
        else: raise AssertionError("Foreign path admitted")
        assert not foreign.exists()
    elif case == "thread":
        def other():
            for action in (owner.connect,owner.snapshot,owner.close,slot.close):
                try: action()
                except StorageSqliteError as error: assert error.operation == "foreign_context"
                else: raise AssertionError("Foreign thread acquired ownership")
        with ThreadPoolExecutor(max_workers=1) as pool: pool.submit(other).result()
        assert db.execute("SELECT count(*) FROM data").fetchone()[0] == 1
    elif case == "mmap_disabled":
        db.execute("PRAGMA mmap_size=1048576")
        assert db.execute("SELECT body FROM data").fetchone()[0] == b"old"
        assert owner.snapshot().shm_highwater <= limits.shm
    elif case == "registration_lifetime":
        pass
    else: raise AssertionError(case)
    owner.close();owner.close()
    state = owner.snapshot()
    assert state.open_files == state.unknown_files == state.leases == library.retained_owners == 0
    if case == "registration_lifetime":
        for _ in range(7): library.open(path,limits).close()
        try: library.open(path,limits)
        except StorageSqliteError as error: assert error.native_error == sqlite3.SQLITE_FULL
        else: raise AssertionError("Nominal close refunded lifetime registration quota")
        assert library.retained_owners == 0
    # The private VFS never replaces SQLite's default registration.
    with sqlite3.connect(":memory:") as ordinary:
        assert ordinary.execute("SELECT 41+1").fetchone()[0] == 42
    print(case,"passed")


if __name__ == "__main__": main()
