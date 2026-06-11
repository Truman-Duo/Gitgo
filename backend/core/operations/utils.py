"""工具函数 — 哈希 / 二进制检测 / glob 匹配 / 序列化"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

from backend.adapters import FileAdapter, LocalFileAdapter

from .models import CommitInfo, FileEntry


def _hash_file(filepath: str | Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(8192 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _is_binary(filepath: str | Path) -> bool:
    try:
        with open(filepath, "rb") as f:
            chunk = f.read(1024)
            return b"\0" in chunk
    except OSError:
        return True


def _normalize_path(path: str) -> str:
    return path.replace(os.sep, "/")


def _read_gitignore(
    workspace: Path = Path(),
    *,
    file_adapter: FileAdapter | None = None,
) -> list[str]:
    if file_adapter is None:
        file_adapter = LocalFileAdapter(workspace)
    if file_adapter.exists(".gitignore"):
        return [
            line.strip()
            for line in file_adapter.read_text(".gitignore").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    return []


def _match_glob(pattern: str, path: str) -> bool:
    import fnmatch

    if pattern.endswith("/"):
        pattern = pattern.rstrip("/")

    if pattern.startswith("/"):
        pattern = pattern[1:]
        return fnmatch.fnmatch(path, pattern)

    if pattern.startswith("**/"):
        inner = pattern[3:]
        parts = path.split("/")
        return any(fnmatch.fnmatch(p, inner) for p in parts)

    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path.startswith(prefix) or fnmatch.fnmatch(path, pattern)

    # 如果模式含 /，表示是一个目录前缀 → 匹配路径开头
    if "/" in pattern:
        if fnmatch.fnmatch(path, pattern):
            return True
        # 也匹配以该目录开头的所有子路径
        normalized = path.replace("\\", "/")
        return normalized.startswith(pattern + "/") or normalized.startswith(pattern)

    parts = path.split("/")
    return any(fnmatch.fnmatch(p, pattern) for p in parts) or fnmatch.fnmatch(path, pattern)


def _is_excluded(rel_path: str, patterns: list[str]) -> bool:
    return any(_match_glob(p, rel_path) for p in patterns)


def _entry_to_dict(e: FileEntry) -> dict:
    return {
        "rel_path": e.rel_path,
        "status": e.status,
        "old_path": e.old_path,
        "workspace_hash": e.workspace_hash,
        "backup_hash": e.backup_hash,
        "selected": e.selected,
    }


def _commit_to_dict(c: CommitInfo) -> dict:
    return {
        "hash": c.hash,
        "subject": c.subject,
        "type": c.type,
        "scope": c.scope,
        "body": c.body,
    }
