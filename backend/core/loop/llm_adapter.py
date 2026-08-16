"""LLM Adapter —— Function Calling 格式适配。

替代 _inject_tool_prompt（XML 文档注入 system prompt）+ _parse_tool_calls（XML 正则）。

核心变化：
- build_tools_json: 从 AgentTool 列表生成 OpenAI tools API 格式
- parse_tool_calls: 优先解析 function calling 响应，回退 XML
- XML 降级保底：非 OpenAI 模型不支持 tool_calls 时仍可工作
"""

from __future__ import annotations

import json
import re

# XML 降级正则（保留，给不支持 function calling 的模型用）
TOOL_CALL_XML_RE = re.compile(
    r"<tool_call>\s*<name>(.*?)</name>\s*<args>(.*?)</args>\s*</tool_call>",
    re.DOTALL,
)


def build_tools_json(tools: dict) -> list[dict]:
    """从 AgentTool 字典生成 OpenAI tools API 格式的 JSON 数组。

    Args:
        tools: {name: AgentTool} 映射

    Returns:
        [{"type": "function", "function": {"name": "...", "description": "...",
         "parameters": {...}}}, ...]
    """
    result = []
    for name, tool in tools.items():
        if hasattr(tool, "to_openai_function"):
            result.append(tool.to_openai_function())
        else:
            # 兼容尚未迁移到 AgentTool 的旧式工具
            result.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": getattr(tool, "description", name),
                    "parameters": getattr(tool, "parameters", {"type": "object", "properties": {}}),
                },
            })
    return result


def parse_tool_calls(response: str | dict) -> list[dict]:
    """从 LLM 响应中解析工具调用。

    优先解析 function calling 响应（OpenAI tool_calls / Anthropic tool_use），
    回退 XML 正则。

    Args:
        response: LLM 返回的响应文本（str）或原始响应对象（dict）

    Returns:
        [{"name": str, "args": dict}, ...]
    """
    # 路径 1: OpenAI function calling 响应（dict 格式）
    if isinstance(response, dict):
        tool_calls = response.get("tool_calls") or response.get("choices", [{}])[0].get(
            "message", {}
        ).get("tool_calls", [])
        results = []
        for tc in tool_calls:
            func = tc.get("function", tc)
            name = func.get("name", "")
            args_str = func.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {"raw": args_str}
            results.append({"name": name, "args": args})
        if results:
            return results

        # 也检查 content 文本（某些实现把 tool_calls 放 text 里）
        content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
        if isinstance(content, str) and content:
            xml_calls = _parse_xml(content)
            if xml_calls:
                return xml_calls

    # 路径 2: XML 降级（字符串响应）
    if isinstance(response, str):
        return _parse_xml(response)

    return []


def _parse_xml(text: str) -> list[dict]:
    """XML <tool_call> 正则解析（降级路径）。"""
    matches = TOOL_CALL_XML_RE.findall(text)
    results = []
    for name, args_str in matches:
        name = name.strip()
        args_str = args_str.strip()
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            args = {"raw": args_str}
        results.append({"name": name, "args": args})
    return results
