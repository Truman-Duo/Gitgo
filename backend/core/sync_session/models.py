"""SyncSession 数据模型 — SessionStage 枚举 + FormalCommit。

零 backend import，避免循环依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class SessionStage(Enum):
    IDLE = auto()
    TRIAL_CHECKING = auto()
    TRIAL_REVIEWING = auto()
    INCOMING_CONFIRMING = auto()
    SCANNING = auto()
    SELECTING = auto()
    COMMITTING = auto()
    SYNCING = auto()
    PUSHING = auto()
    FAILED = auto()


@dataclass
class FormalCommit:
    message: str
    number: int
    prefix: str
    synced: bool = False
    pushed: bool = False
    is_incoming: bool = False
    sources_cleared: bool = False
    created_at: str = ""
    source_indices: set[int] = field(default_factory=set)
