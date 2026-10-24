"""Cache retirement retains exact owners, barriers and finite capacity."""
from collections import OrderedDict
import gc
import inspect
import os
import sys
import threading
import weakref

import pytest

from backend.core.storage import bindings
from backend.core.storage.models import StorageBlocked
from backend.core.storage.maintenance import StorageLease, StorageMaintenanceActive


class Runtime:
    def __init__(self, path):
        self.path = path
        self._closed = False
        self._close_started = False
        self.calls = 0
        self.failures = []
        self.hook = lambda: None

    def close(self):
        self.calls += 1
        self.hook()
        if self.failures:
            raise self.failures.pop(0)
        self._closed = True


@pytest.fixture
def pool(monkeypatch, tmp_path_factory):
    assert not bindings._owned
    made = []
    def factory(path):
        runtime = Runtime(path)
        made.append(runtime)
        return runtime
    monkeypatch.setattr(bindings, '_owned', OrderedDict())
    monkeypatch.setattr(bindings, '_bound', weakref.WeakValueDictionary())
    monkeypatch.setattr(bindings, '_bindings', {})
    monkeypatch.setattr(bindings, '_MAX_OWNED_RUNTIMES', 2)
    monkeypatch.setattr(bindings, 'StorageRuntime', factory)
    yield tmp_path_factory, made
    for runtime in made:
        runtime.failures.clear()
        runtime.hook = lambda: None
    bindings.close_owned_storage()


@pytest.mark.parametrize('error', [OSError, KeyboardInterrupt, SystemExit])
def test_release_retains_failed_close_and_retries_exact_owner(pool, error):
    root, made = pool
    runtime = bindings.get_storage(root)
    runtime.failures = [error('first close'), OSError('second close')]
    ref = weakref.ref(runtime)
    for expected in (error, OSError):
        with pytest.raises(expected):
            bindings.release_storage(root)
        with pytest.raises(StorageBlocked, match='RETIRING'):
            bindings.get_storage(root)
        assert len(made) == 1
    made.clear()
    del runtime
    gc.collect()
    assert ref() is not None
    bindings.release_storage(root)
    gc.collect()
    assert ref() is None


def test_eviction_closes_before_creating_and_failed_debt_counts_against_capacity(pool):
    root, made = pool
    first = bindings.get_storage(root/'a')
    second = bindings.get_storage(root/'b')
    first.failures = [OSError('busy close'), OSError('still busy')]
    for _ in range(2):
        with pytest.raises(OSError):
            bindings.get_storage(root/'c')
        assert made == [first, second]
        assert len(bindings._owned) == 2
    third = bindings.get_storage(root/'c')
    assert first._closed and not second._closed
    assert third is made[2] and len(bindings._owned) == 2


def test_replacement_not_published_until_old_runtime_closes(pool):
    root, made = pool
    old = bindings.get_storage(root)
    replacement = Runtime(root)
    old.failures = [OSError('not closed')]
    with pytest.raises(OSError):
        bindings.bind_storage(root, replacement)
    assert not bindings._bound
    assert replacement.calls == 0
    with pytest.raises(StorageBlocked, match='RETIRING'):
        bindings.get_storage(root)
    bindings.bind_storage(root, replacement)
    assert bindings.get_storage(root) is replacement
    assert old.calls == 2 and old._closed
    bindings.release_storage(root)
    bindings.close_owned_storage()
    assert replacement.calls == 0
    bindings.unbind_storage(root, replacement)


def test_binding_same_live_runtime_transfers_ownership_without_closing(pool):
    root, made = pool
    runtime = bindings.get_storage(root)
    bindings.bind_storage(root, runtime)
    assert not bindings._owned
    bindings.close_owned_storage()
    assert bindings.get_storage(root) is runtime and runtime.calls == 0


def test_same_runtime_cannot_escape_retirement_by_becoming_injected(pool):
    root, made = pool
    runtime = bindings.get_storage(root)
    runtime.failures = [OSError('not closed')]
    with pytest.raises(OSError): bindings.release_storage(root)
    with pytest.raises(StorageBlocked, match='RETIRING'):
        bindings.bind_storage(root, runtime)
    assert not bindings._bound
    bindings.release_storage(root)


def test_close_all_reports_failure_retains_it_and_closes_other_entries(pool):
    root, made = pool
    first = bindings.get_storage(root/'a')
    second = bindings.get_storage(root/'b')
    first.failures = [OSError('retain first')]
    with pytest.raises(OSError, match='retain first'): bindings.close_owned_storage()
    assert second._closed and not first._closed
    assert len(bindings._owned) == 1
    bindings.close_owned_storage()
    assert first.calls == 2 and second.calls == 1 and not bindings._owned


def test_reentrant_closer_cannot_clear_original_attempt(pool):
    root, made = pool
    runtime = bindings.get_storage(root)
    def reenter():
        with pytest.raises(StorageBlocked, match='BUSY'): bindings.release_storage(root)
        with pytest.raises(StorageBlocked, match='RETIRING'): bindings.get_storage(root)
        entry = bindings._owned[bindings._key(root)]
        assert entry.closer is not None
    runtime.hook = reenter
    bindings.release_storage(root)
    assert runtime.calls == 1 and not bindings._owned


