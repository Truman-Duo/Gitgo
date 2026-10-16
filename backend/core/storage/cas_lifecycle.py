"""Inactive CAS lifecycle ordering primitive; not a writer or GC integration.

The maintenance lease is outermost. Tokens permit explicit same-operation
nesting only; opening the same lock file again is never treated as reentrancy.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
import threading
import weakref

from .maintenance import StorageLease, _lock_file, _unlock_file
from .models import StorageBlocked


class CasLifecycleBusy(StorageBlocked):
    code = "STORAGE_CAS_LIFECYCLE_BUSY"


class _Coordinator:
    def __init__(self):
        self.active = None


def _acquire_file_lock(handle):
    # The caller registers the handle before this call. Error translation
    # owns no resource; success-before-result faults still close that handle.
    try:
        return _lock_file(handle, exclusive=True)
    except OSError as error:
        if (getattr(error, 'winerror', None) not in {32, 33}
                and error.errno not in {errno.EAGAIN, errno.EACCES}):
            raise
        raise CasLifecycleBusy(CasLifecycleBusy.code) from error


_registry_mutex = threading.Lock()
_registry = weakref.WeakValueDictionary()


def _after_fork() -> None:
    global _registry_mutex, _registry
    _registry_mutex = threading.Lock()
    _registry = weakref.WeakValueDictionary()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


class CasOperation:
    def __init__(self, lease: StorageLease, coordinator: _Coordinator):
        self._lease = lease
        self._coordinator = coordinator
        self._pid = os.getpid()
        self._thread = threading.get_ident()
        self._active = True

    def require_active(self) -> None:
        if (not self._active or self._pid != os.getpid()
                or self._thread != threading.get_ident()
                or self._coordinator.active is not self):
            raise StorageBlocked("STORAGE_CAS_OPERATION_INVALID")
        self._lease.require_active()


class CasLifecycleLock:
    def __init__(self, lease: StorageLease):
        lease.require_active()
        self._lease = lease
        self.path = lease.path.parent / "cas-lifecycle.lock"
        key = os.path.normcase(str(self.path))
        with _registry_mutex:
            coordinator = _registry.get(key)
            if coordinator is None:
                coordinator = _Coordinator()
                _registry[key] = coordinator
        self._coordinator = coordinator

    @contextmanager
    def operation(self, *, parent: CasOperation | None = None):
        owner_pid = os.getpid()
        with self._lease.use() as borrow:
            if parent is not None:
                parent.require_active()
                if (parent._lease is not self._lease
                        or parent._coordinator is not self._coordinator):
                    raise StorageBlocked("STORAGE_CAS_OPERATION_MISMATCH")
                yield parent
                return
            token = CasOperation(self._lease, self._coordinator)
            # Independent handles let the native nonblocking lock arbitrate
            # both threads and processes, without a Python acquisition gap.
            try:
                borrow.handle = self.path.open("a+b")
                os.set_inheritable(borrow.handle.fileno(), False)
                state = _acquire_file_lock(borrow.handle)
                self._coordinator.active = token
                yield token
            finally:
                try:
                    token._active = False
                    if (os.getpid() == owner_pid
                            and self._coordinator.active is token):
                        self._coordinator.active = None
                        _unlock_file(borrow.handle, state)
                finally:
                    # Close also releases a lock whose result was never
                    # returned. A fork child only closes its own copy; it
                    # must not explicitly unlock the parent's shared lock.
                    if borrow.handle is not None:
                        borrow.handle.close()
