"""Compile the live prompt and retain legacy prompt entry points."""

from backend.core.prompt_templates import render_prompt
import json
from .models import AgentProcess
from .session import AgentSession
from .agent_tool import AgentTool
from .llm_adapter import build_tools_json
TOOL_PROMPT_MARKER = "## 可用工具 (B Agent Ring 3)"

def _refresh_compiled_prompt(
    session: "AgentSession",
    process: AgentProcess,
    tools_dict: dict[str, AgentTool],
    workspace_path: str,
) -> None:
    from backend.core.loop.prompt_compiler import PromptCompiler

    context, _context_version = process.read_context_snapshot()
    governance_brief = str(context.get("brief", ""))
    refs = dict(context.get("context_refs", {}) or {})
    policy = dict(context.get("context_policy", {}) or {})
    lazy_lines = []
    for key in policy.get("lazy", []) or []:
        target = refs.get(key, {})
        if isinstance(target, dict) and target.get("latest"):
            lazy_lines.append(f"- {key}: {target['latest']}")
    if lazy_lines:
        governance_brief = (
            governance_brief.rstrip()
            + "\n\nAddressable context (open only when relevant):\n"
            + "\n".join(lazy_lines)
        )
    text, sections = PromptCompiler.compile(
        process=process,
        tools=tools_dict,
        workspace_path=workspace_path,
        governance_brief=governance_brief,
    )
    text = _build_system_message_for_llm(process, text)
    PromptCompiler.upsert(session, text, sections)
    tool_wire = build_tools_json(tools_dict) if tools_dict else []
    session.context_inventory = {
        "tool_count": len(tool_wire),
        "tool_schema_tokens": max(0, len(json.dumps(
            tool_wire, ensure_ascii=False, sort_keys=True, default=str,
        )) // 4),
    }


def _inject_tool_prompt(session: "AgentSession",
                        tools_dict: dict[str, AgentTool],
                        process: AgentProcess) -> None:
    """注入工具可用性 prompt。含 FC 格式说明 + XML 降级格式。

    v0.38: tools_dict 替代 dispatcher._executors；同时说明两种调用格式。
    """
    for msg in session.messages:
        if msg.get("role") == "system" and TOOL_PROMPT_MARKER in msg.get("content", ""):
            return

    tool_names = list(tools_dict.keys())
    allowed = process.tool_registry.list_all() if process.tool_registry else tool_names
    available = [t for t in tool_names if t in allowed]

    prompt = (
        render_prompt(
            'legacy/tool_calling',
            TOOL_PROMPT_MARKER=format(TOOL_PROMPT_MARKER, ''),
            available_tools=format(', '.join(available) if available else '无', ''),
        )
    )

    prompt = _build_system_message_for_llm(process, prompt)

    if session.messages and session.messages[0].get("role") == "system":
        session.messages[0]["content"] += "\n\n" + prompt
    else:
        session.messages.insert(0, {"role": "system", "content": prompt})


def _build_system_message_for_llm(process: AgentProcess,
                                   base_system: str) -> str:
    """L2-ext: 动态拼接中途约束到 system message 末尾。"""
    if not getattr(process, 'task_constraints', None):
        return base_system

    constraint_block = "\n\n## Task-level Constraints (this task only)\n" + "\n".join(
        f"- {c}" for c in process.task_constraints
    )
    return base_system + constraint_block
