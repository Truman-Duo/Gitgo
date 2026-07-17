"""Agent Executor — B-level Agent 多步执行（含工具调用循环 + Harness 三层注入）。

每轮 agent_run: 注入工具 prompt → 追加指令 → LOOP(LLM → 解析 → dispatch → 追加结果)
直至 TASK_COMPLETE / max_steps / doom_loop。

v0.32: XML tool-calling loop + Harness policy-aware pre-dispatch +
       lesson-triggered verification + rejection-history completion check.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING

from backend.core.loop.models import AgentProcess, ProcessStatus
from backend.core.loop.task_gate import TaskGate, check_doom_loop
from backend.core.loop.context_window import ContextWindow

if TYPE_CHECKING:
    from backend.core.loop.llm import LLMProvider
    from backend.core.loop.session import AgentSession
    from backend.core.dispatch.dispatcher import ToolDispatcher

_task_gate = TaskGate()
_context_window = ContextWindow()

TOOL_CALL_PATTERN = re.compile(
    r"<tool_call>\s*<name>(.*?)</name>\s*<args>(.*?)</args>\s*</tool_call>",
    re.DOTALL,
)

TOOL_PROMPT_MARKER = "## 可用工具 (B Agent Ring 3)"


def agent_step(
    process: AgentProcess,
    llm_provider: "LLMProvider",
    instruction: str = "",
    dispatcher: "ToolDispatcher | None" = None,
    workspace_path: str = "",
) -> dict:
    """执行 B Agent 多步循环：追加指令 → LOOP(LLM → 解析 tool_call → dispatch → 追加结果)。

    循环内:
    - ContextWindow 检查（50% 通知 / 80% prune / 90% compact）
    - XML <tool_call> 解析 → dispatcher.dispatch() → 空结果保护
    - Harness Layer 1: 每次 dispatch 前 policy pre-check（SignalBus / 旧格式兼容）
    - Harness Layer 2: 声明完成时验证必要工具已调用
    - Harness Layer 3: 对照 rejection history 检查完成质量
    - TaskGate（零步完成拦截）+ doom_loop 检测
    """
    session = process.session
    if session is None:
        return _error_result(process, "NO_SESSION")

    if process.status != ProcessStatus.RUNNING:
        return _status_result(process, session)

    # 注入工具 prompt（首次）
    if dispatcher is not None and dispatcher._executors:
        _inject_tool_prompt(session, dispatcher, process)

    # 追加用户指令
    if instruction:
        session.append_user(instruction)
    elif not session.messages:
        return _error_result(process, "EMPTY_INSTRUCTION")

    # ── 多步循环 ──
    # 检测 context_snapshot 格式：新格式有 "signals" 键
    harness = process.context_snapshot or {}
    signals = harness.get("signals")
    _use_signal_bus = signals is not None

    # 初始化 SignalBus（新格式）
    _signal_bus = None
    if _use_signal_bus:
        from backend.core.loop.signal_bus import SignalBus
        _signal_bus = (
            SignalBus.from_contract(workspace_path) if workspace_path
            else SignalBus()
        )
        # 运行一次 pre_dispatch 获取约束摘要，捕获返回值
        _pre_result = _signal_bus.dispatch(signals, process, context="pre_dispatch")
        if _pre_result.suggestions:
            for sug in _pre_result.suggestions[:3]:
                session.append_user(f"[治理建议] {sug}")

    while process.steps_used < process.max_steps:
        # ── 上下文窗口检查 ──
        window_check = _context_window.check(session)
        if window_check["action"] == "prune":
            _context_window.prune(session, harness_data=harness)
        elif window_check["action"] == "force_compact":
            retention_suggestions = None
            if _use_signal_bus and _signal_bus is not None:
                retention_result = _signal_bus.dispatch(
                    signals, process, context="retention",
                )
                retention_suggestions = retention_result.suggestions or None
            _context_window.compact(session, llm_provider, harness_data=harness,
                                    retention_suggestions=retention_suggestions)

        # 调用 LLM
        try:
            start = time.time()
            response = llm_provider.chat(
                session.to_openai_messages(),
                provider_id=process.provider_id,
            )
            duration_ms = (time.time() - start) * 1000
        except Exception as exc:
            process.status = ProcessStatus.KILLED
            return _make_result(process, session, "", duration_ms=0,
                              error=str(exc), status_override="killed")

        session.append_assistant(response)
        process.steps_used += 1

        # ── 解析 tool_call ──
        tool_calls = _parse_tool_calls(response)

        if tool_calls:
            for tc in tool_calls:
                tool_name = tc["name"]
                tool_args = tc["args"]

                # Harness Layer 1: Policy-aware pre-dispatch
                if _use_signal_bus and _signal_bus is not None:
                    pre_check = _signal_bus.check_tool(
                        tool_name, tool_args, process, signals=signals,
                    )
                else:
                    pre_check = _policy_pre_check(tool_name, tool_args, process)
                if not pre_check["allowed"]:
                    session.append_user(
                        f"[工具调用被阻止] {tool_name}: {pre_check['reason']}"
                    )
                    continue

                # Dispatch
                if dispatcher is not None:
                    result = dispatcher.dispatch(process, tool_name, tool_args)
                    result_text = _format_tool_result(tool_name, result)
                else:
                    result_text = f"[无 dispatcher，无法调用工具 {tool_name}]"

                session.append_user(result_text)

            _track_step(process, response[:200])
            continue  # 返回 LLM 继续

        # ── 检查完成 ──
        if _is_completion(response):
            if _use_signal_bus and _signal_bus is not None:
                # 新格式：通过 SignalBus.dispatch 统一检查 Layer 2 + 3
                completion_result = _signal_bus.dispatch(
                    signals, process, context="completion",
                )
                if completion_result.blocked and completion_result.missing_tools:
                    # v0.36: nudge 计数器 + 逃生舱
                    _inc_nudge_counter(process, "required_tools")
                    if _get_nudge_count(process, "required_tools") >= 3:  # MAX_NUDGE_REPEAT
                        process.status = ProcessStatus.KILLED
                        return _make_result(process, session, response,
                                          error="nudge_escalation",
                                          status_override="failed")
                    session.append_user(
                        f"[完成前需先调用以下工具] {', '.join(completion_result.missing_tools)}",
                        message_type="governance_nudge",
                        referenced_files=completion_result.missing_tools,
                    )
                    continue
                if completion_result.warnings:
                    _inc_nudge_counter(process, "rejection")
                    if _get_nudge_count(process, "rejection") >= 3:
                        process.status = ProcessStatus.KILLED
                        return _make_result(process, session, response,
                                          error="nudge_escalation",
                                          status_override="failed")
                    session.append_user(
                        f"[完成检查] 以下历史纠正指令未被处理: {'; '.join(completion_result.warnings)}",
                        message_type="governance_nudge",
                    )
                    continue
            else:
                # 旧格式：Layer 2 + Layer 3 分别检查
                # Harness Layer 3: Rejection-history completion check
                rejection_warning = _rejection_check(process)
                if rejection_warning:
                    session.append_user(rejection_warning)
                    continue

                # Harness Layer 2: Lesson-triggered tool verification
                missing = _verify_required_tools(process)
                if missing:
                    session.append_user(
                        f"[完成前需先调用以下工具] {', '.join(missing)}"
                    )
                    continue

            # TaskGate
            gate_decision = _task_gate.decide(process, response)
            if gate_decision.need_reentry:
                session.append_user(gate_decision.nudge_text)
                continue

            process.status = ProcessStatus.COMPLETED
            process.result = {"response": response, "steps_used": process.steps_used}
            return _make_result(process, session, response,
                              duration_ms=duration_ms,
                              window_action=window_check["action"],
                              window_usage_ratio=window_check["usage_ratio"])

        # ── 普通文本（无 tool_call 无 TASK_COMPLETE）──
        _track_step(process, response[:200])

        if check_doom_loop(getattr(process, '_step_history', [])):
            process.status = ProcessStatus.KILLED
            return _make_result(process, session, response,
                              duration_ms=duration_ms, doom_loop=True)

        # 纯文本死循环检测：连续 3 次相同前缀 → 终止
        if _repeated_plain_text(process, response):
            process.status = ProcessStatus.KILLED
            return _make_result(process, session, response,
                              duration_ms=duration_ms,
                              error="plain_text_loop_detected",
                              status_override="killed")

        # Token budget 延续检测
        budget_check = _context_window.check_budget_continuity(session)
        if budget_check["stagnant"]:
            session.append_user(
                "[系统提示] 最近几轮对话无实质进展（无工具调用或完成信号）。"
                "请调用工具或回复 TASK_COMPLETE 结束任务。"
            )
            continue

    # max_steps 耗尽
    process.status = ProcessStatus.KILLED
    return _make_result(process, session, "", duration_ms=0,
                       status_override="killed")


# ── Tool Prompt ──────────────────────────────────────────────

def _inject_tool_prompt(session: "AgentSession", dispatcher: "ToolDispatcher",
                        process: AgentProcess) -> None:
    """首次注入工具可用性 prompt 到 session。"""
    for msg in session.messages:
        if msg.get("role") == "system" and TOOL_PROMPT_MARKER in msg.get("content", ""):
            return  # 已注入

    tool_names = list(dispatcher._executors.keys())
    allowed = process.tool_registry.list_all() if process.tool_registry else tool_names
    available = [t for t in tool_names if t in allowed]

    prompt = (
        f"{TOOL_PROMPT_MARKER}\n"
        f"可用工具: {', '.join(available) if available else '无'}\n\n"
        "如需调用工具，请使用以下格式：\n"
        "<tool_call>\n"
        "  <name>工具名</name>\n"
        "  <args>{\"key\": \"value\"}</args>\n"
        "</tool_call>\n\n"
        "工具结果将以普通文本追加到对话中。\n"
        "当任务完成时，回复 TASK_COMPLETE 并给出最终结果。"
    )
    if session.messages and session.messages[0].get("role") == "system":
        session.messages[0]["content"] += "\n\n" + prompt
    else:
        session.messages.insert(0, {"role": "system", "content": prompt})


# ── XML 解析 ─────────────────────────────────────────────────

def _parse_tool_calls(response: str) -> list[dict]:
    """从 LLM 响应中提取 <tool_call> 块。"""
    matches = TOOL_CALL_PATTERN.findall(response)
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


# ── 空结果保护 ───────────────────────────────────────────────

def _format_tool_result(tool_name: str, result) -> str:
    """格式化工具结果为 LLM 可读文本，空结果替换为占位消息。"""
    if result.error:
        if result.error == "TOOL_TIMEOUT":
            return f"[工具 {tool_name} 超时（30s）]"
        return f"[工具 {tool_name} 错误: {result.error}]"

    data = result.data
    if data is None or data == {}:
        return f"[工具 {tool_name} 完成，无输出]"

    if isinstance(data, dict):
        return f"[工具 {tool_name} 结果]\n{json.dumps(data, ensure_ascii=False, indent=2)}"

    return f"[工具 {tool_name} 结果]\n{data}"


# ── Harness Layer 1: Policy-aware Pre-dispatch ───────────────

def _policy_pre_check(tool_name: str, args: dict, process: AgentProcess) -> dict:
    """工具调用前检查 harness 规则。

    检查项:
    - lesson_triggers 中标记的危险操作 → 验证前提工具已执行
    - contract_drift 文件上的写入操作 → 要求先执行 drift check
    """
    context = process.context_snapshot or {}
    lessons = context.get("lesson_triggers", [])
    drift_files = [d.get("file", "") for d in context.get("contract_drift", [])]

    for lt in lessons:
        dangerous = lt.get("dangerous_tools", [])
        if tool_name in dangerous:
            prereqs = lt.get("prerequisite_tools", [])
            if prereqs and not _tools_already_called(process, prereqs):
                return {
                    "allowed": False,
                    "reason": f"Lesson '{lt.get('rule', '')}' 要求先执行: {prereqs}",
                }

    # 写入 contract_drift 文件前建议 drift check
    write_tools = {"formalize", "push", "sync"}
    if tool_name in write_tools:
        target_file = args.get("file", "")
        if target_file in drift_files:
            if not _tool_already_called(process, "scan"):
                return {
                    "allowed": False,
                    "reason": f"文件 {target_file} 有 contract drift，写入前请先 scan",
                }

    return {"allowed": True}


# ── Harness Layer 2: Lesson-triggered Tool Verification ──────

def _verify_required_tools(process: AgentProcess) -> list[str]:
    """完成时检查涉及 '前科' 文件的 task 是否遗漏必要工具。"""
    context = process.context_snapshot or {}
    lessons = context.get("lesson_triggers", [])
    task_desc = process.task_description or ""

    missing = []
    for lt in lessons:
        lt_file = lt.get("file", "")
        if lt_file and lt_file in task_desc:
            required = lt.get("required_tools", [])
            for r in required:
                if not _tool_already_called(process, r):
                    missing.append(r)

    return missing


# ── Harness Layer 3: Rejection-history Completion Check ──────

def _rejection_check(process: AgentProcess) -> str:
    """对照 rejection history 验证完成质量。"""
    context = process.context_snapshot or {}
    rejections = context.get("rejection_history", "")
    if not rejections:
        return ""

    instructions = _extract_rejection_instructions(rejections)
    session_text = " ".join(
        m.get("content", "") for m in (process.session.messages if process.session else [])
    )

    unchecked = []
    for instr in instructions:
        instr_words = instr.split()
        if not instr_words:
            continue
        matched = sum(1 for w in instr_words if w in session_text)
        if matched / len(instr_words) < 0.5:
            unchecked.append(instr)

    if unchecked:
        return f"[完成检查] 以下历史纠正指令未被处理: {'; '.join(unchecked)}"
    return ""


def _extract_rejection_instructions(rejection_text: str) -> list[str]:
    """从 rejection 文本中提取纠正指令（#N 格式）。"""
    import re as _re
    if not _re.search(r"#\d+", rejection_text):
        return []
    parts = _re.split(r"#\d+\s+", rejection_text)
    return [p.strip() for p in parts if p.strip()]


