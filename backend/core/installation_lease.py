"""Process-local retained installation descriptors, not a new state authority.

Only a validated installation gate registers descriptors. No environment value
alone can register one. Owned child creation preserves the lease across exec;
the registration/ledger authority remains in backend.core.update.
"""
from contextlib import contextmanager
import os
import threading

_guard = threading.RLock()
_descriptors: dict[int, int] = {}


@contextmanager
def retain_descriptor(descriptor: int):
    if os.name != "posix" or type(descriptor) is not int or descriptor < 3:
        raise ValueError("Invalid installation lease descriptor")
    os.fstat(descriptor)
    with _guard:
        _descriptors[descriptor] = _descriptors.get(descriptor, 0) + 1
    try:
        yield
    finally:
        with _guard:
            count = _descriptors[descriptor] - 1
            if count: _descriptors[descriptor] = count
            else: del _descriptors[descriptor]


def runtime_descriptors() -> tuple[int, ...]:
    with _guard:
        return tuple(sorted(_descriptors))


def child_options() -> dict:
    descriptors = runtime_descriptors()
    return {"pass_fds": descriptors} if descriptors else {}
