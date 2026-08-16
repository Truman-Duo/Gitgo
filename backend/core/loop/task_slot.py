"""TaskSlot —— 逻辑执行单元的四组件拆分。

TaskSlot 不是 God Object。它是四个独立 dataclass 的薄包装：
- TaskSpec: 不可变任务声明（retry 时复用同一个 spec）
- ContextSnapshot: Scheduler 组装的上下文精选
- Capability: 权限四元组
- TaskRuntime: 运行时状态（retry/resume/cost/latency/cache 只动此类）

SlotBackend ABC 定义了"怎么执行"——ForkBackend 是当前唯一实现。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.core.loop.models import RingLevel


# ── 四组件 ──────────────────────────────────────────────

@dataclass
class TaskSpec:
    """任务定义：不可变声明。retry 时复用同一个 spec。"""

    spec_id: str = ""
    task_description: str = ""        # 要做什么
    expected_output: str = ""          # 期望产出什么格式的结果
    input_interfaces: list[str] = field(default_factory=list)
        # 依赖的上游接口符号，如 ["auth.py:authenticate"]
    output_interfaces: list[str] = field(default_factory=list)
        # 承诺产出的接口符号


@dataclass
class ContextSnapshot:
    """上下文精选：Scheduler 组装，不来自 TaskSlot 自身。"""

    needed: list[dict] = field(default_factory=list)
        # 治理信号 + L1 强制上游注入 + L2 冻结契约
    relevant: list[dict] = field(default_factory=list)
        # 知识库 lesson
    dependency: dict = field(default_factory=dict)
        # 依赖图子图 {file: {callers: [...]}}
    status: dict = field(default_factory=dict)
        # workspace 状态（从 status_dict 取 semantic 层）
    rejections: list[dict] = field(default_factory=list)
        # 最近的 rejection 历史
    phase_brief: str = ""
        # 同级其他 Slot 做了什么（文本摘要）


@dataclass
class Capability:
    """权限四元组：精确控制每个 Slot 的能力边界。

    不预设 "explore"/"coder"/"plan" 等角色名。
    能力 = tool_allowlist + ring_level + resources + max_steps 的组合。
    """

    tool_allowlist: list[str] = field(default_factory=list)
        # 可用工具白名单（ToolRegistry 从列表构建）
    ring_level: Any = None           # RingLevel 枚举
    resources: list[str] = field(default_factory=list)
        # 文件级资源声明（Resource Lock 据此调度，空列表=只读）
    max_steps: int = 50
        # 最大执行步数


@dataclass
class TaskRuntime:
    """运行时状态：retry/resume/cost/latency/cache 只动此类。

    与 TaskSpec 分离的动机：同一个 spec 可以 retry——换新 runtime。
    """

    status: str = "PENDING"           # PENDING | RUNNING | COMPLETED | FAILED | KILLED | ESCALATED
    backend: "SlotBackend | None" = None
    result: dict | None = None        # 结构化返回（含 return_context）
    transcript: Any = None            # TaskTranscriptBuilder 实例
    error: str = ""
    started_at: str = ""
    completed_at: str = ""


# ── EscalateToParent ────────────────────────────────────

@dataclass
class EscalateToParent:
    """执行中遇到不可自行解决的冲突 → 结构化升级给父级。

    不是 Python exception——是放入 TaskRuntime.result 的状态数据。
    """

    slot_id: str
    reason: str                       # 升级原因（如 "mid-execution interface conflict"）
    detail: dict = field(default_factory=dict)
        # {"expected": ..., "actual": ...}


# ── TaskSlot ────────────────────────────────────────────

@dataclass
class TaskSlot:
    """逻辑执行单元 —— 薄包装上述四个组件 + identity 字段。"""

    slot_id: str = ""
    parent_slot_id: str | None = None   # 树关系：谁创建的
    depends_on: list[str] = field(default_factory=list)
        # DAG 关系：等待哪些上游 slot 完成再执行
    depth: int = 1                      # 嵌套深度（默认 max 2，可配置）
    spec: TaskSpec = field(default_factory=TaskSpec)
    context: ContextSnapshot = field(default_factory=ContextSnapshot)
    capability: Capability = field(default_factory=Capability)
    runtime: TaskRuntime = field(default_factory=TaskRuntime)

    @property
    def is_complete(self) -> bool:
        return self.runtime.status == "COMPLETED"

    @property
    def is_escalated(self) -> bool:
        return self.runtime.status == "ESCALATED"

    @property
    def is_terminal(self) -> bool:
        return self.runtime.status in ("COMPLETED", "FAILED", "KILLED", "ESCALATED")


# ── SlotBackend ABC ─────────────────────────────────────

class SlotBackend(ABC):
    """执行后端抽象基类。

    Backend 只回答 How——给定一个 TaskSlot，执行并返回结果。
    Scheduler 回答 Where / When / In What Order。
    """

    @abstractmethod
    def run(self, slot: TaskSlot, workspace_path: str = "") -> dict:
        """执行 slot，返回结构化结果 dict。"""
        ...


# ── ForkBackend ─────────────────────────────────────────

class ForkBackend(SlotBackend):
    """基于 AgentProcessManager.fork() 的子进程执行后端。

    流程：
    1. 从 Capability 构建 ToolRegistry
    2. AgentProcessManager.fork() 创建子进程
    3. agent_step() 执行多步循环
    4. wait 子进程结果
    5. 返回结构化 result
    """

    def __init__(self):
        self._apm = None   # lazy init: daemon 注入 AgentProcessManager 实例

    @property
    def apm(self):
        if self._apm is None:
            from backend.core.loop.manager import AgentProcessManager
            self._apm = AgentProcessManager()
        return self._apm

    def run(self, slot: TaskSlot, workspace_path: str = "") -> dict:
        from backend.core.loop.tools import ToolRegistry
        from backend.core.loop.models import RingLevel
        from backend.core.loop.executor import agent_step
        from backend.core.loop.llm import LLMProvider

        ring = slot.capability.ring_level or RingLevel.RING_3

        # 构建 ToolRegistry
        registry = ToolRegistry(slot.capability.tool_allowlist)

        # 组装 context_snapshot（从 ContextSnapshot 转换为 fork 期望的格式）
        snapshot = {
            "signals": slot.context.needed,
            "lessons": slot.context.relevant,
            "dependency": slot.context.dependency,
            "phase_brief": slot.context.phase_brief,
            "rejections": slot.context.rejections,
        }

        # fork
        process = self.apm.fork(
            parent_id=slot.parent_slot_id,
            role="executor",
            tool_registry=registry,
            max_steps=slot.capability.max_steps,
            ring_level=ring,
            context_snapshot=snapshot,
        )

        slot.runtime.status = "RUNNING"

        try:
            # agent_step 需要 llm_provider——从 daemon context 注入
            # 此处是简化实现：实际运行时由 daemon 传 llm_provider
            # Phase 1: ForkBackend 由 daemon 使用，daemon 将 llm_provider 挂到
            # 实例属性上，run() 内部读取
            llm = getattr(self, '_llm_provider', None)
            if llm is None:
                raise RuntimeError("ForkBackend requires _llm_provider to be set by daemon")

            # 创建 session（agent_step 内部用到 process.session）
            from backend.core.loop.session import AgentSession
            session = AgentSession()
            process.session = session

            # 注入 context_snapshot 到 session（governance brief）
            session.inject_governance_brief({
                "signals": slot.context.needed,
                "brief": slot.context.phase_brief,
            })

            # 不需要 dispatcher——agent_step 走 ToolExecution + ToolPipeline 路径
            result = agent_step(
                process=process,
                llm_provider=llm,
                instruction=slot.spec.task_description,
                dispatcher=None,
                workspace_path=workspace_path,
            )

            slot.runtime.status = "COMPLETED"
            slot.runtime.result = result
            return result

        except Exception as exc:
            slot.runtime.status = "FAILED"
            slot.runtime.error = str(exc)
            return {
                "process_id": process.process_id,
                "status": "FAILED",
                "error": str(exc),
                "steps_used": process.steps_used,
            }
