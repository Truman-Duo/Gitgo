"""RepoNode 数据模型 — Phase 1 核心。

三角色模型：workspace（工程版）、release（正式版）、trial（试用版）
各角色均为 RepoNode 实例，通过 FileAccess 抽象底层存储访问。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FileAccessKind(Enum):
    """文件访问方式"""
    LOCAL = "local"
    SSH = "ssh"
    SMB = "smb"


class SyncStatus(Enum):
    """节点同步状态"""
    MISSING = "missing"   # 路径未配置
    EMPTY = "empty"       # 路径已配置但无效
    VALID = "valid"       # 有效


class TrialAction(Enum):
    """Trial 三叉决策结果"""
    PENDING = "pending"
    ACCEPTED = "accepted"
    PROMOTED = "promoted"
    DISCARDED = "discarded"


@dataclass
class IncomingChange:
    """Trial 仓库的新提交数据模型"""
    hash: str = ""
    message: str = ""
    author: str = ""
    timestamp: str = ""
    body: str = ""
    triage: TrialAction = TrialAction.PENDING


@dataclass
class RemoteTarget:
    """远程仓库目标（Phase 5 完整实现）"""
    url: str = ""
    name: str = "origin"
    kind: str = ""  # "github" | "gitlab" | "bare" | ""

    @classmethod
    def from_dict(cls, d: dict | None) -> RemoteTarget:
        if not d:
            return cls()
        return cls(url=d.get("url", ""), name=d.get("name", "origin"),
                   kind=d.get("kind", ""))


@dataclass
class FileAccess:
    """文件访问描述

    LOCAL: path 为本地路径
    SSH (Phase 3): host/port/username/key_path
    SMB (Phase 6): share
    """
    kind: FileAccessKind = FileAccessKind.LOCAL
    path: str = ""
    # SSH
    host: str = ""
    port: int = 22
    username: str = ""
    key_path: str = ""
    # SMB
    share: str = ""

    @classmethod
    def from_dict(cls, d: dict | None) -> FileAccess:
        if not d:
            return cls()
        kind_str = d.get("kind", "local")
        kind = FileAccessKind(kind_str) if kind_str in (
            "local", "ssh", "smb") else FileAccessKind.LOCAL
        return cls(
            kind=kind,
            path=d.get("path", ""),
            host=d.get("host", ""),
            port=d.get("port", 22),
            username=d.get("username", ""),
            key_path=d.get("key_path", ""),
            share=d.get("share", ""),
        )


@dataclass
class RepoNode:
    """仓库节点 — 代表三角色中的一个角色"""
    file_access: FileAccess = field(default_factory=FileAccess)
    remote: Optional[RemoteTarget] = None
    last_known_head: str = ""  # 最近已知 HEAD（替代 sync_base）

    @classmethod
    def from_dict(cls, d: dict | None) -> RepoNode | None:
        if d is None:
            return None
        return cls(
            file_access=FileAccess.from_dict(d.get("file_access")),
            remote=RemoteTarget.from_dict(d.get("remote")),
            last_known_head=d.get("last_known_head", ""),
        )
