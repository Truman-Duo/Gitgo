"""Real process/thread exclusion and token/lease ownership, without CAS rollout."""
import multiprocessing
import errno
import os
from pathlib import Path
import threading
import time

import pytest

from backend.core.storage.cas_lifecycle import CasLifecycleLock, CasLifecycleBusy, CasOperation
from backend.core.storage.maintenance import StorageLease, StorageMaintenanceActive
from backend.core.storage.models import StorageBlocked


def test_explicit_nesting_and_other_runtime_exclusion(tmp_path_factory):
    with StorageLease(tmp_path_factory) as first, StorageLease(tmp_path_factory) as second:
        gate, other = CasLifecycleLock(first), CasLifecycleLock(second)
        with gate.operation() as token:
            with gate.operation(parent=token) as nested:
                assert nested is token
            with pytest.raises(CasLifecycleBusy):
                with gate.operation():
                    pass
            with pytest.raises(CasLifecycleBusy):
                with other.operation():
                    pass
            with pytest.raises(StorageBlocked, match='MISMATCH'):
                with other.operation(parent=token):
                    pass
            with pytest.raises(StorageBlocked, match='LEASE_IN_USE'):
                first.close()
            with pytest.raises(StorageMaintenanceActive):
                StorageLease(tmp_path_factory, exclusive=True)
        with pytest.raises(StorageBlocked, match='INVALID'):
            with gate.operation(parent=token):
                pass
        with other.operation():
            pass
        assert gate.path.is_file()  # Never unlink or replace the lock domain.


@pytest.mark.parametrize('error_type', [ValueError, KeyboardInterrupt, SystemExit])
def test_exception_releases_gate_and_unissued_token_is_invalid(tmp_path_factory, error_type):
    with StorageLease(tmp_path_factory) as lease:
        gate = CasLifecycleLock(lease)
        forged = CasOperation(lease, gate._coordinator)
        with pytest.raises(StorageBlocked, match='INVALID'):
            with gate.operation(parent=forged):
                pass
        with pytest.raises(error_type, match='fixture'):
            with gate.operation():
                raise error_type('fixture')
        with gate.operation() as token:
            token.require_active()


def test_lock_io_failure_is_not_reported_as_busy_and_cleans_up(tmp_path_factory, monkeypatch):
    from backend.core.storage import cas_lifecycle
    with StorageLease(tmp_path_factory) as lease:
        assert not os.get_inheritable(lease._handle.fileno())
        gate = CasLifecycleLock(lease)
        original = cas_lifecycle._lock_file
        handles = []
        def fail(handle, **kwargs):
            assert not os.get_inheritable(handle.fileno())
            handles.append(handle)
            raise OSError(errno.EIO, 'fixture I/O failure')
        with monkeypatch.context() as scoped:
            scoped.setattr(cas_lifecycle, '_lock_file', fail)
            with pytest.raises(OSError, match='fixture I/O failure') as caught:
                with gate.operation():
                    pass
            assert not isinstance(caught.value, CasLifecycleBusy)
        assert handles[0].closed
        assert cas_lifecycle._lock_file is original
        with gate.operation():
            pass


def test_different_projects_do_not_share_one_operation_lock(tmp_path_factory):
    other_root = tmp_path_factory / 'other'
    other_root.mkdir()
    with StorageLease(tmp_path_factory) as first, StorageLease(other_root) as second:
        with CasLifecycleLock(first).operation(), CasLifecycleLock(second).operation():
            pass


def test_thread_cannot_borrow_another_threads_operation(tmp_path_factory):
    errors = []
    with StorageLease(tmp_path_factory) as lease:
        gate = CasLifecycleLock(lease)
        with gate.operation() as token:
            def other_thread():
                for parent in (None, token):
                    try:
                        with gate.operation(parent=parent):
                            errors.append('unexpected admission')
                    except StorageBlocked as error:
                        errors.append(type(error))
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join(5)
            assert not thread.is_alive()
        assert errors == [CasLifecycleBusy, StorageBlocked]


def test_closed_lease_cannot_create_or_enter_gate(tmp_path_factory):
    lease = StorageLease(tmp_path_factory)
    gate = CasLifecycleLock(lease)
    lease.close()
    lease.close()
    with pytest.raises(StorageBlocked, match='CLOSED'):
        CasLifecycleLock(lease)
    with pytest.raises(StorageBlocked, match='CLOSED'):
        with gate.operation():
            pass


def _hold_gate(root, ready, release, abrupt):
    with StorageLease(Path(root)) as lease:
        with CasLifecycleLock(lease).operation():
            ready.set()
            if not release.wait(15):
                os._exit(24)
            if abrupt:
                os._exit(23)


@pytest.mark.parametrize('abrupt', [False, True])
def test_separate_process_is_excluded_until_owner_finishes(tmp_path_factory, abrupt):
    context = multiprocessing.get_context('spawn')
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_hold_gate,
        args=(str(tmp_path_factory), ready, release, abrupt))
    process.start()
    try:
        assert ready.wait(15)
        with StorageLease(tmp_path_factory) as lease:
            gate = CasLifecycleLock(lease)
            with pytest.raises(CasLifecycleBusy):
                with gate.operation():
                    pass
            release.set()
            process.join(15)
            assert process.exitcode == (23 if abrupt else 0)
            deadline = time.monotonic() + 5
            while True:
                try:
                    with gate.operation():
                        break
                except CasLifecycleBusy:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.02)
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(5)
        process.close()


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='requires POSIX fork')
def test_fork_child_cannot_unlock_parent_operation(tmp_path_factory):
    # The child unwinds inherited contexts. Explicit flock(LOCK_UN) there
    # would release the parent's shared open-file-description lock.
    read_fd, write_fd = os.pipe()
    child_pid = None
    try:
        with StorageLease(tmp_path_factory) as lease:
            with CasLifecycleLock(lease).operation() as token:
                child_pid = os.fork()
                if child_pid == 0:
                    os.close(read_fd)
                    try:
                        token.require_active()
                    except StorageBlocked:
                        os.write(write_fd, b'rejected')
                    # Unwind both inherited context managers; lease close
                    # rejects the changed process without unlocking it.
                else:
                    os.close(write_fd)
                    import select
                    assert select.select([read_fd], [], [], 10)[0]
                    assert os.read(read_fd, 32) == b'rejected'
                    _, status = os.waitpid(child_pid, 0)
                    assert os.waitstatus_to_exitcode(status) == 23
                    import fcntl
                    with (tmp_path_factory / 'cas-lifecycle.lock').open('a+b') as probe:
                        with pytest.raises(BlockingIOError):
                            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with StorageLease(tmp_path_factory) as other:
                        with pytest.raises(CasLifecycleBusy):
                            with CasLifecycleLock(other).operation():
                                pass
    except StorageBlocked:
        if child_pid == 0:
            os._exit(23)
        raise
    finally:
        if child_pid == 0:
            os._exit(25)
        os.close(read_fd)
    with StorageLease(tmp_path_factory, exclusive=True):
        pass
