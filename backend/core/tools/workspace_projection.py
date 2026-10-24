"""Bounded raw-byte execution views, separate from the formal workspace.

Only ordinary business files are projected. Git metadata and Gitgo-owned state
are not execution inputs; unsupported types/capacity fail before native launch.
This is a cooperating-writer snapshot, not an OS filesystem snapshot.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat

from backend.core.sandbox_read import ReadRoot, relative_parts
from .file_mutation import FileMutation
from .file_operations import PROTECTED_METADATA_PARTS
from .file_mutation_batch import FileMutationBatch

MAX_FILES = 4096
MAX_NODES = 8192
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_CANDIDATE_BYTES = 64 * 1024 * 1024
_RESERVED = PROTECTED_METADATA_PARTS


@dataclass(frozen=True)
class WorkspaceImage:
    # Immutable tuples prevent a producer from changing its source baseline.
    files: tuple[tuple[str, bytes, int], ...]
    directories: tuple[str, ...]

    @property
    def versions(self):
        return {name: (hashlib.sha256(raw).hexdigest(), mode) for name, raw, mode in self.files}

    def materialize(self, target):
        target = Path(target)
        if any(target.iterdir()):
            raise ValueError("Execution view must start empty")
        for name in self.directories:
            (target / name).mkdir(parents=True, exist_ok=True)
        for name, raw, mode in self.files:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            path.chmod(mode)


def capture(root, *, output=False):
    root = Path(root).resolve(strict=True)
    files, directories = [], []
    used = nodes = 0
    with ReadRoot(root) as acquired:
        stack = [(root, "")]
        while stack:
            directory, prefix = stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    nodes += 1
                    if nodes > MAX_NODES:
                        raise ValueError("Workspace view exceeds 8192 entries")
                    name = prefix + entry.name
                    relative_parts(name)
                    if entry.name.casefold() in _RESERVED:
                        if output:
                            # The native runner owns its private home/temp tree.
                            # No other Gitgo state may be published as business data.
                            if not prefix and entry.name == ".gitgo" and entry.is_dir(follow_symlinks=False):
                                if any(p.name != "sandbox" for p in Path(entry.path).iterdir()):
                                    raise ValueError("Native output contains protected Gitgo state")
                                continue
                            raise ValueError("Native output contains protected .git/.gitgo metadata")
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if (stat.S_ISLNK(info.st_mode) or
                            getattr(info, "st_file_attributes", 0) & 0x400):
                        raise ValueError("Workspace views do not support links or reparse points: " + name)
                    if stat.S_ISDIR(info.st_mode):
                        directories.append(name)
                        stack.append((Path(entry.path), name + "/"))
                    elif stat.S_ISREG(info.st_mode):
                        if len(files) >= MAX_FILES:
                            raise ValueError("Workspace view exceeds 4096 files")
                        with acquired.open(name) as opened:
                            raw = opened.read_bytes(max_bytes=MAX_FILE_BYTES)
                        used += len(raw)
                        if used > MAX_TOTAL_BYTES:
                            raise ValueError("Workspace view exceeds 128 MiB")
                        files.append((name, raw, stat.S_IMODE(info.st_mode)))
                    else:
                        raise ValueError("Workspace views require ordinary files: " + name)
    return WorkspaceImage(tuple(sorted(files)), tuple(sorted(directories)))


def current_matches(root, before):
    current = capture(root)
    return current.versions == before.versions and current.directories == before.directories


def project(root, before, after, *, operation):
    root = Path(root).resolve()
    old = {name: (raw, mode) for name, raw, mode in before.files}
    new = {name: (raw, mode) for name, raw, mode in after.files}
    members, used = [], 0
    for name in sorted(old.keys() | new.keys()):
        previous, following = old.get(name), new.get(name)
        if previous == following:
            continue
        if previous and following and previous[1] != following[1]:
            raise ValueError("Native file mode changes require a richer file service: " + name)
        if following and os.name != "nt" and following[1] & 0o111:
            raise ValueError("Executable outputs require a richer file service: " + name)
        first, last = previous[0] if previous else b"", following[0] if following else b""
        used += len(first) + len(last)
        if used > MAX_CANDIDATE_BYTES:
            raise ValueError("Native before/after candidates exceed 64 MiB")
        view = b"\0" not in first + last
        try:
            first_text, last_text = first.decode("utf-8"), last.decode("utf-8")
        except UnicodeError:
            view, first_text, last_text = False, "", ""
        members.append(FileMutation("write_file" if following else "delete_file", root, root / name,
            hashlib.sha256(first).hexdigest() if previous else "", first_text if view else "",
            last_text if view else "", after_bytes=last, text_view_available=view))
    if len(members) > 128:
        raise ValueError("Native output exceeds the 128-file candidate limit")
    # Empty directory creation/deletion has no FileMutation representation.
    added = set(after.directories) - set(before.directories)
    if any(not any(name.startswith(directory + "/") for name in new) for directory in added):
        raise ValueError("Empty directory outputs require a richer file service")
    deleted = set(before.directories) - set(after.directories)
    if deleted:
        raise ValueError("Directory deletion requires a richer file service")
    return FileMutationBatch(root, tuple(members), operation=operation)
