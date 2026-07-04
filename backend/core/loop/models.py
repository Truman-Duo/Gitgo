"""Agent Process data model."""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.loop.tools import ToolRegistry


class ProcessStatus(Enum):
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    KILLED = "killed"
    ORPHANED = "orphaned"


class RingLevel(Enum):
    RING_0 = 0  # 治理权限：sync/push/accept/promote_lesson
    RING_3 = 3  # 执行权限：只能调本进程 tool_registry 里的工具


@dataclass
class AgentProcess:
    process_id: str          # UUID
    role: str                # "planner" | "executor" | "reviewer" | "reporter"
    ring_level: RingLevel
    tool_registry: "ToolRegistry | None" = None
    max_steps: int = 50
    steps_used: int = 0               # ToolDispatcher 每步 +1
    status: ProcessStatus = ProcessStatus.RUNNING
    parent_id: str | None = None      # 谁 fork 的
    result: dict | None = None        # wait 之后的产出
    context_snapshot: dict | None = None  # C3: A 级 Agent 冻结的治理简报
    created_at: str = ""
