"""Rust owns the fixed Linux socket filter; bubblewrap installs it in the child.

The Host never installs a seccomp filter into itself. No Python construction or
unfiltered fallback remains. Resource ownership tokens are not authorization.
"""
from contextlib import contextmanager
import os
from pathlib import Path

from backend.core.native_sandbox import get_native
from backend.core.sandbox import SandboxDenied

_PENDING_FILTERS = {}


class _Filter:
    def __init__(self, library, response):
        self.library, self.token, self.fd = library, response['token'], response['fd']
        self.closed, self.failed = False, False
        _PENDING_FILTERS[id(self)] = self

    def fileno(self):
        if self.closed or self.failed:
            raise OSError('Native filter descriptor is unavailable')
        return self.fd

    def close(self):
        if self.closed:
            return
        if self.failed:
            raise OSError('Native filter cleanup is unconfirmed; no automatic replay')
        try:
            self.library.call(op='linux_filter_close', token=self.token)
        except BaseException:
            self.failed = True
            raise
        self.closed = True
        _PENDING_FILTERS.pop(id(self), None)


@contextmanager
def socket_filter(workspace: Path):
    try:
        if any(item.failed for item in _PENDING_FILTERS.values()):
            raise OSError('An earlier filter has unconfirmed cleanup')
        library = get_native()
        if library.call(op='capabilities').get('linux_filter_version') != 1:
            raise OSError('The matching Rust socket-filter boundary is required')
        program = _Filter(library, library.call(op='linux_filter_create', workspace=list(os.fsencode(workspace))))
    except (OSError, ValueError, RuntimeError) as exc:
        raise SandboxDenied('SANDBOX_UNAVAILABLE',
            f'Linux socket isolation is unavailable; trusted system libseccomp, Rust module '
            f'and a supported kernel are required. No unfiltered fallback: {exc}') from exc
    try:
        yield program
    finally:
        program.close()
