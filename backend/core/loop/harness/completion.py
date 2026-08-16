"""CompletionGuard — 任务完成前 Harness 验证（原 executor.py Layer 2 + Layer 3）。

消费 lesson_trigger + rejection 信号：
- Layer 2: 涉及 lesson 前科文件的 task → 验证 required_tools 已调用
- Layer 3: rejection history 中的纠正指令 → 验证已被处理
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.core.loop.signals import GovernanceSignal, HarnessResult
from backend.core.loop.signal_bus import HarnessPlugin
from backend.core.loop.harness.tool_history import tool_succeeded

if TYPE_CHECKING:
    from backend.core.loop.models import AgentProcess


class CompletionGuard(HarnessPlugin):
    """任务完成前验证：必要工具已调用 + rejection 指令已处理。"""

    name = "completion_guard"
    description = "声明完成时验证：lesson required_tools 已执行、rejection 指令已处理"

    subscribed_sources = ["lesson_trigger", "rejection"]
    subscribed_severities = ["critical", "high", "medium"]

    def on_signals(
        self,
        signals: list[GovernanceSignal],
        process: "AgentProcess",
    ) -> HarnessResult:
        """验证任务完成条件是否满足。

        检查:
        1. lesson 信号中 required_tools 是否已调用
        2. rejection 信号中的纠正指令是否已在 session 中处理
        """
        result = HarnessResult()

        # Layer 2: 检查 required_tools
        missing = self._check_required_tools(signals, process)
        if missing:
            result.missing_tools.extend(missing)
            result.nudge_text = f"[完成前需先调用以下工具] {', '.join(missing)}"
            result.allowed = False
            result.blocked = True

        # Layer 3: 检查 rejection 指令
        unchecked = self._check_rejection_instructions(signals, process)
        if unchecked:
            result.warnings.extend(unchecked)
            if not result.nudge_text:
                result.nudge_text = (
                    f"[完成检查] 以下历史纠正指令未被处理: {'; '.join(unchecked)}"
                )

        return result

    def _check_required_tools(
        self,
        signals: list[GovernanceSignal],
        process: "AgentProcess",
    ) -> list[str]:
        """检查涉及前科文件的 task 是否遗漏必要工具。

        v0.42: 从 tool_already_called（只检查"调过没有"）升级为 tool_succeeded
        （验证工具调用是否真正成功：is_error=False 且 exit_code=0）。
        """
        if process is None:
            return []
        task_desc = process.task_description or ""
        missing: list[str] = []

        for sig in signals:
            if sig.source != "lesson_trigger":
                continue
            if not sig.required_tools:
                continue

            # 检查 task 是否涉及该 signal 的文件
            sig_files = sig.target_files
            if sig_files:
                task_involves_signal = any(f in task_desc for f in sig_files)
                if not task_involves_signal:
                    continue

            for tool_name in sig.required_tools:
                if not tool_succeeded(process, tool_name):
                    missing.append(tool_name)

        return missing

    def _check_rejection_instructions(
        self,
        signals: list[GovernanceSignal],
        process: "AgentProcess",
    ) -> list[str]:
        """检查 rejection 纠正指令是否已被处理。"""
        rejection_signals = [s for s in signals if s.source == "rejection"]
        if not rejection_signals:
            return []

        session_messages = (
            process.session.messages if (process and process.session) else []
        )
        session_text = " ".join(
            m.get("content", "") for m in session_messages
        )

        unchecked: list[str] = []
        for sig in rejection_signals:
            instruction = sig.rule
            if not instruction:
                continue
            instr_words = instruction.split()
            if not instr_words:
                continue
            matched = sum(1 for w in instr_words if w in session_text)
            if matched / len(instr_words) < 0.5:
                unchecked.append(instruction[:120])

        return unchecked
