"""Owned, bounded SQLite VFS construction inside a qualified native worker.

The caller supplies pinned loaded modules, an exclusive protected namespace,
the exact database path and prepaid limits. This component grants no project
authority, makes no commit claim, and never refunds physical worker capacity.
All file callbacks stay in C. Uncertain connection/close outcomes stay owned
until the containing native process has been conclusively retired.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import sys
import threading
import uuid

from .models import StorageBlocked
from .support import _StorageConnection

_U64 = ctypes.c_uint64
_I64 = ctypes.c_int64
_LIVE: dict[int, WindowsSqliteVfs] = {}
_ROLES = ("main", "wal", "journal")


class StorageSqliteError(StorageBlocked):
    code = "STORAGE_NATIVE_SQLITE"

    def __init__(self, operation: str, native_error: int):
        self.operation, self.native_error = operation, native_error
        super().__init__(f"{self.code}: {operation} ({native_error})")


def _check(operation: str, result: int) -> None:
    if result:
        raise StorageSqliteError(operation, result)


@dataclass(frozen=True)
class SqliteFileLimits:
    main: int
    wal: int
    journal: int
    shm: int

    def __post_init__(self):
        if any(type(n) is not int or not 0 < n <= 1 << 40
               for n in (self.main, self.wal, self.journal, self.shm)):
            raise ValueError("Explicit positive native byte limits are required")
        if self.shm < 32768 or self.shm % 32768 or self.main < 4096:
            raise ValueError("Qualified SQLite file granularity is required")


@dataclass(frozen=True)
class SqliteNativeSnapshot:
    sealed: int
    open_files: int
    unknown_files: int
    leases: int
    main_highwater: int
    wal_highwater: int
    journal_highwater: int
    shm_highwater: int
    refusals: int
    close_attempts: int
    lifetime_file_opens: int
    owner_pool_bytes: int


class WindowsSqliteLibrary:
    """No discovery: the enclosing pinned runtime owns all three modules."""

    def __init__(self, loaded_module, python_extension, selected_sqlite):
        if sys.platform != "win32":
            raise StorageSqliteError("unsupported_platform", 50)
        self._module, self._extension, self._sqlite = loaded_module, python_extension, selected_sqlite
        self._owners: dict[int, WindowsSqliteVfs] = {}
        signatures = {
            "abi": [], "header_version": [],
            "create": [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                       _I64, _I64, _I64, _I64, ctypes.POINTER(_U64)],
            "lease": [_U64, ctypes.c_int], "limit": [_U64, ctypes.c_int, _I64],
            "seal": [_U64], "stats": [_U64, ctypes.POINTER(_U64), ctypes.c_int],
            "extent": [_U64, ctypes.c_int, ctypes.POINTER(_I64)], "destroy": [_U64],
        }
        self._calls = {}
        for name, arguments in signatures.items():
            call = getattr(loaded_module, "gitgo_vfs_" + name)
            call.argtypes, call.restype = arguments, ctypes.c_int
            self._calls[name] = call
        if self._calls["abi"]() != 1:
            raise StorageSqliteError("unsupported_abi", 50)
        bind = loaded_module.gitgo_sqlite_bind
        bind.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_U64), ctypes.c_uint32]
        bind.restype = ctypes.c_uint32
        proof = (_U64 * 5)()
        _check("actual_sqlite_imports", bind(python_extension._handle, selected_sqlite._handle, proof, 5))
        if proof[3] != self._calls["header_version"]():
            raise StorageSqliteError("unqualified_header", sqlite3.SQLITE_MISUSE)

    @property
    def retained_owners(self) -> int:
        return len(self._owners)

    def open(self, path: Path, limits: SqliteFileLimits) -> WindowsSqliteVfs:
        return WindowsSqliteVfs(self, path, limits)


class WindowsSqliteVfs:
    def __init__(self, library: WindowsSqliteLibrary, path: Path, limits: SqliteFileLimits):
        if type(library) is not WindowsSqliteLibrary or type(limits) is not SqliteFileLimits:
            raise ValueError("An explicit native binding and immutable limits are required")
        if len(_LIVE) >= 8 or library.retained_owners >= 8:
            raise StorageSqliteError("owner_capacity", sqlite3.SQLITE_FULL)
        path = Path(path)
        if not path.is_absolute() or "\x00" in str(path) or len(str(path).encode("utf8")) >= 4087:
            raise ValueError("A bounded absolute path from the namespace owner is required")
        self._library, self.path, self.limits = library, path, limits
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._cookie = _U64()
        self._name = ("gitgo-storage-" + uuid.uuid4().hex).encode("ascii")
        self._slots: list[WindowsSqliteConnection] = []
        self._sealed = self._closed = False
        self._final: SqliteNativeSnapshot | None = None
        self._destroy_entered = False
        library._owners[id(self)] = self
        _LIVE[id(self)] = self
        try:
            _check("register", self._call("create", library._extension._handle, library._sqlite._handle,
                   self._name, str(path).encode("utf8"), limits.main, limits.wal, limits.journal,
                   limits.shm, ctypes.byref(self._cookie)))
        except BaseException:
            self._sealed = True
            if not self._cookie.value:
                library._owners.pop(id(self), None)
                _LIVE.pop(id(self), None)
            raise

    def _call(self, name, *args):
        return self._library._calls[name](*args)

    def _context(self):
        if (os.getpid(), threading.get_ident()) != (self._pid, self._thread):
            raise StorageSqliteError("foreign_context", sqlite3.SQLITE_MISUSE)

    def _require(self):
        self._context()
        if self._sealed or self._closed:
            raise StorageSqliteError("owner_sealed", sqlite3.SQLITE_MISUSE)

    def snapshot(self) -> SqliteNativeSnapshot:
        self._context()
        if self._closed:
            return self._final
        output = (_U64 * 12)()
        _check("snapshot", self._call("stats", self._cookie, output, 12))
        return SqliteNativeSnapshot(*output)

    def limit(self, role: str, capacity: int) -> None:
        self._require()
        if role not in _ROLES or type(capacity) is not int or not 0 < capacity <= getattr(self.limits, role):
            raise ValueError("A byte ceiling within the prepaid family is required")
        self._sealed = True
        _check("write_limit", self._call("limit", self._cookie, _ROLES.index(role), capacity))
        self._sealed = False

    def extent(self, role: str) -> int | None:
        self._require()
        if role not in _ROLES:
            raise ValueError("Unknown SQLite file role")
        output = _I64(-1)
        result = self._call("extent", self._cookie, _ROLES.index(role), ctypes.byref(output))
        if result == sqlite3.SQLITE_NOTFOUND:
            return None  # Absence is not a measured zero-byte file.
        _check("owned_extent", result)
        return output.value

    def connect(self) -> WindowsSqliteConnection:
        self._require()
        if len(self._slots) >= 16:
            raise StorageSqliteError("connection_lifetime_capacity", sqlite3.SQLITE_FULL)
        slot = WindowsSqliteConnection(self)
        self._slots.append(slot)  # Own intent before either lease or SQLite opens.
        try:
            slot._start()
            return slot
        except BaseException:
            self._sealed = True
            raise

    def close(self) -> None:
        self._context()
        if self._closed:
            return
        self._sealed = True
        failed = False
        # Attempt every still-known child once; an unknown child never retries.
        for slot in self._slots:
            try:
                slot.close()
            except BaseException:
                failed = True
        state = self.snapshot()
        if failed or state.open_files or state.unknown_files or state.leases:
            _check("seal", self._call("seal", self._cookie))
            raise StorageSqliteError("retained_connection_debt", sqlite3.SQLITE_BUSY)
        if self._destroy_entered:
            raise StorageSqliteError("unregistration_unknown", sqlite3.SQLITE_MISUSE)
        # Native retirement is idempotent, but a failed acknowledgement is not
        # silently retried by this owner. Worker retirement settles its debt.
        self._destroy_entered = True
        _check("unregister", self._call("destroy", self._cookie))
        self._final, self._closed = state, True
        self._library._owners.pop(id(self), None)
        _LIVE.pop(id(self), None)


class _OwnedStorageConnection(_StorageConnection):
    """The existing repository quarantine also seals its enclosing VFS owner."""
    def require_available(self):
        super().require_available()
        self._native_owner._require()

    def quarantine(self):
        super().quarantine()
        self._native_owner._sealed = True


class WindowsSqliteConnection:
    """Repository borrows the DB-API connection; this owner alone closes it."""

    def __init__(self, owner: WindowsSqliteVfs):
        self._owner = owner
        self._database = None
        self._started = self._initialized = self._closed = False
        self._close_entered = self._release_entered = False

    def _start(self):
        if self._started:
            raise StorageSqliteError("connection_attempt_consumed", sqlite3.SQLITE_MISUSE)
        self._started = True
        _check("connection_lease", self._owner._call("lease", self._owner._cookie, 1))
        # Pre-own the actual object, including failure during Connection.__init__.
        # No application-defined factory or callback can substitute an owner.
        self._database = _OwnedStorageConnection.__new__(_OwnedStorageConnection)
        self._database._native_owner = self._owner
        _OwnedStorageConnection.__init__(self._database,
            self._owner.path.as_uri() + "?mode=rw&vfs=" + self._owner._name.decode("ascii"),
            uri=True, isolation_level=None, timeout=.1, check_same_thread=True)
        self._initialized = True
        db = self._database
        page = db.execute("PRAGMA page_size").fetchone()[0]
        if type(page) is not int or page < 512 or page > 65536 or page & (page-1):
            raise StorageSqliteError("page_profile", sqlite3.SQLITE_MISUSE)
        ceiling = self._owner.limits.main // page
        if ceiling < 1 or db.execute("PRAGMA page_count").fetchone()[0] > ceiling:
            raise StorageSqliteError("existing_main_capacity", sqlite3.SQLITE_FULL)
        if db.execute(f"PRAGMA max_page_count={ceiling}").fetchone()[0] != ceiling:
            raise StorageSqliteError("logical_page_ceiling", sqlite3.SQLITE_FULL)
        db.execute("PRAGMA mmap_size=0")
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA wal_autocheckpoint=0")

    @property
    def database(self) -> sqlite3.Connection:
        self._owner._require()
        if not self._initialized or self._close_entered or self._closed:
            raise StorageSqliteError("connection_unavailable", sqlite3.SQLITE_MISUSE)
        return self._database

    def close(self) -> None:
        self._owner._context()
        if self._closed:
            return
        if not self._initialized or self._close_entered or self._release_entered:
            raise StorageSqliteError("connection_outcome_unknown", sqlite3.SQLITE_MISUSE)
        self._close_entered = True
        self._database.close()
        self._release_entered = True
        _check("release_connection_lease", self._owner._call("lease", self._owner._cookie, 0))
        self._closed = True
