"""Read-only bounded payload identities, including modes, directories and links."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time

from .archive import ExtractionLimits, _Entry, _parts, _validate
from .transport import CHUNK_BYTES
from .validation import reject


def encode(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def inventory(root: Path) -> list[dict]:
    if root.is_symlink() or not root.is_dir():
        reject("UPDATE_TREE_INVALID", "Payload root must be a real directory")
    limits = ExtractionLimits()
    started = time.monotonic()

    def checkpoint():
        if time.monotonic() - started >= limits.seconds:
            reject("UPDATE_TREE_INVALID", "Payload inspection exceeded its deadline")

    records, entries = [], []
    total = 0

    def walk(directory_fd, prefix):
        checkpoint()
        # scandir is lazy; do not load an unbounded directory listing first.
        # Descendant opens are fd-relative and no-follow, so a moved directory
        # or a symlink substitution cannot redirect reads into protected data.
        with os.scandir(directory_fd) as children:
            for child in children:
                inspect(directory_fd, prefix, child.name)

    def inspect(directory_fd, prefix, leaf):
        nonlocal total
        checkpoint()
        if len(records) >= limits.entries:
            reject("UPDATE_TREE_INVALID", "Payload contains too many entries")
        name = prefix + leaf
        _parts("payload/" + name)
        info = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
        mode = stat.S_IMODE(info.st_mode)
        if info.st_uid != os.getuid() or (not stat.S_ISLNK(info.st_mode) and mode & 0o6022):
            reject("UPDATE_TREE_INVALID", "Payload has unsafe ownership or permissions")
        record = {"path": name, "mode": mode}
        if stat.S_ISLNK(info.st_mode):
            kind, size, link = "symlink", 0, os.readlink(leaf, dir_fd=directory_fd)
            record.update(kind=kind, link=link)
        elif stat.S_ISDIR(info.st_mode):
            kind, size, link = "directory", 0, ""
            record.update(kind=kind)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            kind, size, link = "file", info.st_size, ""
            total += size
            if size > limits.file_bytes or total > limits.total_bytes:
                reject("UPDATE_TREE_INVALID", "Payload size exceeds inspection limits")
            digest = hashlib.sha256()
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    reject("UPDATE_TREE_INVALID", "Payload file identity changed")
                received = 0
                while chunk := handle.read(CHUNK_BYTES):
                    checkpoint()
                    received += len(chunk)
                    if received > size:
                        reject("UPDATE_TREE_INVALID", "Payload changed during inspection")
                    digest.update(chunk)
                if received != size:
                    reject("UPDATE_TREE_INVALID", "Payload changed during inspection")
            record.update(kind=kind, bytes=size, sha256=digest.hexdigest())
        else:
            reject("UPDATE_TREE_INVALID", "Payload contains a hard link or special file")
        records.append(record)
        # A virtual wrapper lets the archive validator check Unix links
        # against this payload root, including legitimate '..' within it.
        entries.append(_Entry("payload/" + name, kind, size, False, None, link))
        if kind == "directory":
            fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    reject("UPDATE_TREE_INVALID", "Payload directory identity changed")
                walk(fd, name + "/")
            finally:
                os.close(fd)

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o6022:
            reject("UPDATE_TREE_INVALID", "Payload root ownership or permissions are unsafe")
        walk(fd, "")
    finally:
        os.close(fd)
    _validate(entries, limits, checkpoint, wrapped=True)
    return sorted(records, key=lambda r: r["path"])


def tree_digest(root: Path) -> str:
    # Root mode matters too: changing a directory into a more permissive one
    # must not silently keep its installation identity.
    return hashlib.sha256(encode({"mode": stat.S_IMODE(root.stat().st_mode),
                                  "entries": inventory(root)})).hexdigest()


def sync_tree(root: Path) -> None:
    from .persistence import sync_directory
    records = inventory(root)
    for record in records:
        if record["kind"] == "file":
            fd = os.open(root / record["path"], os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    directories = [root / r["path"] for r in records if r["kind"] == "directory"]
    for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        sync_directory(path)
    sync_directory(root)
