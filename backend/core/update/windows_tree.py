"""Bounded Windows inventory with pinned no-delete directory handles.

Reject every reparse point, hard link, alternate data stream and case collision.
Windows READONLY is not a Unix execution/ownership bit; canonical modes here
represent only the schema's portable file/directory kinds, not ACL evidence.
ACLs are verified separately on each opened node.
"""
import hashlib
import os
from pathlib import Path
import time

from .archive import ExtractionLimits, _Entry, _parts, _validate
from .validation import reject
from .windows_storage import opened, handle_identity, open_file, reject_alternate_streams


def inventory(root):
    limits = ExtractionLimits()
    started = time.monotonic()
    records, entries = [], []
    total = 0
    def checkpoint():
        if time.monotonic() - started >= limits.seconds:
            reject("UPDATE_TREE_INVALID", "Windows inventory exceeded its deadline")
    def walk(path, prefix):
        nonlocal total
        # Keep this directory unrenamable while resolving its children.
        with opened(path, share=3, program=True) as directory:
            reject_alternate_streams(path)
            root_id = handle_identity(directory)
            with os.scandir(path) as children:
                for child in children:
                    checkpoint()
                    if len(records) >= limits.entries:
                        reject("UPDATE_TREE_INVALID", "Windows payload has too many nodes")
                    name = prefix + child.name
                    _parts("payload/" + name)
                    target = path / child.name
                    with opened(target, share=1, program=True) as node:
                        reject_alternate_streams(target)
                        identity = handle_identity(node)
                        if identity["device"] != root_id["device"]:
                            reject("UPDATE_TREE_INVALID", "Windows payload crossed volumes")
                        info = target.stat(follow_symlinks=False)
                        if target.is_dir():
                            record = {"path": name, "kind": "directory", "mode": 0o700}
                            size = 0
                        elif target.is_file() and info.st_nlink == 1:
                            size = info.st_size; total += size
                            if size > limits.file_bytes or total > limits.total_bytes:
                                reject("UPDATE_TREE_INVALID", "Windows payload exceeds size bounds")
                            digest = hashlib.sha256()
                            fd = open_file(target, program=True)
                            with os.fdopen(fd, "rb") as handle:
                                count = 0
                                while chunk := handle.read(64 * 1024):
                                    checkpoint(); count += len(chunk)
                                    if count > size: reject("UPDATE_TREE_INVALID", "Windows payload changed")
                                    digest.update(chunk)
                            if count != size: reject("UPDATE_TREE_INVALID", "Windows payload changed")
                            record = {"path": name, "kind": "file", "mode": 0o600,
                                      "bytes": size, "sha256": digest.hexdigest()}
                        else: reject("UPDATE_TREE_INVALID", "Windows payload contains a hard link or special node")
                        records.append(record)
                        entries.append(_Entry("payload/" + name, record["kind"], size, False, None))
                        if record["kind"] == "directory": walk(target, name + "/")
            if handle_identity(directory) != root_id:
                reject("UPDATE_TREE_INVALID", "Windows inventory root changed")
    walk(root, "")
    _validate(entries, limits, checkpoint, wrapped=True)
    return sorted(records, key=lambda item: item["path"])
