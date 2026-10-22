"""Darwin descriptor-bound reads using kernel beneath/no-symlink resolution.

Requires a kernel enforcing O_RESOLVE_BENEATH (macOS 26 baseline). A real
escape probe rejects older kernels that silently ignore unknown open flags.
No path-walk/check-then-open fallback is provided.
"""
import errno
import os
import platform
import stat

from backend.core.sandbox_read import ObjectIdentity, ReadBoundaryError

O_RESOLVE_BENEATH = 0x00001000
O_UNIQUE = 0x00002000
O_EVTONLY = 0x00008000
O_NOFOLLOW_ANY = 0x20000000


def identity(fd):
    info = os.fstat(fd)
    return ObjectIdentity('darwin', info.st_dev, info.st_ino)


def _flags():
    if platform.system() != 'Darwin' or os.open not in os.supports_dir_fd:
        raise ReadBoundaryError('No stable read backend is available for this Darwin kernel')
    return os.O_CLOEXEC | os.O_NOFOLLOW | O_NOFOLLOW_ANY | os.O_NONBLOCK


def root(path):
    fd = os.open(path, _flags() | os.O_DIRECTORY | O_EVTONLY)
    try:
        # The positive probe alone would also pass on a kernel ignoring the
        # flag. Verify that escaping the actual acquired directory is denied.
        try:
            escaped = os.open('..', _flags() | os.O_DIRECTORY | O_EVTONLY | O_RESOLVE_BENEATH, dir_fd=fd)
        except OSError as error:
            if error.errno not in {getattr(errno, 'ENOTCAPABLE', 107), errno.EXDEV}:
                raise ReadBoundaryError('Darwin beneath lookup is unavailable') from error
        else:
            os.close(escaped)
            raise ReadBoundaryError('Darwin kernel does not enforce beneath lookup; macOS 26 or newer is required')
        within = os.open('.', _flags() | os.O_DIRECTORY | O_EVTONLY | O_RESOLVE_BENEATH, dir_fd=fd)
        try:
            if identity(within) != identity(fd):
                raise ReadBoundaryError('Darwin root identity changed during capability probe')
        finally:
            os.close(within)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_file(directory, name, owners, *, metadata_only):
    flags = _flags() | O_RESOLVE_BENEATH | O_UNIQUE
    # EVTONLY permits inspection without reading bytes or waiting on a FIFO.
    pinned = os.open(name, flags | O_EVTONLY, dir_fd=directory)
    owners.callback(os.close, pinned)
    info = os.fstat(pinned)
    if stat.S_ISDIR(info.st_mode):
        raise IsADirectoryError(name)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ReadBoundaryError('Read requires a singly linked regular file')
    if info.st_dev != os.fstat(directory).st_dev:
        raise ReadBoundaryError('Read cannot cross a filesystem boundary')
    selected = identity(pinned)
    if metadata_only:
        return None, selected
    # The second lookup has the same kernel containment rules. Verify its live
    # descriptor against the selected object before any byte read. It cannot
    # silently switch to a replacement inode or a newly mounted filesystem.
    readable = os.open(name, flags | os.O_RDONLY | os.O_NOCTTY, dir_fd=directory)
    owners.callback(os.close, readable)
    current = os.fstat(readable)
    if (identity(readable) != selected or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1):
        raise ReadBoundaryError('Selected Darwin object changed before readable acquisition')
    return readable, selected
