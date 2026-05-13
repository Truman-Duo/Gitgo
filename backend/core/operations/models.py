"""数据类 — FileEntry, CommitInfo"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class FileEntry:
    rel_path: str  # 相对工作区根目录的路径，使用 /
    status: str  # new | modified | same | renamed
    old_path: Optional[str] = None  # renamed 时记录旧路径
    workspace_hash: str = ""
    backup_hash: str = ""
    selected: bool = True


@dataclass
class CommitInfo:
    hash: str
    subject: str
    type: str  # feat/fix/docs/...
    scope: Optional[str]
    body: str = ""