def test_parallel_closer_does_not_block_other_key_or_remove_original_attempt(pool):
    root, made = pool
    runtime = bindings.get_storage(root/'a')
    entered, finish = threading.Event(), threading.Event()
    errors = []
    def hold():
        entered.set()
        assert finish.wait(10)
    runtime.hook = hold
    def close():
        try: bindings.release_storage(root/'a')
        except BaseException as error: errors.append(error)
    thread = threading.Thread(target=close)
    thread.start()
    try:
        assert entered.wait(10)
        with pytest.raises(StorageBlocked, match='BUSY'): bindings.release_storage(root/'a')
        with pytest.raises(StorageBlocked, match='RETIRING'): bindings.get_storage(root/'a')
        assert bindings.get_storage(root/'b') is made[1]
        assert runtime.calls == 1
    finally:
        finish.set(); thread.join(10)
    assert not thread.is_alive() and not errors


def test_failed_close_after_physical_close_requires_idempotent_confirmation(pool):
    root, made = pool
    runtime = bindings.get_storage(root)
    def physical_close(): runtime._closed = True
    runtime.hook = physical_close
    runtime.failures = [OSError('reply lost')]
    with pytest.raises(OSError): bindings.release_storage(root)
    assert len(bindings._owned) == 1
    with pytest.raises(StorageBlocked, match='RETIRING'): bindings.get_storage(root)
    bindings.release_storage(root)
    assert runtime.calls == 2


def test_failed_reentrant_binding_does_not_clear_original_binding_attempt(pool):
    root, made = pool
    runtime = bindings.get_storage(root)
    replacement = Runtime(root)
    def reenter():
        with pytest.raises(StorageBlocked, match='BUSY'): bindings.bind_storage(root, Runtime(root))
        with pytest.raises(StorageBlocked, match='BUSY'): bindings.get_storage(root)
        assert bindings._bindings
    runtime.hook = reenter
    bindings.bind_storage(root, replacement)
    assert bindings.get_storage(root) is replacement


def test_closed_injected_runtime_does_not_create_fallback(pool):
    root, made = pool
    injected = Runtime(root)
    bindings.bind_storage(root, injected)
    injected._close_started = True
    with pytest.raises(StorageBlocked, match='UNAVAILABLE'): bindings.get_storage(root)
    assert not made


def test_creation_reserves_capacity_and_close_all_seals_pending_result(pool, monkeypatch):
    root, made = pool
    def create(path):
        runtime = Runtime(path)
        made.append(runtime)
        with pytest.raises(StorageBlocked, match='BUSY'): bindings.get_storage(root)
        with pytest.raises(StorageBlocked, match='BUSY'): bindings.close_owned_storage()
        return runtime
    monkeypatch.setattr(bindings, 'StorageRuntime', create)
    with pytest.raises(StorageBlocked, match='RETIRING'): bindings.get_storage(root)
    assert len(bindings._owned) == 1 and made[0].calls == 0
    bindings.release_storage(root)
    assert made[0]._closed


def test_factory_failure_does_not_leave_empty_slot(pool, monkeypatch):
    root, made = pool
    def fail(path): raise OSError('constructor failed')
    monkeypatch.setattr(bindings, 'StorageRuntime', fail)
    with pytest.raises(OSError): bindings.get_storage(root)
    assert not bindings._owned


def test_cached_reuse_preserves_lru_and_does_not_close_caller_owned_facade(pool):
    root, made = pool
    first = bindings.get_storage(root/'a')
    second = bindings.get_storage(root/'b')
    assert bindings.get_storage(root/'a') is first
    third = bindings.get_storage(root/'c')
    assert second._closed and not first._closed and not third._closed
    assert len(made) == 3


def test_old_retirement_reference_cannot_close_replacement(pool):
    root, made = pool
    original = bindings.get_storage(root)
    key = bindings._key(root)
    entry = bindings._owned[key]
    bindings.release_storage(root)
    replacement = bindings.get_storage(root)
    bindings._retire(key, entry)
    assert original.calls == 1 and replacement.calls == 0


def test_inherited_pool_is_rejected_before_using_parent_mutex_or_runtime(pool, monkeypatch):
    root, made = pool
    runtime = bindings.get_storage(root)
    with monkeypatch.context() as changed:
        changed.setattr(bindings, '_pid', os.getpid()+1)
        for call in (lambda: bindings.get_storage(root), lambda: bindings.release_storage(root),
                     lambda: bindings.bind_storage(root, runtime),
                     lambda: bindings.unbind_storage(root, runtime), bindings.close_owned_storage):
            with pytest.raises(StorageBlocked, match='PROCESS_CHANGED'): call()
        bindings._close_at_exit()
        assert runtime.calls == 0
    bindings.release_storage(root)


