"""Descriptor-bound reads inside an existing native sandbox.

This layer neither grants access nor pins an earlier approval. ReadRoot acquires
the directory object *now*. Callers must supply Host-selected roots and retain
OS isolation. Approval-time identity binding and write operations are separate.
"""
from __future__ import annotations

import ctypes
import os
import platform
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

MAX_READ_BYTES = 16 * 1024 * 1024


class ReadBoundaryError(PermissionError):
    """A required read capability or object invariant is unavailable."""


@dataclass(frozen=True)
class ObjectIdentity:
    platform: str
    volume: int
    file_id: int


def relative_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ReadBoundaryError('A nonempty relative file name is required')
    path = Path(value)
    if path.is_absolute() or path.anchor or '..' in path.parts:
        raise ReadBoundaryError('File name must stay below the acquired root')
    parts = path.parts
    if not parts or len(parts) > 256:
        raise ReadBoundaryError('A bounded file name below the root is required')
    if sys.platform == 'win32':
        reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}
        reserved.update(f'{kind}{n}' for kind in ('COM', 'LPT') for n in '123456789¹²³')
        for part in parts:
            if (part.endswith((' ', '.')) or any(c in part for c in ':*?"<>|')
                    or any(ord(c) < 32 for c in part)
                    or part.split('.')[0].upper() in reserved):
                raise ReadBoundaryError('Ambiguous Windows file name is not supported')
    return parts


def _linux_openat2(directory: int, name: str, flags: int, resolve: int) -> int:
    # Supported native Linux ABIs use syscall 437. Unknown ABIs fail closed.
    if platform.machine().lower() not in {'x86_64', 'amd64', 'aarch64', 'arm64'}:
        raise ReadBoundaryError('Stable reads require a supported Linux ABI')
    class OpenHow(ctypes.Structure):
        _fields_ = [('flags', ctypes.c_uint64), ('mode', ctypes.c_uint64),
                    ('resolve', ctypes.c_uint64)]
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.syscall
    call.restype = ctypes.c_long
    how = OpenHow(flags, 0, resolve)
    result = call(ctypes.c_long(437), ctypes.c_int(directory),
                  ctypes.c_char_p(os.fsencode(name)), ctypes.byref(how),
                  ctypes.c_size_t(ctypes.sizeof(how)))
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)
    return result


def _linux_identity(fd: int) -> ObjectIdentity:
    info = os.fstat(fd)
    return ObjectIdentity('linux', info.st_dev, info.st_ino)


class WriteRoot:
    """Legacy directory ownership for Publish new-file writes only.

    Exposes no content reader. Writing and Linux protected Git execution are
    separate from the stage 2 stable read migration.
    """
    def __init__(self, root: Path):
        root = Path(root)
        if not root.is_absolute() or '..' in root.parts or root == Path(root.anchor):
            raise ReadBoundaryError('A concrete absolute resource root is required')
        self._owners = ExitStack()
        self._closed = False
        try:
            if sys.platform == 'linux':
                self._handle = _linux_openat2(-100, str(root),
                    os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC, 0x04 | 0x02)
                self._owners.callback(os.close, self._handle)
                self.identity = _linux_identity(self._handle)
            elif sys.platform == 'win32':
                from backend.core.sandbox_read_windows import WindowsReadApi
                self._windows = WindowsReadApi()
                self._handle = self._windows.root(root)
                self._owners.callback(self._windows.close, self._handle)
                self.identity = self._windows.identity(self._handle, directory=True)
            elif sys.platform == 'darwin':
                from backend.core import sandbox_read_darwin as darwin
                self._handle = darwin.root(root)
                self._owners.callback(os.close, self._handle)
                self.identity = darwin.identity(self._handle)
            else:
                raise ReadBoundaryError('No stable read backend is available')
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self._closed:
            self._closed = True
            self._owners.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ReadRoot:
    """Stable object reads use Rust; no legacy fallback on load or kernel failure."""
    def __init__(self, root: Path):
        if sys.platform not in {'linux', 'win32', 'darwin'}:
            raise ReadBoundaryError('No stable read backend is available')
        from backend.core.sandbox_read_native import NativeReadRoot
        self._native = NativeReadRoot(root)
        self.identity = self._native.identity

    def pin(self, relative: str):
        return self._native.pin(relative)

    def open(self, relative: str):
        return self._native.open(relative)

    def _linux_directory_descriptor(self):
        return self._native._linux_directory_descriptor()

    def close(self):
        self._native.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
