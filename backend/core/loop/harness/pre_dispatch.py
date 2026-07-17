"""PreDispatchGuard — 工具调用前 Harness 检查（原 executor.py Layer 1）。

消费 lesson_trigger + contract_drift 信号：
- 危险工具（target_tools 中有 BLOCK 信号）→ 验证 prerequisite_tools 已调用
- contract_drift 文件上的写入操作 → 要求先 scan
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.core.loop.signals import GovernanceSignal, HarnessResult
from backend.core.loop.signal_bus import HarnessPlugin

if TYPE_CHECKING:
    from backend.core.loop.models import AgentProcess


class PreDispatchGuard(HarnessPlugin):
    """工具调用前检查：危险工具前置条件 + contract drift 文件保护。"""

    name = "pre_dispatch_guard"
    description = "工具调用前验证：lesson 前置工具已执行、drift 文件已 scan"

    subscribed_sources = ["lesson_trigger", "contract_drift"]
    subscribed_severities = ["critical", "high", "medium"]

    def on_signals(
        self,
        signals: list[GovernanceSignal],
        process: "AgentProcess",
    ) -> HarnessResult:
        """分析信号批次，返回整体约束摘要。

        具体工具检查由 check_tool() 执行。
        """
        result = HarnessResult()

        for sig in signals:
            if sig.category.value == "block" and sig.target_tools:
                prereqs = sig.prerequisite_tools
                if prereqs:
                    result.suggestions.append(
                        f"[{sig.check_id}] 调用 {', '.join(sig.target_tools)} "
                        f"前需先执行: {', '.join(prereqs)}"
                    )
            if sig.source == "contract_drift" and sig.target_files:
                result.suggestions.append(
                    f"文件 {', '.join(sig.target_files)} 有 contract drift，"
                    f"写入前请先 scan 验证签名"
                )

        return result

    def check_tool(
        self,
        tool_name: str,
        args: dict,
        process: "AgentProcess",
        signals: list[GovernanceSignal] | None = None,
    ) -> dict:
        """检查特定工具调用是否允许。

        Args:
            tool_name: 要调用的工具名
            args: 工具参数
            process: 当前 AgentProcess
            signals: 预过滤的信号列表（None = 从 context_snapshot 读取）

        Returns:
            {"allowed": bool, "reason": str}
        """
        if signals is None:
            context = process.context_snapshot or {}
            signals = context.get("signals", [])

        from backend.core.loop.harness.tool_history import tool_already_called, tools_already_called

        # 1. 检查 lesson_trigger 中的危险工具
        for sig in signals:
            if sig.source != "lesson_trigger":
                continue
            if tool_name in sig.target_tools:
                prereqs = sig.prerequisite_tools
                if prereqs and not tools_already_called(process, prereqs):
                    return {
                        "allowed": False,
                        "reason": f"Lesson '{sig.rule}' 要求先执行: {', '.join(prereqs)}",
                    }

        # 2. 写入 contract_drift 文件前要求 scan
        write_tools = {"formalize", "push", "sync"}
        if tool_name in write_tools:
            target_file = args.get("file", "")
            if target_file:
                drift_files = _get_drift_files(signals)
                if target_file in drift_files:
                    if not tool_already_called(process, "scan"):
                        return {
                            "allowed": False,
                            "reason": f"文件 {target_file} 有 contract drift，写入前请先 scan",
                        }

        return {"allowed": True}


def _get_drift_files(signals: list[GovernanceSignal]) -> set[str]:
    """从 contract_drift 信号中提取受影响文件集合。"""
    files: set[str] = set()
    for sig in signals:
        if sig.source == "contract_drift":
            files.update(sig.target_files)
    return files
