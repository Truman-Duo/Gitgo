"""Descriptor-bound reads inside an existing native sandbox.

This layer neither grants access nor pins an earlier approval. ReadRoot acquires
the directory object *now*. Callers must supply Host-selected roots and retain
OS isolation. Approval-time identity binding and write operations are separate.
"""
from __future__ import annotations

import ctypes
import os
import platform
import stat
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


class PinnedObject:
    """Metadata-only pin. It cannot read bytes and is never serialized."""
    def __init__(self, identity, owners):
        self.identity, self._owners = identity, owners

    def close(self):
        self._owners.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class PinnedRead:
    """Own the readable descriptor until close; never reopen the input name."""
    def __init__(self, fd: int, identity: ObjectIdentity, owners: ExitStack):
        self._fd = fd
        self.identity = identity
        self._owners = owners
        self._closed = False

    def read_bytes(self, *, max_bytes: int = MAX_READ_BYTES) -> bytes:
        if self._closed:
            raise ValueError('Read object is closed')
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_READ_BYTES:
            raise ValueError('Invalid bounded read size')
        before = os.fstat(self._fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ReadBoundaryError('Read requires a singly linked regular file')
        if before.st_size > max_bytes:
            raise ReadBoundaryError('File exceeds the bounded read size')
        output = bytearray()
        while len(output) <= max_bytes:
            chunk = os.read(self._fd, min(65536, max_bytes + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
        if len(output) > max_bytes:
            raise ReadBoundaryError('File grew beyond the bounded read size')
        after = os.fstat(self._fd)
        if (after.st_nlink != 1 or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ReadBoundaryError('File changed during the read')
        return bytes(output)

    def close(self):
        if not self._closed:
            self._closed = True
            self._owners.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ReadRoot:
    """Acquire a concrete directory, with no link traversal or fallback.

    A live root can open multiple files under that same object. Its identity is
    not an authorization token; no JSON-supplied identity is accepted here.
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
            else:
                raise ReadBoundaryError('No stable read backend is available')
        except BaseException:
            self.close()
            raise

    def pin(self, relative: str) -> PinnedObject:
        return self._open(relative, metadata_only=True)

    def open(self, relative: str) -> PinnedRead:
        return self._open(relative, metadata_only=False)

    def _open(self, relative: str, *, metadata_only: bool):
        if self._closed:
            raise ValueError('Read root is closed')
        parts = relative_parts(relative)
        owners = ExitStack()
        try:
            if sys.platform == 'linux':
                # O_PATH inspects even devices/FIFOs without opening them for
                # I/O. BENEATH + NO_XDEV + NO_SYMLINKS is enforced by the kernel
                # for the whole lookup, including concurrent rename races.
                pinned = _linux_openat2(self._handle, '/'.join(parts),
                    os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW, 0x08 | 0x01 | 0x04 | 0x02)
                owners.callback(os.close, pinned)
                info = os.fstat(pinned)
                if stat.S_ISDIR(info.st_mode):
                    raise IsADirectoryError(relative)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ReadBoundaryError('Read requires a singly linked regular file')
                identity = _linux_identity(pinned)
                if metadata_only:
                    return PinnedObject(identity, owners)
                # The native sandbox mounts its own procfs. Follow only our
                # retained descriptor, never the caller's path, after checking
                # type. This is not a general procfs access capability.
                fd = os.open(f'/proc/self/fd/{pinned}',
                             os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
                owners.callback(os.close, fd)
                if _linux_identity(fd) != identity:
                    raise ReadBoundaryError('Opened descriptor identity changed')
            else:
                import msvcrt
                parent = self._handle
                # Hold every intermediate directory without delete sharing;
                # all opens are relative to the preceding live handle.
                for part in parts[:-1]:
                    parent = self._windows.open(part, parent, directory=True)
                    owners.callback(self._windows.close, parent)
                    self._windows.identity(parent, directory=True)
                handle = self._windows.open(parts[-1], parent, directory=False, read_data=not metadata_only)
                try:
                    identity = self._windows.identity(handle, directory=False)
                    if metadata_only:
                        owners.callback(self._windows.close, handle)
                        return PinnedObject(identity, owners)
                    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                except BaseException:
                    self._windows.close(handle)
                    raise
                owners.callback(os.close, fd)  # CRT now owns the HANDLE.
            return PinnedRead(fd, identity, owners)
        except BaseException:
            owners.close()
            raise

    def close(self):
        if not self._closed:
            self._closed = True
            self._owners.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
