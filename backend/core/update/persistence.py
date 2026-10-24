"""POSIX prototype persistence: no-follow control files and retained flock inode."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import uuid

from .tree import encode
from .validation import json_object, reject, number, text


MAX_CONTROL_BYTES = 256 * 1024


def _darwin() -> bool:
    # Use the real kernel, not product-target declarations or environment.
    return os.name == "posix" and os.uname().sysname == "Darwin"


def _volume_uuid(fd: int) -> str:
    """Darwin ATTR_VOL_UUID from the SAME opened object used for inode checks.

    st_dev is a mount's temporary device number and may change after reboot.
    Never infer volume identity from a path, cached device number or mount name.
    SDK sys/attr.h specifies attrlist, ATTR_VOL_INFO and ATTR_VOL_UUID; the
    returned buffer is a uint32 length followed by the full 16-byte UUID.
    """
    import ctypes
    import struct
    class AttrList(ctypes.Structure):
        _fields_ = [("bitmapcount", ctypes.c_uint16), ("reserved", ctypes.c_uint16),
                    *[(name, ctypes.c_uint32) for name in ("common", "volume", "directory", "file", "fork")]]
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        query = libc.fgetattrlist
        query.argtypes = [ctypes.c_int, ctypes.POINTER(AttrList), ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong]
        query.restype = ctypes.c_int
        attrs = AttrList(5, 0, 0, 0x80040000, 0, 0, 0)
        result = ctypes.create_string_buffer(20)
        if query(fd, ctypes.byref(attrs), result, 20, 0) != 0 or struct.unpack("=I", result.raw[:4])[0] != 20:
            raise ValueError("Volume identity query failed")
        value = uuid.UUID(bytes=result.raw[4:20])
        if value.int == 0: raise ValueError("Volume UUID unavailable")
        return "darwin-volume:" + str(value)
    except (AttributeError, OSError, ValueError):
        reject("UPDATE_VOLUME_ID_UNAVAILABLE", "Stable filesystem identity is unavailable")


def validate_device(value) -> None:
    if os.name == "nt":
        text(value, r"windows-volume:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", 51)
        if value == "windows-volume:" + str(uuid.UUID(int=0)):
            reject("UPDATE_CONTROL_INVALID", "Filesystem GUID must be nonzero")
        return
    if _darwin():
        text(value, r"darwin-volume:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", 50)
        if value == "darwin-volume:" + str(uuid.UUID(int=0)):
            reject("UPDATE_CONTROL_INVALID", "Filesystem UUID must be nonzero")
    else:
        number(value, minimum=0, maximum=2**64 - 1)


def descriptor_identity(fd: int) -> dict:
    if os.name == "nt":
        from .windows_storage import descriptor_identity as windows_identity
        return windows_identity(fd)
    info = os.fstat(fd)
    return {"device": _volume_uuid(fd) if _darwin() else info.st_dev, "inode": info.st_ino}


def filesystem_identity(path: Path) -> dict:
    if os.name == "nt":
        from .windows_storage import filesystem_identity as windows_identity
        return windows_identity(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    try: return descriptor_identity(fd)
    finally: os.close(fd)


def sync_directory(path: Path) -> None:
    if os.name == "nt":
        # No POSIX directory fsync on Windows. NTFS replacements below use
        # WRITE_THROUGH; verify the directory boundary instead of claiming a
        # directory FlushFileBuffers succeeded. Physical power loss is separate.
        from .windows_storage import opened
        with opened(path): pass
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_control(path: Path) -> dict:
    fd = open_readonly(path)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600))
                or info.st_size > MAX_CONTROL_BYTES):
            reject("UPDATE_CONTROL_INVALID", "Control file ownership, type or size is invalid")
        return json_object(handle.read(MAX_CONTROL_BYTES + 1), MAX_CONTROL_BYTES)


def atomic_control(path: Path, value: dict) -> None:
    data = encode(value)
    if len(data) > MAX_CONTROL_BYTES:
        reject("UPDATE_CONTROL_INVALID", "Control record exceeds its size limit")
    if path.exists() or path.is_symlink():
        read_control(path)
    temporary = path.parent / (".record-" + uuid.uuid4().hex)
    if os.name == "nt":
        from .windows_storage import open_file
        fd = open_file(temporary, write=True, create=True)
    else:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            from .windows_storage import move
            move(temporary, path, replace=True)
        else: os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        # Only the exact random file created by this call. A hard kill can
        # leave it behind; readers NEVER select a .record-* as the ledger.
        if temporary.exists():
            temporary.unlink()


@contextmanager
def lock_file(path: Path, *, shared: bool = False, expected_identity: dict | None = None):
    if os.name == "nt" and hasattr(os, "open"):
        from .windows_storage import supported, lock_file as windows_lock
        if supported():
            with windows_lock(path, shared=shared, expected_identity=expected_identity) as fd: yield fd
            return
    if os.name != "posix":
        reject("UPDATE_TRANSACTION_PLATFORM", "Transaction ownership adapter is unavailable")
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
            reject("UPDATE_LOCK_INVALID", "Installation lock ownership is invalid")
        if expected_identity is not None and expected_identity != descriptor_identity(fd):
            reject("UPDATE_LOCK_INVALID", "Installation lock was replaced after registration")
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            reject("UPDATE_BUSY", "Installation has an active operation or runtime")
        # Do not unlink/recreate a flock file: two lock inodes would split authority.
        yield fd
    finally:
        os.close(fd)


def open_readonly(path):
    if os.name == "nt":
        from .windows_storage import open_file
        return open_file(path)
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
