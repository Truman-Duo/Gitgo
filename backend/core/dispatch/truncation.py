"""Tool Result Truncation —— 工具结果截断 + 持久化。

在结果进入 session 之前控制大小。完整输出写 .gitgo/tool-results/，2KB 预览返给 LLM。

Claude Code 持久化方案的 Python 实现：结构化 JSON 不能 head+tail 截断（会切断结构），
所以完整存磁盘 + 预览给 LLM。和 gitgo 已有的 Raw 永存/Context 转录设计一致。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

MAX_OUTPUT_CHARS = 32_000      # Reasonix 标准：32KB
PREVIEW_CHARS = 2_000          # Claude Code 标准：2KB


def format_tool_result(
    tool_name: str,
    result_data: dict | None,
    workspace_path: str = "",
) -> str:
    """格式化工具结果为 LLM 可读文本。超限自动持久化 + 预览。

    Args:
        tool_name: 工具名
        result_data: 工具返回的原始数据
        workspace_path: 工作区路径（.gitgo/tool-results/ 的父目录）

    Returns:
        格式化后的文本（≤ MAX_OUTPUT_CHARS + 截断提示）
    """
    if result_data is None:
        return f"[工具 {tool_name} 完成，无输出]"

    output = json.dumps(result_data, ensure_ascii=False, indent=2)

    if len(output) <= MAX_OUTPUT_CHARS:
        return f"[工具 {tool_name} 结果]\n{output}"

    # 持久化完整输出
    cache_dir = None
    if workspace_path:
        cache_dir = Path(workspace_path) / ".gitgo" / "tool-results"
        cache_dir.mkdir(parents=True, exist_ok=True)

    short_id = uuid.uuid4().hex[:8]
    cache_path = (cache_dir / f"{tool_name}_{short_id}.json") if cache_dir else None

    if cache_path:
        cache_path.write_text(output, encoding="utf-8")

    preview = output[:PREVIEW_CHARS]
    path_hint = (
        f"\n\n[输出被截断: 共 {len(output)} 字符。"
        f"完整内容已保存至 {cache_path}。"
        f"使用 Read 工具读取完整输出。]"
    ) if cache_path else (
        f"\n\n[输出被截断: 共 {len(output)} 字符。]"
    )

    return f"[工具 {tool_name} 结果]\n{preview}{path_hint}"
