"""Admission interruption, exact ownership, and confirmed handle cleanup."""
import errno
import inspect
import multiprocessing
import os
from pathlib import Path
import sys
import threading

import pytest

from backend.core.storage.cas_lifecycle import CasLifecycleLock
from backend.core.storage import cas_lifecycle
from backend.core.storage.models import StorageBlocked
from backend.core.storage.maintenance import StorageLease, StorageMaintenanceActive


def enter(context):
    with context:
        pass


def prefix_lines(function, context):
    code = function.__wrapped__.__code__
    seen = []
    finished = False
    old = sys.gettrace()
    def trace(frame, event, arg):
        nonlocal finished
        if frame.f_code is code and not finished:
            if event == 'line':
                seen.append(frame.f_lineno)
            if event == 'return':
                finished = True
        return trace
    try:
        sys.settrace(trace)
        enter(context)
    finally:
        sys.settrace(old)
    return sorted(set(seen))


def inject(function, line, action, error=KeyboardInterrupt):
    code = function.__wrapped__.__code__
    fired = []
    old = sys.gettrace()
    def trace(frame, event, arg):
        if frame.f_code is code and event == 'line' and frame.f_lineno == line and not fired:
            fired.append(line)
            raise error('single admission fault')
        return trace
    try:
        sys.settrace(trace)
        with pytest.raises(error, match='single admission fault') as caught:
            action()
    finally:
        sys.settrace(old)
    assert fired == [line]
    return caught.value


def cleanup(lease):
    # Public retry only; never reset internal ownership to make cleanup pass.
    if lease._closing:
        enter(lease.shutdown())
    else:
        lease.close()


@pytest.mark.parametrize('kind', ['use', 'shutdown', 'operation'])
@pytest.mark.parametrize('error', [KeyboardInterrupt, SystemExit, OSError])
def test_every_observed_admission_line(tmp_path_factory, kind, error):
    def make():
        lease = StorageLease(tmp_path_factory)
        owner = CasLifecycleLock(lease) if kind == 'operation' else lease
        return lease, owner, getattr(owner, kind)
    lease, owner, method = make()
    lines = prefix_lines(method, method())
    cleanup(lease)
    for line in lines:
        lease, owner, method = make()
        try:
            caught = inject(method, line, lambda: enter(method()), error)
            assert caught.__traceback__ is not None  # Keep frames alive through checks.
            assert not lease._users, (kind, line, 'retained borrower')
            assert lease._shutdown_active is None, (kind, line, 'retained shutdown')
            if kind == 'operation':
                assert owner._coordinator.active is None
                enter(owner.operation())
            elif kind == 'use':
                enter(lease.use())
            # Failure before a successful shutdown body never releases lease.
            assert not lease._handle.closed
        finally:
            cleanup(lease)
        with StorageLease(tmp_path_factory, exclusive=True):
            pass
    print(f'ADMISSION_MATRIX {kind} {error.__name__} {len(lines)}')


def test_nested_borrow_rejection_does_not_remove_existing_registration(tmp_path_factory):
    lease = StorageLease(tmp_path_factory)
    try:
        with lease.use():
            outer = set(lease._users)
            with pytest.raises(StorageBlocked, match='IN_USE'):
                enter(lease.shutdown())
            assert lease._users == outer
            assert not lease._closing
            with lease.use():
                assert len(lease._users) == 2
            assert lease._users == outer
    finally:
        cleanup(lease)


def test_rejected_shutdown_preserves_exact_current_owner(tmp_path_factory):
    lease = StorageLease(tmp_path_factory)
    with lease.shutdown():
        owner = lease._shutdown_active
        with pytest.raises(StorageBlocked, match='CLOSING'):
            enter(lease.shutdown())
        assert lease._shutdown_active is owner
        with pytest.raises(StorageBlocked, match='CLOSING'):
            enter(lease.use())
        assert lease._shutdown_active is owner
        assert not lease._handle.closed


def test_injected_nested_borrow_only_discards_its_own_token(tmp_path_factory):
    lease = StorageLease(tmp_path_factory)
    lines, first = inspect.getsourcelines(StorageLease.use.__wrapped__)
    target = next(first + i for i, line in enumerate(lines) if line.strip() == 'yield attempt')
    try:
        with lease.use():
            owner = set(lease._users)
            inject(StorageLease.use, target, lambda: enter(lease.use()))
            assert lease._users == owner
    finally:
        cleanup(lease)


def test_native_lock_success_before_python_result_is_unwound(tmp_path_factory, monkeypatch):
    lease = StorageLease(tmp_path_factory)
    gate = CasLifecycleLock(lease)
    original = cas_lifecycle._lock_file
    handles = []
    def acquired_then_interrupted(handle, **kwargs):
        original(handle, **kwargs)
        handles.append(handle)
        raise KeyboardInterrupt('after OS success, before result')
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(cas_lifecycle, '_lock_file', acquired_then_interrupted)
            with pytest.raises(KeyboardInterrupt):
                enter(gate.operation())
        assert len(handles) == 1 and handles[0].closed
        assert not lease._users
        enter(gate.operation())
    finally:
        cleanup(lease)


