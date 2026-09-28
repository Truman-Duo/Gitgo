"""Cross-process admission barrier for explicit storage maintenance.

Normal runtimes hold a shared lease. Recovery/replacement holds an exclusive
lease. A stable lock file is never removed or renamed with a database family.
This complements SQLite's locks; it does not serialize normal SQL traffic.
"""
from __future__ import annotations

import os
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


class StorageLease:
    def __init__(self, project_root: Path, *, exclusive: bool = False):
        self.path = project_root / "storage-maintenance.lock"
        self._handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                self._overlapped = _windows_lock(self._handle, exclusive=exclusive)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(),
                            (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            raise StorageMaintenanceActive(
                "Storage is in use or under maintenance; close project runtimes "
                "before recovery, then retry. No database was replaced."
            ) from exc

    def close(self) -> None:
        if self._handle.closed:
            return
        try:
            if os.name == "nt":
                _windows_lock(self._handle, exclusive=False, unlock=True,
                              overlapped=self._overlapped)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
