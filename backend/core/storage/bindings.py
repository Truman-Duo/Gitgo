"""Process-local storage binding for legacy static manager APIs.

The daemon injects its one project-owned :class:`StorageRuntime`.  Older
callers that only provide a workspace use a small bounded LRU of owned
runtimes, so History/Knowledge/Trace cannot silently open a new SQLite writer
for every read.  This module never exposes raw connections.
"""

from __future__ import annotations

import atexit
import threading
import weakref
from collections import OrderedDict
from pathlib import Path

from .runtime import StorageRuntime


_MAX_OWNED_RUNTIMES = 16
_lock = threading.RLock()
_bound: weakref.WeakValueDictionary[str, StorageRuntime] = (
    weakref.WeakValueDictionary()
)
_owned: OrderedDict[str, StorageRuntime] = OrderedDict()


def _key(workspace: str | Path) -> str:
    return str(Path(workspace).resolve()).casefold()


def bind_storage(workspace: str | Path, runtime: StorageRuntime) -> None:
    """Bind a caller-owned runtime as the only in-process project facade."""
    key = _key(workspace)
    stale: StorageRuntime | None = None
    with _lock:
        stale = _owned.pop(key, None)
        _bound[key] = runtime
    if stale is not None and stale is not runtime:
        stale.close()


def unbind_storage(workspace: str | Path, runtime: StorageRuntime) -> None:
    key = _key(workspace)
    with _lock:
        current = _bound.get(key)
        if current is runtime:
            _bound.pop(key, None)


def get_storage(workspace: str | Path) -> StorageRuntime:
    """Return the injected runtime or one bounded process-owned fallback."""
    key = _key(workspace)
    evicted: StorageRuntime | None = None
    with _lock:
        injected = _bound.get(key)
        if injected is not None and not getattr(injected, "_closed", False):
            return injected
        cached = _owned.get(key)
        if cached is not None and not getattr(cached, "_closed", False):
            _owned.move_to_end(key)
            return cached
        runtime = StorageRuntime(Path(workspace).resolve())
        _owned[key] = runtime
        _owned.move_to_end(key)
        if len(_owned) > _MAX_OWNED_RUNTIMES:
            _old_key, evicted = _owned.popitem(last=False)
    if evicted is not None:
        evicted.close()
    return runtime


def release_storage(workspace: str | Path) -> None:
    """Close a fallback runtime; injected daemon runtimes remain caller-owned."""
    key = _key(workspace)
    with _lock:
        runtime = _owned.pop(key, None)
    if runtime is not None:
        runtime.close()


def close_owned_storage() -> None:
    with _lock:
        runtimes = list(_owned.values())
        _owned.clear()
    for runtime in runtimes:
        try:
            runtime.close()
        except Exception:
            pass


atexit.register(close_owned_storage)