# ── 工具调用历史查询（从共享模块）────────────────────────────

from backend.core.loop.harness.tool_history import tool_already_called as _tool_already_called
from backend.core.loop.harness.tool_history import tools_already_called as _tools_already_called


# ── Helpers ──────────────────────────────────────────────────

def _track_step(process: AgentProcess, response_prefix: str) -> None:
    if not hasattr(process, '_step_history'):
        process._step_history = []  # type: ignore[attr-defined]
    process._step_history.append({  # type: ignore[attr-defined]
        "tool_name": "llm_call",
        "args": response_prefix,
    })
    if len(process._step_history) > 5:  # type: ignore[attr-defined]
        process._step_history = process._step_history[-5:]  # type: ignore[attr-defined]


def _is_completion(response: str) -> bool:
    markers = [
        "任务完成", "分析完成", "处理完成", "执行完成",
        "TASK_COMPLETE", "DONE:", "FINAL_ANSWER:",
    ]
    return any(m in response for m in markers)


def _repeated_plain_text(process: AgentProcess, response: str,
                         threshold: int = 3) -> bool:
    """检测连续纯文本响应是否陷入重复循环。

    最近 threshold 条 assistant 消息的前 100 字符相同 → 死循环。
    """
    if not process.session:
        return False
    assistant_msgs = [
        m.get("content", "")[:100]
        for m in process.session.messages
        if m.get("role") == "assistant"
    ]
    if len(assistant_msgs) < threshold:
        return False
    recent = assistant_msgs[-threshold:]
    return len(set(recent)) == 1


