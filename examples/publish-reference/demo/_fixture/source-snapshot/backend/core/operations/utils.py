# Reference extract from Gitgo c8e7906e6ed5e23af29ebdbc51e4bfafe696926b
from __future__ import annotations
import hashlib
import os
from pathlib import Path
from typing import Optional
from backend.adapters import FileAdapter, LocalFileAdapter
from .models import CommitInfo, FileEntry

def _match_glob(pattern: str, path: str) -> bool:
    import fnmatch
    if pattern.endswith('/'):
        pattern = pattern.rstrip('/')
    if pattern.startswith('/'):
        pattern = pattern[1:]
        return fnmatch.fnmatch(path, pattern)
    if pattern.startswith('**/'):
        inner = pattern[3:]
        parts = path.split('/')
        return any((fnmatch.fnmatch(p, inner) for p in parts))
    if pattern.endswith('/**'):
        prefix = pattern[:-3]
        return path.startswith(prefix) or fnmatch.fnmatch(path, pattern)
    if '/' in pattern:
        if fnmatch.fnmatch(path, pattern):
            return True
        normalized = path.replace('\\', '/')
        return normalized.startswith(pattern + '/') or normalized.startswith(pattern)
    parts = path.split('/')
    return any((fnmatch.fnmatch(p, pattern) for p in parts)) or fnmatch.fnmatch(path, pattern)
