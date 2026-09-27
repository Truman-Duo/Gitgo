"""共享工具调用历史查询 — 供 executor + harness 插件使用。

避免 tool_already_called / tools_already_called 在三处重复定义。
v0.42: 新增 tool_succeeded —— 从结构化 ToolResult 验证工具调用是否真正成功。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.loop.models import AgentProcess


def tool_already_called(process: "AgentProcess", tool_name: str) -> bool:
    """Return whether Host receipts prove a successful tool invocation.

    Session prose is untrusted: user text and model-visible tool formatting must
    never satisfy a governance prerequisite.  A committed receipt is the single
    source of truth for both read and effectful tools.
    """
    if process is None:
        return False
    return any(
        receipt.get("tool_name") == tool_name
        and receipt.get("succeeded") is True
        and receipt.get("committed") is True
        for receipt in (getattr(process, "tool_receipts", []) or [])
    )


def tools_already_called(process: "AgentProcess", tool_names: list[str]) -> bool:
    """检查所有工具是否都已调用过。"""
    return all(tool_already_called(process, t) for t in tool_names)


def tool_succeeded(process: "AgentProcess", tool_name: str) -> bool:
    """Alias for the receipt-backed success predicate."""
    return tool_already_called(process, tool_name)