def _make_result(process: AgentProcess, session: "AgentSession",
                 response: str, *, duration_ms: float = 0.0,
                 error: str = "", doom_loop: bool = False,
                 status_override: str = "",
                 window_action: str = "none",
                 window_usage_ratio: float = 0.0) -> dict:
    return {
        "response": response,
        "process_id": process.process_id,
        "status": status_override or process.status.value,
        "steps_used": process.steps_used,
        "steps_remaining": process.max_steps - process.steps_used,
        "session_tokens": session.estimate_tokens(),
        "duration_ms": duration_ms,
        "error": error,
        "doom_loop": doom_loop,
        "window_action": window_action,
        "window_usage_ratio": window_usage_ratio,
    }


def _error_result(process: AgentProcess, error: str) -> dict:
    return {
        "response": "",
        "process_id": process.process_id,
        "status": process.status.value,
        "error": error,
        "steps_used": process.steps_used,
        "steps_remaining": 0,
        "session_tokens": 0,
        "doom_loop": False,
    }


def _status_result(process: AgentProcess, session: "AgentSession") -> dict:
    return {
        "response": "",
        "process_id": process.process_id,
        "status": process.status.value,
        "steps_used": process.steps_used,
        "steps_remaining": process.max_steps - process.steps_used,
        "session_tokens": session.estimate_tokens(),
        "doom_loop": False,
    }


