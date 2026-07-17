"""TaskGate — B Agent 完成检查 + doom_loop 检测。

参考 OpenCode task/gate.ts: Agent 不能自己说"做完了"就停，
必须检查是否还有未完成任务。最多重入 2 次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.loop.models import AgentProcess


@dataclass
class GateDecision:
    allowed: bool
    reason: str = ""
    need_reentry: bool = False
    nudge_text: str = ""
    cap_exceeded: bool = False


class TaskGate:
    """B Agent 完成时的任务门检查。"""

    MAX_REENTRY = 2

    def __init__(self):
        self._reentry_count: dict[str, int] = {}

    def decide(self, process: "AgentProcess", llm_response: str) -> GateDecision:
        """检查 LLM 响应是否真的表示完成。

        规则:
        1. LLM 说完成但没有 tool 产出 → allowed
        2. LLM 说完成但 steps_used=0（什么都没做）→ need_reentry
        3. 重入次数超 MAX_REENTRY → cap_exceeded（允许完成）
        """
        pid = process.process_id
        reentries = self._reentry_count.get(pid, 0)

        # 如果 B Agent 一步都没走就说完成 → 需要重入
        if process.steps_used == 0:
            if reentries >= self.MAX_REENTRY:
                return GateDecision(
                    allowed=True, reason="cap_exceeded_zero_steps",
                    cap_exceeded=True,
                )
            self._reentry_count[pid] = reentries + 1
            return GateDecision(
                allowed=False, reason="zero_steps",
                need_reentry=True,
                nudge_text=(
                    "你还没有执行任何操作就声明任务完成。"
                    "请至少执行一步分析或操作后再确认完成。"
                ),
            )

        # 正常完成
        return GateDecision(allowed=True, reason="completed")


def check_doom_loop(recent_steps: list[dict], threshold: int = 3) -> bool:
    """检测死循环: 连续 threshold 次相同 tool+相同 args。

    recent_steps: 最近几步的 {tool_name, args} 记录。
    """
    if len(recent_steps) < threshold:
        return False
    last_n = recent_steps[-threshold:]
    first = last_n[0]
    return all(
        s.get("tool_name") == first.get("tool_name")
        and s.get("args") == first.get("args")
        for s in last_n[1:]
    )
