"""Invocation-scoped runtime dependencies; no grant or new state authority."""
from contextvars import ContextVar
from contextlib import contextmanager

_CANCEL = ContextVar("tool_service_cancel", default=None)
_RUNTIME = ContextVar("tool_service_runtime", default=None)


@contextmanager
def tool_service_scope(cancel_check, runtime=None):
    first, second = _CANCEL.set(cancel_check), _RUNTIME.set(runtime)
    try:
        yield
    finally:
        _RUNTIME.reset(second)
        _CANCEL.reset(first)


def service_runtime():
    return _RUNTIME.get()


def service_cancelled():
    check = _CANCEL.get()
    return bool(check and check())