# ── v0.36: Nudge Counter ──────────────────────────────────

def _inc_nudge_counter(process: AgentProcess, nudge_type: str) -> None:
    if not hasattr(process, '_nudge_counters'):
        process._nudge_counters = {}
    process._nudge_counters[nudge_type] = process._nudge_counters.get(nudge_type, 0) + 1


def _get_nudge_count(process: AgentProcess, nudge_type: str) -> int:
    return getattr(process, '_nudge_counters', {}).get(nudge_type, 0)


# ── v0.36: 中途约束晋升 ──────────────────────────────────

import re as _re

_NEGATIVE_DIRECTIVE_RE = _re.compile(
    r'(?:不要|先别|这次不要|暂时别|do not|don\'t|must not|never)\s+(.{10,80})',
    _re.I,
)


def _has_action_object(text: str) -> bool:
    """检查是否包含明确的动词+名词结构（判别"不要这样" vs "不要改 API"）。"""
    import re
    # 有中英文动词+宾语: "改API", "修改文件", "delete user"
    return bool(re.search(
        r'(?:改|动|删|修改|删除|新建|重构|迁移|调用|update|delete|remove|change|modify|refactor)\s*\w+',
        text, re.I,
    ))


def _promote_mid_task_constraints(session, process: AgentProcess) -> int:
    """从最近 user 消息中检测中途指令 → 晋升为 task 约束。

    Returns: 新晋升的约束数。
    """
    if not hasattr(process, 'task_constraints'):
        process.task_constraints = []

    recent_user_msgs = [
        m for m in session.messages[-5:]
        if m.get("role") == "user"
    ]
    promoted = 0

    for msg in recent_user_msgs:
        content = msg.get("content", "")
        candidates = _NEGATIVE_DIRECTIVE_RE.findall(content)
        candidates = [c for c in candidates if _has_action_object(c)]
        candidates = [c for c in candidates
                      if not any(q in c for q in ["lesson", "规则说", "系统提示", "Claude说"])]

        for c in candidates:
            if c not in process.task_constraints:
                process.task_constraints.append(c)
                promoted += 1

    return promoted


def _build_system_message_for_llm(process: AgentProcess,
                                   base_system: str) -> str:
    """L2-ext: 动态拼接中途约束到 system message 末尾。

    代价: constraint 变化时破坏 prompt cache。权衡: task 内变化频率极低(0-3次)。
    """
    if not getattr(process, 'task_constraints', None):
        return base_system

    constraint_block = "\n\n## Task-level Constraints (this task only)\n" + "\n".join(
        f"- {c}" for c in process.task_constraints
    )
    return base_system + constraint_block
