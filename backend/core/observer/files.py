"""Private developer-owned capture directories, never project storage."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat


def private_directory(path: Path) -> Path:
    if not path.is_absolute() or ".gitgo" in (p.lower() for p in path.parts):
        raise ValueError("OBSERVER_DIRECTORY_INVALID: use an absolute developer directory outside .gitgo")
    # Reject existing redirected components before resolving, including Windows junctions.
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("OBSERVER_DIRECTORY_REDIRECTED")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            # Reuse the OS-only ACL implementation; no update protocol or service is involved.
            from backend.core.update.windows_storage import create_private_directory
            create_private_directory(path)
        else:
            path.mkdir(mode=0o700)
    if os.name == "nt":
        from backend.core.update.windows_storage import opened
        with opened(path):
            pass
    else:
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("OBSERVER_DIRECTORY_NOT_PRIVATE")
    return path


def read_private_json(path: Path) -> dict:
    if not path.parent.is_dir():
        raise FileNotFoundError("OBSERVER_DESCRIPTOR_MISSING")
    private_directory(path.parent)
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("OBSERVER_DESCRIPTOR_INVALID")
    if os.name == "nt":
        from backend.core.update.windows_storage import open_file
        fd = open_file(path)
    else:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise ValueError("OBSERVER_DESCRIPTOR_NOT_PRIVATE")
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("OBSERVER_DESCRIPTOR_INVALID")
    return value


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    # Parent is private. Never follow a pre-existing temporary file.
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)

