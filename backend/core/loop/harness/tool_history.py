"""共享工具调用历史查询 — 供 executor + harness 插件使用。

避免 tool_already_called / tools_already_called 在三处重复定义。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.loop.models import AgentProcess


def tool_already_called(process: "AgentProcess", tool_name: str) -> bool:
    """检查某个工具是否已在当前 session 中调用过。"""
    if process is None or not process.session:
        return False
    marker = f"[工具 {tool_name}"
    for msg in process.session.messages:
        if msg.get("role") == "user" and marker in msg.get("content", ""):
            return True
    return False


def tools_already_called(process: "AgentProcess", tool_names: list[str]) -> bool:
    """检查所有工具是否都已调用过。"""
    return all(tool_already_called(process, t) for t in tool_names)
