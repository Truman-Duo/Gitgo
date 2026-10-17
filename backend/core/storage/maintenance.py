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


class _LeaseBorrow:
    """Owner-private registration, never a caller-supplied release credential."""

    def __init__(self):
        self.handle = None
        self.cleanup_pending = False


class StorageLease:
    def __init__(self, project_root: Path, *, exclusive: bool = False):
        self.path = project_root.resolve() / "storage-maintenance.lock"
        self._pid = os.getpid()
        self._mutex = threading.RLock()
        self._users: set[_LeaseBorrow] = set()
        self._closing = False
        self._shutdown_active: object | None = None
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
        """Check physical lease ownership, including during owner shutdown."""
        # Do this before touching a potentially inherited Python mutex.
        if os.getpid() != self._pid:
            raise StorageBlocked("STORAGE_LEASE_PROCESS_CHANGED")
        with self._mutex:
            if self._handle.closed:
                raise StorageBlocked("STORAGE_LEASE_CLOSED")

    @contextmanager
    def use(self):
        """Prevent maintenance admission from disappearing during an operation."""
        attempt = _LeaseBorrow()
        self.require_active()
        try:
            with self._mutex:
                self.require_active()
                if self._closing:
                    raise StorageBlocked('STORAGE_LEASE_CLOSING')
                self._users.add(attempt)
            yield attempt
        finally:
            if os.getpid() == self._pid:
                with self._mutex:
                    if attempt.handle is None or attempt.handle.closed:
                        self._users.discard(attempt)
                    else:
                        # The operation has ended, but its registered handle
                        # has not closed. Keep its exact debt and the barrier.
                        self._closing = True
                        attempt.cleanup_pending = True

    def close(self) -> None:
        if os.getpid() != self._pid:
            raise StorageBlocked("STORAGE_LEASE_PROCESS_CHANGED")
        with self._mutex:
            if self._handle.closed:
                return
            if self._closing:
                raise StorageBlocked('STORAGE_LEASE_CLOSING')
            if self._users:
                raise StorageBlocked("STORAGE_LEASE_IN_USE")
            self._release_handle()

    def _release_handle(self) -> None:
        # Caller owns _mutex. Closing the handle releases the OS lock even
        # when explicit unlock reports an error.
        try:
            _unlock_file(self._handle, self._overlapped)
        finally:
            self._handle.close()

    @contextmanager
    def shutdown(self):
        """Atomically stop borrowing before closing the owner's resources.

        Busy admission leaves the lease usable. Once admitted, new borrowers
        stay blocked, including after a failed cleanup. Only a successful body
        releases the physical lease. A failed body may be retried by its owner.
        """
        attempt = object()
        if os.getpid() != self._pid:
            raise StorageBlocked('STORAGE_LEASE_PROCESS_CHANGED')
        try:
            with self._mutex:
                admitted = not self._handle.closed
                if admitted:
                    if self._shutdown_active is not None:
                        raise StorageBlocked('STORAGE_LEASE_CLOSING')
                    if any(not user.cleanup_pending for user in self._users):
                        raise StorageBlocked('STORAGE_LEASE_IN_USE')
                    self._closing = True
                    self._shutdown_active = attempt
                    # Only ended operations with exact registered handles
                    # may be retried. Live borrowers still return IN_USE.
                    for user in tuple(self._users):
                        user.handle.close()
                        if not user.handle.closed:
                            raise StorageBlocked('STORAGE_RESOURCE_CLOSE_UNKNOWN')
                        self._users.discard(user)
            yield admitted
            if admitted and os.getpid() == self._pid:
                with self._mutex:
                    if self._shutdown_active is not attempt:
                        raise StorageBlocked('STORAGE_SHUTDOWN_OWNER_CHANGED')
                    self._release_handle()
        finally:
            if os.getpid() == self._pid:
                with self._mutex:
                    if self._shutdown_active is attempt:
                        self._shutdown_active = None

    def __enter__(self):
        self.require_active()
        return self

    def __exit__(self, *_args):
        self.close()