def test_native_unlock_error_still_closes_handle_and_invalidates_token(tmp_path_factory, monkeypatch):
    lease = StorageLease(tmp_path_factory)
    gate = CasLifecycleLock(lease)
    tokens = []
    handles = []
    def fail(handle, state):
        handles.append(handle)
        raise OSError(errno.EIO, 'unlock fault')
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(cas_lifecycle, '_unlock_file', fail)
            with pytest.raises(OSError, match='unlock fault'):
                with gate.operation() as token:
                    tokens.append(token)
        assert handles[0].closed
        with pytest.raises(StorageBlocked, match='INVALID'):
            tokens[0].require_active()
        assert not lease._users
        enter(gate.operation())
    finally:
        cleanup(lease)


def test_same_process_separate_handles_preserve_active_owner(tmp_path_factory):
    with StorageLease(tmp_path_factory) as lease, StorageLease(tmp_path_factory) as other:
        gate, contender = CasLifecycleLock(lease), CasLifecycleLock(other)
        with gate.operation() as owner:
            for candidate in (gate, contender):
                with pytest.raises(cas_lifecycle.CasLifecycleBusy):
                    enter(candidate.operation())
                owner.require_active()
            outcome = []
            def worker():
                try:
                    enter(contender.operation())
                    outcome.append('unexpected admission')
                except cas_lifecycle.CasLifecycleBusy:
                    outcome.append('busy')
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(3)
            assert not thread.is_alive(), 'contender waited for active operation'
            assert outcome == ['busy']
            owner.require_active()
        enter(contender.operation())


@pytest.mark.parametrize('closed_before_error', [False, True])
@pytest.mark.parametrize('failures', [1, 2])
def test_handle_close_failure_retains_only_unconfirmed_debt(tmp_path_factory, closed_before_error, failures):
    lease = StorageLease(tmp_path_factory)
    gate = CasLifecycleLock(lease)
    real_path = gate.path
    captured = []

    class Handle:
        def __init__(self, raw):
            self.raw = raw
            self.attempts = 0
        def fileno(self):
            return self.raw.fileno()
        @property
        def closed(self):
            return self.raw.closed
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
        def close(self):
            self.attempts += 1
            if self.attempts <= failures:
                if closed_before_error:
                    self.raw.close()
                raise OSError('fixture handle close failure')
            self.raw.close()

    class CandidatePath:
        def open(self, mode):
            handle = Handle(real_path.open(mode))
            captured.append(handle)
            return handle

    gate.path = CandidatePath()
    try:
        with pytest.raises(OSError, match='fixture handle close failure'):
            enter(gate.operation())
        assert gate._coordinator.active is None
        if closed_before_error:
            assert not lease._users
            lease.close()
        else:
            assert len(lease._users) == 1
            assert all(user.cleanup_pending for user in lease._users)
            with pytest.raises(StorageMaintenanceActive):
                StorageLease(tmp_path_factory, exclusive=True)
            with pytest.raises(StorageBlocked, match='CLOSING'):
                lease.close()
            with pytest.raises(StorageBlocked, match='CLOSING'):
                enter(gate.operation())
            if failures == 2:
                with pytest.raises(OSError, match='fixture handle close failure'):
                    enter(lease.shutdown())
                assert lease._shutdown_active is None
                assert len(lease._users) == 1
                with pytest.raises(StorageMaintenanceActive):
                    StorageLease(tmp_path_factory, exclusive=True)
            # Normal shutdown retries the ended operation's exact handle.
            with lease.shutdown() as admitted:
                assert admitted
                assert captured[0].closed
                assert not lease._users
            assert captured[0].attempts == failures + 1
        with StorageLease(tmp_path_factory, exclusive=True):
            pass
    finally:
        cleanup(lease)


def test_old_cleanup_debt_cannot_release_a_new_runtime_owner(tmp_path_factory):
    lease = StorageLease(tmp_path_factory)
    gate = CasLifecycleLock(lease)
    path = gate.path
    class FailCloseOnce:
        def __init__(self, handle):
            self.handle = handle
            self.attempts = 0
        @property
        def closed(self):
            return self.handle.closed
        def fileno(self):
            return self.handle.fileno()
        def close(self):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError('fixture close debt')
            self.handle.close()
    class DebtPath:
        def open(self, mode):
            return FailCloseOnce(path.open(mode))
    gate.path = DebtPath()
    try:
        with pytest.raises(OSError, match='fixture close debt'):
            enter(gate.operation())
        with StorageLease(tmp_path_factory) as other:
            current = CasLifecycleLock(other)
            with current.operation() as token:
                enter(lease.shutdown())
                token.require_active()
                with pytest.raises(cas_lifecycle.CasLifecycleBusy):
                    enter(current.operation())
    finally:
        cleanup(lease)


def _native_owner(root, ready, release, abrupt):
    with StorageLease(Path(root)) as lease:
        with CasLifecycleLock(lease).operation():
            ready.set()
            if not release.wait(12):
                os._exit(92)
            if abrupt:
                os._exit(91)


@pytest.mark.parametrize('abrupt', [False, True])
def test_real_process_owner(tmp_path_factory, abrupt):
    ctx = multiprocessing.get_context('spawn')
    ready, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_native_owner, args=(str(tmp_path_factory), ready, release, abrupt))
    child.start()
    try:
        assert ready.wait(12)
        with StorageLease(tmp_path_factory) as lease:
            gate = CasLifecycleLock(lease)
            with pytest.raises(cas_lifecycle.CasLifecycleBusy):
                enter(gate.operation())
            release.set()
            child.join(12)
            assert child.exitcode == (91 if abrupt else 0)
            enter(gate.operation())
    finally:
        release.set()
        if child.is_alive():
            child.terminate()
            child.join(5)
        child.close()
