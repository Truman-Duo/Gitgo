"""POSIX prototype persistence: no-follow control files and retained flock inode."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import uuid

from .tree import encode
from .validation import json_object, reject


MAX_CONTROL_BYTES = 256 * 1024


def filesystem_identity(path: Path) -> dict:
    info = path.lstat()
    return {"device": info.st_dev, "inode": info.st_ino}


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_control(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
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
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        # Only the exact random file created by this call. A hard kill can
        # leave it behind; readers NEVER select a .record-* as the ledger.
        if temporary.exists():
            temporary.unlink()


@contextmanager
def lock_file(path: Path, *, shared: bool = False, expected_identity: dict | None = None):
    if os.name != "posix":
        reject("UPDATE_TRANSACTION_PLATFORM", "Transaction ownership adapter is unavailable")
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
            reject("UPDATE_LOCK_INVALID", "Installation lock ownership is invalid")
        if expected_identity is not None and expected_identity != {"device": info.st_dev, "inode": info.st_ino}:
            reject("UPDATE_LOCK_INVALID", "Installation lock was replaced after registration")
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            reject("UPDATE_BUSY", "Installation has an active operation or runtime")
        # Do not unlink/recreate a flock file: two lock inodes would split authority.
        yield fd
    finally:
        os.close(fd)
