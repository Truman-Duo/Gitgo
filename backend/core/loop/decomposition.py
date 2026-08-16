"""Decomposition —— 分区决策：双层门控 + 过度分解防护。

Partition 是多 Agent 系统最核心的 AI 问题——如果切得不好，
context 再干净、backend 再漂亮，最终效果都会下降。

双层决策：
- 层 1（系统规则）：structural hard gate——不依赖 LLM
- 层 2（LLM 自主分解）：LLM 建议拆分 → structural 验证 → 采纳或驳回
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.loop.models import AgentProcess
    from backend.core.loop.session import AgentSession


# ── 数据模型 ────────────────────────────────────────────

@dataclass
class SplitSuggestion:
    """LLM 建议的一个子任务拆分。"""

    task_description: str
    target_files: list[str] = field(default_factory=list)
    input_interfaces: list[str] = field(default_factory=list)
    output_interfaces: list[str] = field(default_factory=list)
    estimated_steps: int = 10


@dataclass
class DecompositionDecision:
    """分区决策结果。"""

    required: bool = False               # 是否强制要求分区
    suggested: bool = False              # 是否建议分区（LLM 提议）
    reason: str = ""                     # 决策原因
    suggestions: list[SplitSuggestion] = field(default_factory=list)
    rejected: bool = False               # 建议是否被 structural 验证驳回
    reject_reason: str = ""


# ── 系统规则层（hard gate）──────────────────────────────

def should_decompose(
    process: "AgentProcess",
    session: "AgentSession",
    target_files: list[str] | None = None,
    nudge_counters: dict[str, int] | None = None,
    context_compact_count: int = 0,
) -> DecompositionDecision:
    """系统规则层硬门控：判断当前任务是否应分区。

    不依赖 LLM——纯 structural 条件检测。
    """
    nudge = nudge_counters or {}
    files = target_files or []

    # 上下文已超 90% 连续 2 轮 → 强制分区
    if context_compact_count >= 2:
        return DecompositionDecision(
            required=True,
            reason=f"manage_context 连续 {context_compact_count} 轮返回 need_compact=True",
        )

    # 步数消耗 ≥ 80% → 强制分区
    if process.max_steps > 0 and process.steps_used / process.max_steps >= 0.8:
        return DecompositionDecision(
            required=True,
            reason=f"max_steps 消耗 {process.steps_used}/{process.max_steps} (≥80%)，任务未完成",
        )

    # target_files ≥ 5 且有交叉依赖 → 建议分区
    if len(files) >= 5 and _has_cross_dependency(files):
        return DecompositionDecision(
            required=False,
            suggested=True,
            reason=f"target_files={len(files)}，有交叉依赖",
        )

    # 任一 nudge_counter ≥ MAX_NUDGE_REPEAT → 强制 upgrade
    from backend.core.loop.context_window import ContextConstants
    for nudge_type, count in nudge.items():
        if count >= ContextConstants.MAX_NUDGE_REPEAT:
            return DecompositionDecision(
                required=True,
                reason=f"nudge_counter[{nudge_type}]={count} ≥ MAX ({ContextConstants.MAX_NUDGE_REPEAT})",
            )

    return DecompositionDecision()


def _has_cross_dependency(files: list[str]) -> bool:
    """检查文件列表是否存在交叉依赖（A import B 且 B import A 或 C import A 且 D import B）。

    简化实现：≥5 个文件且涉及 ≥2 个目录 → 有交叉依赖可能。
    完整检测需要加载 dep_graph——Phase 2 增强。
    """
    if len(files) < 5:
        return False
    dirs = set()
    for f in files:
        parts = f.replace("\\", "/").split("/")
        if len(parts) > 1:
            dirs.add(parts[0])
        else:
            dirs.add(".")
    return len(dirs) >= 2


# ── LLM 自主分解层 ──────────────────────────────────────

def suggest_split(
    task_description: str,
    files: list[str],
    dep_graph: dict | None = None,
) -> list[SplitSuggestion]:
    """LLM 调 decompose_task 工具建议拆分后，经 structural 验证。

    当前为 stub：Phase 1 中 LLM 通过 decompose_task AgentTool 输出
    SplitSuggestion 列表，Scheduler 在 Partition 阶段验证 input/output 接口
    是否能形成完整依赖链。

    本函数可由 LLM 的输出直接填充 SplitSuggestion 列表——不需要此处的启发式。
    保留此函数作为未来非 LLM 启发式分解的入口。
    """
    return []


# ── 过度分解防护 ────────────────────────────────────────

class DecompositionGuard:
    """防止 LLM 滥用 decompose_task 工具。

    - 连续驳回 N 次 → 冷却期（暂时禁用 decompose_task）
    - 分解预算：子 slot max_steps 总和从父预算扣
    """

    MAX_CONSECUTIVE_REJECTIONS = 3         # 连续驳回上限
    COOLDOWN_ROUNDS = 5                     # 冷却轮数

    def __init__(self):
        self._rejection_streak: int = 0
        self._cooldown_remaining: int = 0
        self._total_decompositions: int = 0
        self._total_rejections: int = 0

    def is_available(self) -> bool:
        """decompose_task 工具当前是否可用。"""
        return self._cooldown_remaining <= 0

    def record_accept(self) -> None:
        """一次分解被 structural 验证接受。"""
        self._rejection_streak = 0
        self._cooldown_remaining = 0
        self._total_decompositions += 1

    def record_reject(self) -> None:
        """一次分解被 structural 验证驳回。"""
        self._rejection_streak += 1
        self._total_rejections += 1
        if self._rejection_streak >= self.MAX_CONSECUTIVE_REJECTIONS:
            self._cooldown_remaining = self.COOLDOWN_ROUNDS

    def tick(self) -> None:
        """每轮结束后递减冷却计数。"""
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            if self._cooldown_remaining == 0:
                self._rejection_streak = 0  # 冷却结束，重置

    @property
    def stats(self) -> dict:
        return {
            "rejection_streak": self._rejection_streak,
            "cooldown_remaining": self._cooldown_remaining,
            "total_decompositions": self._total_decompositions,
            "total_rejections": self._total_rejections,
            "available": self.is_available(),
        }
