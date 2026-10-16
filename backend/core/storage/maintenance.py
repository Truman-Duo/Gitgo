"""Cross-process admission barrier for explicit storage maintenance.

Normal runtimes hold a shared lease. Recovery/replacement holds an exclusive
lease. A stable lock file is never removed or renamed with a database family.
This complements SQLite's locks; it does not serialize normal SQL traffic.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from .models import StorageBlocked


def _windows_lock(handle, *, exclusive: bool, unlock: bool = False, overlapped=None):
    # The CRT locking API does not provide the shared lease semantics we need.
    # Use LockFileEx explicitly, with a correctly pointer-sized OVERLAPPED.
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    state = overlapped if overlapped is not None else Overlapped()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    raw = wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno()))
    if unlock:
        function = kernel.UnlockFileEx
        function.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                             wintypes.DWORD, ctypes.c_void_p]
        arguments = [raw, 0, 1, 0, ctypes.byref(state)]
    else:
        function = kernel.LockFileEx
        function.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                             wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        arguments = [raw, 1 | (2 if exclusive else 0), 0, 1, 0, ctypes.byref(state)]
    function.restype = wintypes.BOOL
    if not function(*arguments):
        raise ctypes.WinError(ctypes.get_last_error())
    return state


class StorageMaintenanceActive(StorageBlocked):
    code = "STORAGE_MAINTENANCE_BUSY"


def _lock_file(handle, *, exclusive: bool):
    if os.name == "nt":
        return _windows_lock(handle, exclusive=exclusive)
    import fcntl
    fcntl.flock(handle.fileno(),
                (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    return None


def _unlock_file(handle, state) -> None:
    if os.name == "nt":
        _windows_lock(handle, exclusive=False, unlock=True, overlapped=state)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class StorageLease:
    def __init__(self, project_root: Path, *, exclusive: bool = False):
        self.path = project_root.resolve() / "storage-maintenance.lock"
        self._pid = os.getpid()
        self._mutex = threading.RLock()
        self._users = 0
        self._handle = self.path.open("a+b")
        try:
            os.set_inheritable(self._handle.fileno(), False)
            self._overlapped = _lock_file(self._handle, exclusive=exclusive)
        except OSError as exc:
            self._handle.close()
            raise StorageMaintenanceActive(
                "Storage is in use or under maintenance; close project runtimes "
                "before recovery, then retry. No database was replaced."
            ) from exc

    def require_active(self) -> None:
        # Do this before touching a potentially inherited Python mutex.
        if os.getpid() != self._pid:
            raise StorageBlocked("STORAGE_LEASE_PROCESS_CHANGED")
        with self._mutex:
            if self._handle.closed:
                raise StorageBlocked("STORAGE_LEASE_CLOSED")

    @contextmanager
    def use(self):
        """Prevent maintenance admission from disappearing during an operation."""
        self.require_active()
        with self._mutex:
            self.require_active()
            self._users += 1
        try:
            yield
        finally:
            if os.getpid() == self._pid:
                with self._mutex:
                    self._users -= 1

    def close(self) -> None:
        if os.getpid() != self._pid:
            raise StorageBlocked("STORAGE_LEASE_PROCESS_CHANGED")
        with self._mutex:
            if self._handle.closed:
                return
            if self._users:
                raise StorageBlocked("STORAGE_LEASE_IN_USE")
            try:
                _unlock_file(self._handle, self._overlapped)
            finally:
                self._handle.close()

    def __enter__(self):
        self.require_active()
        return self

    def __exit__(self, *_args):
        self.close()
