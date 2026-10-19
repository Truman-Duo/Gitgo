"""Bounded process-owned storage facades and confirmed retirement.

A failed close keeps the original runtime in its cache slot. Retiring entries
are unavailable to callers and still consume capacity until close confirms
completion. Caller-injected runtimes remain caller-owned.
"""
from __future__ import annotations

import atexit
from collections import OrderedDict
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import threading
import weakref

from .models import StorageBlocked
from .runtime import StorageRuntime


_MAX_OWNED_RUNTIMES = 16
_lock = threading.RLock()
_pid = os.getpid()
_bound: weakref.WeakValueDictionary[str, StorageRuntime] = weakref.WeakValueDictionary()


@dataclass(eq=False)
class _Entry:
    runtime: StorageRuntime | None = None
    creating: bool = True
    retiring: bool = False
    closer: object | None = None


_owned: OrderedDict[str, _Entry] = OrderedDict()
_bindings: dict[str, object] = {}


def _key(workspace: str | Path) -> str:
    _require_process()
    return str(Path(workspace).resolve()).casefold()


def _require_process() -> None:
    if os.getpid() != _pid:
        raise StorageBlocked("STORAGE_BINDING_PROCESS_CHANGED")


def _available(runtime: StorageRuntime) -> bool:
    return not (getattr(runtime, "_closed", False) or getattr(runtime, "_close_started", False))


def _retire(key: str, entry: _Entry) -> None:
    # State transitions are short. Never hold the pool lock during Runtime I/O
    # or callbacks; only the admitted attempt may clear its own closer marker.
    attempt = object()
    try:
        with _lock:
            if _owned.get(key) is not entry:
                return  # An earlier confirmed close already removed this entry.
            if entry.creating or entry.closer is not None:
                raise StorageBlocked("STORAGE_BINDING_BUSY")
            entry.retiring = True
            entry.closer = attempt
        entry.runtime.close()
        with _lock:
            if _owned.get(key) is entry:
                del _owned[key]
    finally:
        with _lock:
            if entry.closer is attempt:
                entry.closer = None


def bind_storage(workspace: str | Path, runtime: StorageRuntime) -> None:
    """Publish the caller's facade only after any old owned facade is closed."""
    key = _key(workspace)
    attempt = object()
    try:
        with _lock:
            if key in _bindings:
                raise StorageBlocked("STORAGE_BINDING_BUSY")
            if not _available(runtime):
                raise StorageBlocked("STORAGE_BINDING_UNAVAILABLE")
            _bindings[key] = attempt
            stale = _owned.get(key)
            if stale is not None and stale.runtime is runtime:
                if stale.creating or stale.retiring or stale.closer is not None:
                    raise StorageBlocked("STORAGE_BINDING_RETIRING")
                # The caller now owns this exact facade. Install its weak
                # binding before dropping the pool's strong ownership.
                _bound[key] = runtime
                del _owned[key]
                return
        if stale is not None:
            _retire(key, stale)
        with _lock:
            if not _available(runtime):
                raise StorageBlocked("STORAGE_BINDING_UNAVAILABLE")
            _bound[key] = runtime
    finally:
        with _lock:
            if _bindings.get(key) is attempt:
                del _bindings[key]


def unbind_storage(workspace: str | Path, runtime: StorageRuntime) -> None:
    key = _key(workspace)
    with _lock:
        if key in _bindings:
            raise StorageBlocked("STORAGE_BINDING_BUSY")
        if _bound.get(key) is runtime:
            _bound.pop(key, None)


def get_storage(workspace: str | Path) -> StorageRuntime:
    """Get the canonical project facade from the shared bounded ownership pool."""
    return _get_owned(_key(workspace), lambda: StorageRuntime(Path(workspace).resolve()))


def _get_owned(key: str, factory) -> StorageRuntime:
    """Get a usable facade, reserving bounded capacity before construction."""
    _require_process()
    entry = None
    try:
        while True:
            with _lock:
                if key in _bindings:
                    raise StorageBlocked("STORAGE_BINDING_BUSY")
                cached = _owned.get(key)
                if cached is not None:
                    if cached.creating:
                        raise StorageBlocked("STORAGE_BINDING_BUSY")
                    if cached.retiring:
                        raise StorageBlocked("STORAGE_BINDING_RETIRING")
                    if _available(cached.runtime):
                        _owned.move_to_end(key)
                        return cached.runtime
                    victim = (key, cached)
                else:
                    injected = _bound.get(key)
                    if injected is not None:
                        if not _available(injected):
                            raise StorageBlocked("STORAGE_BINDING_UNAVAILABLE")
                        return injected
                    if len(_owned) >= _MAX_OWNED_RUNTIMES:
                        victim = next(iter(_owned.items()))
                    else:
                        entry = _Entry()
                        _owned[key] = entry
                        break
            # A failed eviction does not mint a replacement or free capacity.
            _retire(*victim)
        entry.runtime = factory()
        with _lock:
            entry.creating = False
            if entry.retiring:
                raise StorageBlocked("STORAGE_BINDING_RETIRING")
            return entry.runtime
    except BaseException:
        if entry is None:
            raise
        with _lock:
            if entry.runtime is None:
                # Construction retains its existing Runtime cleanup contract;
                # no successfully returned facade was handed to this slot.
                if _owned.get(key) is entry:
                    del _owned[key]
            else:
                entry.creating = False
                entry.retiring = True
        raise


def release_storage(workspace: str | Path) -> None:
    """Close/retry the exact fallback; never close an injected facade."""
    key = _key(workspace)
    with _lock:
        if key in _bindings:
            raise StorageBlocked("STORAGE_BINDING_BUSY")
        entry = _owned.get(key)
    if entry is not None:
        _retire(key, entry)


def get_shared_knowledge_storage(state_home: str | Path, *, create: bool = False) -> StorageRuntime | None:
    """Use the same creation, capacity and confirmed-retirement protocol as projects."""
    _require_process()
    home = Path(state_home).resolve()
    key = "shared-knowledge:" + str(home).casefold()
    with _lock:
        known = key in _owned
    if not known and not create and not (home / "shared" / "knowledge" / "state.sqlite3").is_file():
        return None
    return _get_owned(key, lambda: StorageRuntime(home, state_home=home, shared_knowledge=True))



def close_owned_storage() -> None:
    """Close all current owned facades; retain and report incomplete cleanup."""
    _require_process()
    with _lock:
        entries = list(_owned.items())
        for _, entry in entries:
            entry.retiring = True
    errors = []
    for key, entry in entries:
        try:
            _retire(key, entry)
        except Exception as error:
            errors.append(error)
    if errors:
        raise errors[0]


def _close_at_exit() -> None:
    if os.getpid() != _pid:
        return
    try:
        close_owned_storage()
    except Exception:
        # Explicit shutdown callers receive the failure; interpreter exit can
        # only report it. Never erase retained entries to make shutdown green.
        sys.stderr.write("Gitgo storage cleanup incomplete at process exit\n")


atexit.register(_close_at_exit)
