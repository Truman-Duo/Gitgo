"""ExecutionContext —— 横切关注点封装。

替代 agent_step 中逐层传递的 process/session/workspace/cancellation/... 参数。
新增横切关注点（tracer/profiler/auditor）只改此 dataclass，不改函数签名。

execution_id 不在此处——它属于 ToolExecution，每批次新建。
ctx 存活整个 agent_step，是"工具运行时环境"；Execution 是"一次工具批次的事务"。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from backend.core.loop.models import AgentProcess
    from backend.core.loop.session import AgentSession
    from backend.core.loop.event_bus import EventBus


@dataclass
class ExecutionContext:
    """工具运行时环境——存活整个 agent_step 调用。

    所有横切关注点在此封装。ToolPipeline / ToolExecution 通过 ctx 访问共享资源。
    """

    process: "AgentProcess"
    session: "AgentSession"
    workspace_path: str
    event_bus: "EventBus" = field(default_factory=lambda: __import__(
        "backend.core.loop.event_bus", fromlist=["EventBus"]
    ).EventBus())
    cancellation: threading.Event = field(default_factory=threading.Event)
    logger: Callable | None = None
    artifacts: dict = field(default_factory=dict)

    def is_cancelled(self) -> bool:
        return self.cancellation.is_set()

    def cancel(self) -> None:
        self.cancellation.set()
