"""AgentProcessManager — 进程树生命周期管理。

fork → wait → kill → reap。daemon 调用 fork；daemon loop 调 reap 回收孤儿。
"""

from __future__ import annotations
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from backend.core.loop.models import AgentProcess, ProcessStatus, RingLevel

if TYPE_CHECKING:
    from backend.core.loop.tools import ToolRegistry


class AgentProcessManager:
    """进程树管理器。daemon 之上的调度层。"""

    # 集成点：daemon 在启动时创建实例，workspace_dirty 处理前调 fork，
    # 定期调 reap 回收孤儿
    MAX_FORK_DEPTH = 2

    def __init__(self):
        self._processes: dict[str, AgentProcess] = {}

    def fork(self, parent_id: str | None, role: str,
             tool_registry: "ToolRegistry", max_steps: int,
             ring_level: RingLevel,
             context_snapshot: dict | None = None) -> AgentProcess:
        """创建子进程。parent_id=None 时为 A 级 Agent (ring 0)。"""
        # Fork depth ≤ 2 检查
        if parent_id is not None:
            parent = self._processes.get(parent_id)
            if parent and parent.parent_id is not None:
                raise ValueError(
                    f"Fork depth exceeds {self.MAX_FORK_DEPTH}. "
                    f"B-level agents cannot fork."
                )

        # B 级 Agent 的 context_snapshot 不能包含 contract 原文或完整 lessons
        if ring_level == RingLevel.RING_3 and context_snapshot:
            forbidden = {"contract_yaml", "lessons_full", "contract_raw"}
            if forbidden & set(context_snapshot.keys()):
                raise ValueError(
                    "B-level agent context must not contain raw contract "
                    "or full lessons. Use governance_brief summaries instead."
                )

        process = AgentProcess(
            process_id=str(uuid.uuid4()),
            role=role,
            ring_level=ring_level,
            tool_registry=tool_registry,
            max_steps=max_steps,
            status=ProcessStatus.RUNNING,
            parent_id=parent_id,
            context_snapshot=context_snapshot,
            created_at=datetime.now().isoformat(),
        )
        self._processes[process.process_id] = process

        from backend.core.history import HistoryManager
        HistoryManager.add_operation(
            "system", "agent_forked", "success",
            {"process_id": process.process_id, "role": role,
             "ring_level": ring_level.value, "parent_id": parent_id},
            correlation_id=process.process_id,
        )
        return process

    def wait(self, process_id: str,
             timeout: float | None = None) -> dict | None:
        """阻塞直到子进程完成。超时返回 None 不 kill。"""
        process = self._processes.get(process_id)
        if not process:
            return None

        start = time.time()
        while process.status == ProcessStatus.RUNNING:
            if timeout and time.time() - start > timeout:
                return None
            time.sleep(0.5)

        return process.result

    def kill(self, process_id: str) -> None:
        """终止子进程。标记为 KILLED。"""
        process = self._processes.get(process_id)
        if process and process.status == ProcessStatus.RUNNING:
            process.status = ProcessStatus.KILLED
            from backend.core.history import HistoryManager
            HistoryManager.add_operation(
                "system", "agent_killed", "success",
                {"process_id": process_id, "role": process.role},
                correlation_id=process_id,
            )

    def reap(self) -> list[AgentProcess]:
        """回收孤儿进程——父进程已死但本身还在的进程。"""
        active_ids = {p.process_id for p in self._processes.values()
                      if p.status == ProcessStatus.RUNNING}
        orphans = []
        for p in self._processes.values():
            if p.parent_id and p.parent_id not in active_ids:
                p.status = ProcessStatus.ORPHANED
                orphans.append(p)
                from backend.core.history import HistoryManager
                HistoryManager.add_operation(
                    "system", "agent_reaped", "success",
                    {"process_id": p.process_id, "role": p.role,
                     "parent_id": p.parent_id},
                    correlation_id=p.process_id,
                )
        return orphans

    def get(self, process_id: str) -> AgentProcess | None:
        return self._processes.get(process_id)
