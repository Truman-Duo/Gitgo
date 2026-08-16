"""共享工具调用历史查询 — 供 executor + harness 插件使用。

避免 tool_already_called / tools_already_called 在三处重复定义。
v0.42: 新增 tool_succeeded —— 从结构化 ToolResult 验证工具调用是否真正成功。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.loop.models import AgentProcess


def tool_already_called(process: "AgentProcess", tool_name: str) -> bool:
    """检查某个工具是否已在当前 session 中调用过（字符串匹配）。"""
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


def tool_succeeded(process: "AgentProcess", tool_name: str) -> bool:
    """检查某个工具是否调用成功（结构化验证，非字符串匹配）。

    判定逻辑：
    1. 在 session.messages 中找到该工具的 tool_result
    2. 读取 message 的 metadata: is_error == False
    3. 检查 exit_code == 0（对于 test/lint/typecheck 等验证类工具）
    4. 如果找不到对应的 tool_result → 返回 False

    与 tool_already_called 的区别：
    - tool_already_called: "B 说它调过了"（字符串匹配，可被 LLM 绕过）
    - tool_succeeded: "工具真正返回了成功状态"（结构化验证，不可伪造）
    """
    if process is None or not process.session:
        return False
    for msg in process.session.messages:
        if msg.get("message_type") != "tool_result":
            continue
        if msg.get("_tool_name") != tool_name:
            continue
        # v0.38+: tool_result 消息可能携带 is_error 标记
        if msg.get("is_error", False):
            return False
        # 检查结构化结果中是否包含 exit_code（验证类工具）
        data = msg.get("data")
        if isinstance(data, dict):
            exit_code = data.get("exit_code")
            if exit_code is not None and exit_code != 0:
                return False
        return True
    return False
