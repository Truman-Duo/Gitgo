"""Read-only local observations using the existing descriptor boundary.

No write capability, executable content, or authority is supplied by this adapter.
Bounds are explicit failures, not silently truncated successful observations.
"""
from pathlib import Path
from types import SimpleNamespace
import os
import stat
from backend.core.sandbox_read import ReadRoot, relative_parts


class BoundedLocalReader:
    def __init__(self, root, *, max_file_bytes=2*1024*1024, max_total_bytes=64*1024*1024,
                 max_nodes=8192, ignored=()):
        self.root = Path(root).absolute()
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_nodes = max_nodes
        self.ignored = {p.casefold() for p in ignored}
        self.cache = {}
        self.used = 0
        self.failures = []

    def read_bytes(self, path):
        parts = relative_parts(path)
        if any(part.casefold() in {".git", ".gitgo"} for part in parts):
            raise PermissionError("Business observations cannot select product/repository metadata")
        if path not in self.cache:
            try:
                with ReadRoot(self.root) as root:
                    with root.open(path) as opened:
                        raw = opened.read_bytes(max_bytes=self.max_file_bytes)
                self.used += len(raw)
                if self.used > self.max_total_bytes:
                    raise PermissionError("Observation exceeds its total byte bound")
                self.cache[path] = raw
            except OSError:
                self.failures.append(path)
                raise
        return self.cache[path]

    def read_text(self, path, encoding="utf8"):
        return self.read_bytes(path).decode(encoding, errors="replace")

    def exists(self, path):
        parts = relative_parts(path)
        if any(part.casefold() in {".git", ".gitgo"} for part in parts):
            raise PermissionError("Business observations cannot select product/repository metadata")
        try:
            with ReadRoot(self.root) as root:
                with root.pin(path):
                    return True
        except FileNotFoundError:
            return False

    def stat(self, path):
        return SimpleNamespace(st_size=len(self.read_bytes(path)))

    def walk(self, top=""):
        if top:
            relative_parts(top)
        count = 0
        def visit(relative):
            nonlocal count
            directory = self.root / relative
            # Retain directory objects while enumerating and recursing.
            with ReadRoot(directory):
                dirs, files = [], []
                with os.scandir(directory) as entries:
                    for entry in entries:
                        count += 1
                        if count > self.max_nodes:
                            raise PermissionError("Observation exceeds its directory-entry bound")
                        if entry.name.casefold() in self.ignored:
                            continue
                        name = str(Path(relative) / entry.name).replace("\\", "/")
                        relative_parts(name)
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                            raise PermissionError("Observation cannot traverse links or reparse points")
                        if stat.S_ISDIR(info.st_mode):
                            dirs.append(entry.name)
                        elif stat.S_ISREG(info.st_mode):
                            files.append(entry.name)
                        else:
                            raise PermissionError("Observation requires ordinary files")
                dirs.sort(); files.sort()
                yield relative or ".", dirs, files
                for name in dirs:
                    yield from visit(str(Path(relative) / name).replace("\\", "/"))
        yield from visit(top)
