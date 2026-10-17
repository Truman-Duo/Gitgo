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
from backend.core.storage import StorageRuntime


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


def _runtime(root):
    workspace = root / 'workspace'
    workspace.mkdir()
    return StorageRuntime(workspace, state_home=root / 'state')


def _assert_maintenance_available(runtime):
    with StorageLease(runtime.paths.project_root, exclusive=True):
        pass


def test_runtime_busy_close_preserves_connections_and_can_retry(tmp_path_factory):
    runtime = _runtime(tmp_path_factory)
    try:
        with CasLifecycleLock(runtime._maintenance_lease).operation():
            with pytest.raises(StorageBlocked, match='LEASE_IN_USE'):
                runtime.close()
            assert not runtime._closed
            runtime.put_state_ref('fixture', 'fact', 'still-open')
            assert runtime.get_state_ref('fixture', 'fact') == 'still-open'
        runtime.close()
        runtime.close()
        _assert_maintenance_available(runtime)
    finally:
        runtime.close()


def test_other_threads_operation_prevents_runtime_close(tmp_path_factory):
    runtime = _runtime(tmp_path_factory)
    ready, release = threading.Event(), threading.Event()
    errors = []
    def worker():
        try:
            with CasLifecycleLock(runtime._maintenance_lease).operation():
                ready.set()
                assert release.wait(10)
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert ready.wait(5)
        with pytest.raises(StorageBlocked, match='LEASE_IN_USE'):
            runtime.close()
        assert runtime._state.execute('SELECT 1').fetchone()[0] == 1
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert not errors
        runtime.close()
        _assert_maintenance_available(runtime)
    finally:
        release.set()
        thread.join(5)
        runtime.close()


def test_shutdown_excludes_new_operations_and_concurrent_close(tmp_path_factory, monkeypatch):
    runtime = _runtime(tmp_path_factory)
    gate = CasLifecycleLock(runtime._maintenance_lease)
    ready, release = threading.Event(), threading.Event()
    errors = []
    original = runtime._state.close
    def paused_close():
        ready.set()
        assert release.wait(10)
        original()
    monkeypatch.setattr(runtime._state, 'close', paused_close)
    def closer():
        try:
            runtime.close()
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=closer)
    thread.start()
    try:
        assert ready.wait(5)
        with pytest.raises(StorageBlocked, match='LEASE_CLOSING'):
            with gate.operation():
                pytest.fail('new operation admitted during shutdown')
        with pytest.raises(StorageBlocked, match='LEASE_CLOSING'):
            runtime.close()
        with pytest.raises(StorageMaintenanceActive):
            StorageLease(runtime.paths.project_root, exclusive=True)
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert not errors
        runtime.close()
        _assert_maintenance_available(runtime)
    finally:
        release.set()
        thread.join(5)
        runtime.close()


def test_shutdown_callback_cannot_reenter_close_or_admit_an_operation(tmp_path_factory, monkeypatch):
    runtime = _runtime(tmp_path_factory)
    gate = CasLifecycleLock(runtime._maintenance_lease)
    observed = []
    def callback():
        with pytest.raises(StorageBlocked, match='LEASE_CLOSING'):
            runtime.close()
        with pytest.raises(StorageBlocked, match='LEASE_CLOSING'):
            runtime._maintenance_lease.close()
        with pytest.raises(StorageBlocked, match='LEASE_CLOSING'):
            with gate.operation():
                pytest.fail('callback admitted an operation')
        observed.append('denied')
    monkeypatch.setattr(runtime, 'record_storage_metric', callback)
    runtime.close()
    assert observed == ['denied']
    _assert_maintenance_available(runtime)


@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt, SystemExit])
def test_failed_connection_close_keeps_barrier_until_retry(tmp_path_factory, monkeypatch, error_type):
    runtime = _runtime(tmp_path_factory)
    gate = CasLifecycleLock(runtime._maintenance_lease)
    state_close, observation_close = runtime._state.close, runtime._observability.close
    attempts = []
    def fail_once():
        attempts.append('state')
        if attempts.count('state') == 1:
            raise error_type('fixture close failure')
        state_close()
    def close_observation():
        attempts.append('observability')
        observation_close()
    monkeypatch.setattr(runtime._state, 'close', fail_once)
    monkeypatch.setattr(runtime._observability, 'close', close_observation)
    try:
        with pytest.raises(error_type, match='fixture close failure'):
            runtime.close()
        assert not runtime._closed
        with pytest.raises(StorageBlocked, match='RUNTIME_CLOSING'):
            runtime.put_state_ref('fixture', 'fact', 'rejected')
        with pytest.raises(StorageBlocked, match='LEASE_CLOSING'):
            runtime._maintenance_lease.close()
        with pytest.raises(StorageBlocked, match='LEASE_CLOSING'):
            with gate.operation():
                pytest.fail('failed shutdown admitted an operation')
        with pytest.raises(StorageMaintenanceActive):
            StorageLease(runtime.paths.project_root, exclusive=True)
        runtime.close()
        assert attempts == ['state', 'observability', 'state']
        assert runtime._closed
        _assert_maintenance_available(runtime)
    finally:
        runtime.close()


def test_diagnostic_failure_after_connection_close_releases_maintenance(tmp_path_factory, monkeypatch):
    runtime = _runtime(tmp_path_factory)
    def fail(_health):
        raise OSError('fixture diagnostic failure')
    monkeypatch.setattr(runtime, '_write_health_snapshot', fail)
    with pytest.raises(OSError, match='fixture diagnostic failure'):
        runtime.close()
    assert runtime._closed
    runtime.close()
    _assert_maintenance_available(runtime)


def test_unlock_failure_still_closes_handle_and_runtime(tmp_path_factory, monkeypatch):
    from backend.core.storage import maintenance
    runtime = _runtime(tmp_path_factory)
    def fail(*args, **kwargs):
        raise OSError('fixture unlock failure')
    with monkeypatch.context() as scoped:
        scoped.setattr(maintenance, '_unlock_file', fail)
        with pytest.raises(OSError, match='fixture unlock failure'):
            runtime.close()
    assert runtime._closed
    assert runtime._maintenance_lease._handle.closed
    runtime.close()
    _assert_maintenance_available(runtime)


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
