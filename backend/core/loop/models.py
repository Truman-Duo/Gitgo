"""Agent Process data model."""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.session import AgentSession


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
    cancel_requested: bool = False      # kill() 置位，agent_step 每步检查以真停线程
    parent_id: str | None = None      # 谁 fork 的
    result: dict | None = None        # wait 之后的产出
    context_snapshot: dict | None = None  # C3: A 级 Agent 冻结的治理简报
    task_description: str = ""            # 当前 task 描述
    task_constraints: list[str] = field(default_factory=list)  # v0.36: 中途约束
    created_at: str = ""
    worktree_path: str = ""            # 挂载的工作区路径（B 级 Agent）
    provider_id: str = ""              # LLM provider id
    model_id: str = ""                 # LLM model id
    session: Any = field(default=None, repr=False)   # AgentSession, 避免循环 import
    _step_history: list[dict] = field(default_factory=list, repr=False)
    _nudge_counters: dict[str, int] = field(default_factory=dict, repr=False)
    _transcript_builder: Any = field(default=None, repr=False)  # v0.39: TaskTranscriptBuilder