@pytest.mark.parametrize('error', [OSError, KeyboardInterrupt, SystemExit])
def test_interruption_after_empty_slot_registration_leaves_no_unreachable_reservation(pool, error):
    root, made = pool
    lines, start = inspect.getsourcelines(bindings._get_owned)
    target = start + next(i for i, line in enumerate(lines) if line.strip() == 'break')
    fired = []
    def trace(frame, event, arg):
        if (frame.f_code is bindings._get_owned.__code__ and event == 'line'
                and frame.f_lineno == target and not fired):
            fired.append(target)
            raise error('reservation response lost')
        return trace
    old = sys.gettrace()
    try:
        sys.settrace(trace)
        with pytest.raises(error, match='reservation response lost'):
            bindings.get_storage(root)
    finally: sys.settrace(old)
    assert fired and not made and not bindings._owned
    assert bindings.get_storage(root) is made[0]


def test_real_runtime_close_failure_keeps_maintenance_barrier(tmp_path_factory, monkeypatch):
    workspace = tmp_path_factory/'project'
    workspace.mkdir()
    runtime = bindings.get_storage(workspace)
    root = runtime.paths.project_root
    real = runtime._state
    class FailingClose:
        calls = 0
        def __getattr__(self, name): return getattr(real, name)
        def close(self):
            self.calls += 1
            if self.calls < 3: raise OSError('actual connection still owned')
            real.close()
    proxy = FailingClose()
    runtime._state = proxy
    try:
        for _ in range(2):
            with pytest.raises(OSError): bindings.release_storage(workspace)
            with pytest.raises(StorageMaintenanceActive):
                with StorageLease(root, exclusive=True): pass
            with pytest.raises(StorageBlocked, match='RETIRING'): bindings.get_storage(workspace)
        bindings.release_storage(workspace)
        with StorageLease(root, exclusive=True): pass
        assert proxy.calls == 3 and not bindings._owned
    finally:
        proxy.calls = 3
        bindings.release_storage(workspace)


@pytest.mark.parametrize("shared", [False, True])
def test_parallel_open_shares_exact_attempt_without_blocking_other_keys(pool, monkeypatch, shared):
    root, made = pool
    entered, finish, waiting = threading.Event(), threading.Event(), threading.Event()
    results, errors = [], []
    def create():
        entered.set()
        assert finish.wait(5)
        runtime = Runtime(root)
        made.append(runtime)
        return runtime
    key = "shared-knowledge:test" if shared else bindings._key(root)
    def open_one():
        try: results.append(bindings._get_owned(key, create))
        except BaseException as error: errors.append(error)
    first = threading.Thread(target=open_one)
    first.start()
    assert entered.wait(5)
    entry = bindings._owned[key]
    original_wait = entry.ready.wait
    def observe_wait(timeout):
        waiting.set()
        return original_wait(timeout)
    monkeypatch.setattr(entry.ready, "wait", observe_wait)
    second = threading.Thread(target=open_one)
    second.start()
    try:
        assert waiting.wait(5)
        other = bindings.get_storage(root / "other")
        assert not other._closed and not results
    finally:
        finish.set(); first.join(5); second.join(5)
    assert not first.is_alive() and not second.is_alive() and not errors
    assert len(results) == 2 and results[0] is results[1]
    assert len(made) == 2


@pytest.mark.parametrize("fail", [False, True])
def test_pending_open_timeout_or_failure_never_mints_another_owner(pool, monkeypatch, fail):
    root, made = pool
    entered, finish, waiting = threading.Event(), threading.Event(), threading.Event()
    errors, attempts = [], []
    def create(path):
        attempts.append(path)
        entered.set()
        assert finish.wait(5)
        if fail: raise OSError("open failed")
        runtime = Runtime(path)
        made.append(runtime)
        return runtime
    monkeypatch.setattr(bindings, "StorageRuntime", create)
    def open_one():
        try: bindings.get_storage(root)
        except BaseException as error: errors.append(error)
    first = threading.Thread(target=open_one)
    first.start()
    assert entered.wait(5)
    entry = bindings._owned[bindings._key(root)]
    if not fail:
        monkeypatch.setattr(bindings, "_OPEN_WAIT_SECONDS", 0.01)
        try:
            with pytest.raises(StorageBlocked, match="BUSY"): bindings.get_storage(root)
            assert len(attempts) == 1 and entry.creating
        finally:
            finish.set(); first.join(5)
        assert not errors and bindings.get_storage(root) is made[0]
    else:
        original_wait = entry.ready.wait
        def observe_wait(timeout):
            waiting.set()
            return original_wait(timeout)
        monkeypatch.setattr(entry.ready, "wait", observe_wait)
        second = threading.Thread(target=open_one)
        second.start()
        try: assert waiting.wait(5)
        finally:
            finish.set(); first.join(5); second.join(5)
        assert len(errors) == 2 and all(isinstance(error, OSError) for error in errors)
        assert all(str(error) == "open failed" for error in errors)
        assert len(attempts) == 1 and not bindings._owned
