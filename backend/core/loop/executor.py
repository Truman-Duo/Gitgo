"""Agent Executor — B-level Agent 多步执行（含工具调用循环 + Harness 三层注入）。

每轮 agent_run: 注入工具 prompt → 追加指令 → LOOP(LLM → 解析 → dispatch → 追加结果)
直至 TASK_COMPLETE / max_steps / doom_loop。

v0.32: XML tool-calling loop + Harness policy-aware pre-dispatch +
       lesson-triggered verification + rejection-history completion check.
v0.38: Function Calling 优先 + XML 降级；ToolExecution 批次事务 + ToolPipeline 五步管道；
       EventBus 事件骨干；LoopGuard 统一循环守卫。
v0.44: 流式响应 —— on_stream_event 回调注入；stream_chat 替代同步 chat；
       StreamInterruptedError 恢复 + chat() 降级。
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from backend.core.loop.agent_tool import AgentTool
from backend.core.loop.event_bus import EventBus
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.llm_adapter import build_tools_json, parse_tool_calls
from backend.core.loop.loop_guard import LoopGuard
from backend.core.loop.models import AgentProcess, ProcessStatus
from backend.core.loop.context_window import ContextWindow, manage_context
from backend.core.loop.transcript import TaskTranscriptBuilder
from backend.core.loop.tool_execution import ToolExecution

if TYPE_CHECKING:
    from backend.core.loop.llm import LLMProvider
    from backend.core.loop.session import AgentSession
    from backend.core.dispatch.dispatcher import ToolDispatcher

_context_window = ContextWindow()
_loop_guard = LoopGuard()

TOOL_PROMPT_MARKER = "## 可用工具 (B Agent Ring 3)"

# ── v0.44: 流中断恢复 ──
MAX_STREAM_RECOVERIES = 1


def _stream_recovery_message(has_text: bool, had_partial_tool: bool) -> str:
    """生成流中断恢复提示消息。"""
    if had_partial_tool:
        return (
            "前一次回复在流式传输工具调用参数时中断。"
            "如果需要工具调用请从头发出新的完整工具调用；"
            "不要依赖中断流中的部分参数。继续当前任务。"
        )
    if has_text:
        return (
            "前一次回复在流式传输中中断。"
            "从上面中断处继续，不要重复已有内容。继续当前任务。"
        )
    return (
        "前一次回复在流式传输开始前中断。"
        "继续当前任务并提供正常回复。"
    )


def agent_step(
    process: AgentProcess,
    llm_provider: "LLMProvider",
    instruction: str = "",
    dispatcher: "ToolDispatcher | None" = None,
    workspace_path: str = "",
    on_stream_event: callable = None,
) -> dict:
    """执行 B Agent 多步循环。

    v0.38: 使用 ToolExecution 批次事务 + ToolPipeline 五步管道替代裸调
    dispatcher.dispatch()。Function Calling 格式优先，XML 正则降级保底。

    v0.44: 流式响应。通过 on_stream_event 回调发射 text_delta / toolcall_start /
    toolcall_delta / stream_recovery 事件。流中断时恢复（最多 1 次），
    恢复用尽后降级为同步 chat()。
    """
    session = process.session
    if session is None:
        return _error_result(process, "NO_SESSION")

    # B 级 Agent 由 fork 置为 WAITING；真正开始执行时转入 RUNNING。
    if process.status == ProcessStatus.WAITING:
        process.status = ProcessStatus.RUNNING

    if process.status != ProcessStatus.RUNNING:
        return _status_result(process, session)

    # ── v0.38: 构建运行时环境 ──
    # daemon 已将 tool_executors 注册为 AgentTool 实例，直接使用
    tools_dict: dict[str, AgentTool] = (
        dispatcher._executors if dispatcher else {}
    )
    event_bus = EventBus()
    ctx = ExecutionContext(
        process=process,
        session=session,
        workspace_path=workspace_path,
        event_bus=event_bus,
    )
    transcript = TaskTranscriptBuilder(task_id=process.process_id)

    # v0.38: EventBus 接线 —— Transcript 通过订阅 ExecutionCompleted 自动写入
    def _on_execution_completed(event):
        for r in (event.results or []):
            transcript.append_tool_call(
                process.steps_used, r.tool_name,
                {}, {"allowed": r.allowed, "error": r.error},
                r.duration_ms,
            )

    event_bus.subscribe("ExecutionCompleted", _on_execution_completed)

    # v0.39: 挂 transcript 到 process 上（daemon 的 _build_return_context 需要）
    process._transcript_builder = transcript

    # 注入工具 prompt（首次，含 FC 格式 + XML 降级说明）
    if tools_dict:
        _inject_tool_prompt(session, tools_dict, process)

    # 追加用户指令
    if instruction:
        session.append_user(instruction)
    elif not session.messages:
        return _error_result(process, "EMPTY_INSTRUCTION")

    # ── 治理信号初始化 ──
    harness = process.context_snapshot or {}
    signals = harness.get("signals")
    _use_signal_bus = signals is not None

    _signal_bus = None
    if _use_signal_bus:
        from backend.core.loop.signal_bus import SignalBus
        _signal_bus = (
            SignalBus.from_contract(workspace_path) if workspace_path
            else SignalBus()
        )
        _pre_result = _signal_bus.dispatch(signals, process, context="pre_dispatch")
        if _pre_result.suggestions:
            for sug in _pre_result.suggestions[:3]:
                session.append_user(f"[治理建议] {sug}")

    # ── 多步循环 ──
    _stream_recoveries = 0  # v0.44: 流中断恢复计数（局部变量，非 AgentProcess 字段）
    while process.steps_used < process.max_steps:
        # ── 取消检查（kill 置位 cancel_requested，真停线程）──
        if process.cancel_requested:
            process.status = ProcessStatus.KILLED
            return _status_result(process, session)

        # ── 上下文窗口检查（v0.39: manage_context 五级压缩链）──
        window_check = _context_window.check(session)
        if window_check["action"] == "soft_warn":
            pass  # 仅通知，不压缩
        elif window_check["action"] in ("prune", "force_compact"):
            # v0.39: 替代裸调 prune/compact，使用五级优先级链
            need_compact = manage_context(
                session, harness,
                llm_provider if window_check["action"] == "force_compact" else None,
            )
            if need_compact and window_check["action"] == "force_compact":
                retention_suggestions = None
                if _use_signal_bus and _signal_bus is not None:
                    retention_result = _signal_bus.dispatch(
                        signals, process, context="retention",
                    )
                    retention_suggestions = retention_result.suggestions or None
                _context_window.compact(session, llm_provider, harness_data=harness,
                                        retention_suggestions=retention_suggestions)

        # ── v0.44: 流式 LLM 调用 ──
        tools_param = build_tools_json(tools_dict) if tools_dict else None
        accumulated_text = ""
        pending_tool_calls: dict[int, dict] = {}
        start = time.time()
        content = ""
        tool_calls: list[dict] = []

        try:
            for chunk in llm_provider.stream_chat(
                session.to_openai_messages(),
                tools=tools_param,
            ):
                delta = chunk.get("choices", [{}])[0].get("delta", {})

                # ── 文本 delta ──
                text = delta.get("content", "")
                if text:
                    accumulated_text += text
                    if on_stream_event:
                        on_stream_event({
                            "event": "text_delta",
                            "process_id": process.process_id,
                            "delta": text,
                            "accumulated": accumulated_text,
                            "step": process.steps_used,
                        })

                # ── tool_call delta ──
                for tc in delta.get("tool_calls", []):
                    idx = tc.get("index", 0)
                    if idx not in pending_tool_calls:
                        pending_tool_calls[idx] = {
                            "id": tc.get("id", ""),
                            "name": tc.get("function", {}).get("name", ""),
                            "partial_json": "",
                        }
                        if on_stream_event:
                            on_stream_event({
                                "event": "toolcall_start",
                                "process_id": process.process_id,
                                "tool_call_id": pending_tool_calls[idx]["id"],
                                "tool_name": pending_tool_calls[idx]["name"],
                            })

                    args_fragment = tc.get("function", {}).get("arguments", "")
                    if args_fragment:
                        pending_tool_calls[idx]["partial_json"] += args_fragment
                        if on_stream_event:
                            on_stream_event({
                                "event": "toolcall_delta",
                                "process_id": process.process_id,
                                "tool_call_id": pending_tool_calls[idx]["id"],
                                "delta": args_fragment,
                                "partial_json": pending_tool_calls[idx]["partial_json"],
                            })

            # 流成功完成
            duration_ms = (time.time() - start) * 1000
            _stream_recoveries = 0

            # ── 转换 pending_tool_calls → tool_calls ──
            for idx in sorted(pending_tool_calls.keys()):
                p = pending_tool_calls[idx]
                try:
                    args = json.loads(p["partial_json"]) if p["partial_json"].strip() else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({
                    "name": p["name"],
                    "id": p["id"],
                    "args": args,
                })

            content = accumulated_text
            if content:
                session.append_assistant(content)
            process.steps_used += 1

        except Exception as exc:
            from backend.core.loop.llm import StreamInterruptedError
            if isinstance(exc, StreamInterruptedError):
                # ── 流中断恢复 ──
                if _stream_recoveries < MAX_STREAM_RECOVERIES:
                    _stream_recoveries += 1

                    if accumulated_text:
                        session.append_assistant(accumulated_text)
                    session.append_user(
                        _stream_recovery_message(
                            bool(accumulated_text),
                            bool(pending_tool_calls),
                        ),
                    )

                    if on_stream_event:
                        on_stream_event({
                            "event": "stream_recovery",
                            "process_id": process.process_id,
                            "attempt": _stream_recoveries,
                            "max": MAX_STREAM_RECOVERIES,
                        })

                    # 不修改 steps_used —— 流中断时尚未递增
                    continue  # 重试本轮

                # 恢复次数用尽 → 降级为同步 chat()
                try:
                    response = llm_provider.chat(
                        session.to_openai_messages(),
                        tools=tools_param,
                    )
                    duration_ms = (time.time() - start) * 1000
                    if isinstance(response, dict):
                        content = response.get("content", "") or ""
                        tool_calls = parse_tool_calls(response)
                    else:
                        content = response or ""
                        tool_calls = parse_tool_calls(content)
                    if content:
                        session.append_assistant(content)
                    process.steps_used += 1
                    _stream_recoveries = 0
                except Exception as chat_exc:
                    process.status = ProcessStatus.KILLED
                    return _make_result(process, session, "", duration_ms=0,
                                      error=f"stream_fallback_failed: {chat_exc}",
                                      status_override="killed")
            else:
                # 非流中断异常 → 直接 KILLED
                process.status = ProcessStatus.KILLED
                return _make_result(process, session, "", duration_ms=0,
                                  error=str(exc), status_override="killed")

        # ── 工具调用 → ToolExecution 批次事务 ──
        if tool_calls:
            # 治理预检（保留在 executor，基于 lesson_triggers/contract_drift）
            filtered_calls = []
            for tc in tool_calls:
                tool_name = tc["name"]
                tool_args = tc.get("args", {})

                # v0.45: Storm break
                storm_nudge = _loop_guard.check_storm_break(tool_name)
                if storm_nudge:
                    session.append_user(storm_nudge, message_type="governance_nudge")
                    continue

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
                filtered_calls.append(tc)

            if filtered_calls:
                import uuid
                execution = ToolExecution(
                    execution_id=str(uuid.uuid4()),
                    ctx=ctx,
                    tool_calls=filtered_calls,
                )
                execution.begin()
                results = execution.execute_batch(tools_dict)

                # v0.45: 检查是否触发回滚
                if execution._rolled_back:
                    # Rollback 已处理：文件恢复 + 会话裁剪 + 回滚通知已注入
                    # 发射 rollback_notification 给 Dashboard
                    if on_stream_event:
                        on_stream_event({
                            "event": "rollback_notification",
                            "process_id": process.process_id,
                            "execution_id": execution.execution_id,
                        })
                    continue  # 跳到下一轮循环，让 LLM 重新尝试

                execution.commit()

                for r in results:
                    ref_files = _extract_referenced_files(r.tool_name,
                        r.data if r.data else {})
                    session.append_user(
                        r.formatted,
                        message_type="tool_result",
                        referenced_files=ref_files,
                    )
                    session.messages[-1]["_tool_name"] = r.tool_name
                    # v0.45: 记录工具错误到 LoopGuard（storm break 追踪）
                    if r.is_error:
                        error_code = _extract_error_code(r)
                        _loop_guard.record_tool_error(r.tool_name, error_code)

            _track_step(process, content[:200])
            _promote_mid_task_constraints(session, process)
            continue

        # ── 检查完成 ──
        guard_result = _loop_guard.check(
            process, content, session, _signal_bus, signals,
        )
        if guard_result.is_complete:
            process.status = ProcessStatus.COMPLETED
            process.result = {"response": content, "steps_used": process.steps_used}
            return _make_result(process, session, content,
                              duration_ms=duration_ms,
                              window_action=window_check["action"],
                              window_usage_ratio=window_check["usage_ratio"])
        if guard_result.blocked:
            _inc_nudge_counter(process,
                "required_tools" if "工具" in guard_result.nudge_text else "rejection")
            if _get_nudge_count(process, "required_tools") >= 3 or \
               _get_nudge_count(process, "rejection") >= 3:
                process.status = ProcessStatus.KILLED
                return _make_result(process, session, content,
                                  error="nudge_escalation",
                                  status_override="failed")
            session.append_user(
                guard_result.nudge_text,
                message_type="governance_nudge",
            )
            continue

        # ── 普通文本（无 tool_call 无 TASK_COMPLETE）──
        _track_step(process, content[:200])

        # v0.42: doom_loop 检测已接入 LoopGuard.check()（通过 check_doom_loop_safe）。
        # 普通文本路径中，LoopGuard 在 agent_step 的完成检查分支中调用，
        # 此处保留冗余检测作为最后防线（check_budget_continuity 也在这里）。
        # 如果 LoopGuard missed 了一个 doom_loop（例如 completion markers 出现在非完成上下文中），
        # 这个直接调用是最后的安全网。

        # 纯文本死循环检测
        if _repeated_plain_text(process, content):
            process.status = ProcessStatus.KILLED
            return _make_result(process, session, content,
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
        f"{TOOL_PROMPT_MARKER}\n"
        f"可用工具: {', '.join(available) if available else '无'}\n\n"
        "你可以使用 Function Calling 格式调用工具。"
        "如果不支持 Function Calling，请使用以下 XML 格式：\n"
        "<tool_call>\n"
        "  <name>工具名</name>\n"
        "  <args>{\"key\": \"value\"}</args>\n"
        "</tool_call>\n\n"
        "工具结果将以普通文本追加到对话中。\n"
        "当任务完成时，回复 TASK_COMPLETE 并给出最终结果。"
    )

    prompt = _build_system_message_for_llm(process, prompt)

    if session.messages and session.messages[0].get("role") == "system":
        session.messages[0]["content"] += "\n\n" + prompt
    else:
        session.messages.insert(0, {"role": "system", "content": prompt})


# ── Harness Layer 1: Policy-aware Pre-dispatch ───────────────

def _policy_pre_check(tool_name: str, args: dict, process: AgentProcess) -> dict:
    """工具调用前检查 harness 规则（旧格式兼容路径）。

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

def _extract_referenced_files(tool_name: str, args: dict) -> list[str]:
    """从工具参数中提取涉及的 files。"""
    files = []
    for key in ("file", "path", "files", "target", "source"):
        val = args.get(key)
        if isinstance(val, str) and val:
            files.append(val)
        elif isinstance(val, list):
            files.extend([v for v in val if isinstance(v, str)])
    return files


def _extract_error_code(result) -> str:
    """从 ToolResult 中提取错误码（用于 storm break 追踪）。

    优先取 result.error 中已知的错误码模式，
    否则取 diagnostics 中的 code 字段，
    否则返回 "UNKNOWN"。
    """
    error_text = getattr(result, "error", "") or ""
    diag = getattr(result, "diagnostics", {}) or {}
    code = diag.get("code", "")
    if code:
        return code
    # 尝试从 error 文本中提取已知模式
    known_codes = [
        "TOOL_TIMEOUT", "TOOL_CRASH", "TOOL_NOT_FOUND",
        "FILE_NOT_FOUND", "PERMISSION_DENIED", "NETWORK_ERROR",
    ]
    for kc in known_codes:
        if kc in error_text:
            return kc
    return "TOOL_ERROR"


def _track_step(process: AgentProcess, response_prefix: str) -> None:
    process._step_history.append({
        "tool_name": "llm_call",
        "args": response_prefix,
    })
    if len(process._step_history) > 5:
        process._step_history = process._step_history[-5:]


def _is_completion(response: str) -> bool:
    markers = [
        "任务完成", "分析完成", "处理完成", "执行完成",
        "TASK_COMPLETE", "DONE:", "FINAL_ANSWER:",
    ]
    return any(m in response for m in markers)


def _repeated_plain_text(process: AgentProcess, response: str,
                         threshold: int = 3) -> bool:
    """检测连续纯文本响应是否陷入重复循环。"""
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
    process._nudge_counters[nudge_type] = process._nudge_counters.get(nudge_type, 0) + 1


def _get_nudge_count(process: AgentProcess, nudge_type: str) -> int:
    return process._nudge_counters.get(nudge_type, 0)


# ── v0.36: 中途约束晋升 ──────────────────────────────────

import re as _re

_NEGATIVE_DIRECTIVE_RE = _re.compile(
    r'(?:不要|先别|这次不要|暂时别|do not|don\'t|must not|never)\s*(.{5,120})',
    _re.I,
)


def _has_action_object(text: str) -> bool:
    """检查是否包含明确的动词+名词结构（判别"不要这样" vs "不要改 API"）。"""
    import re
    return bool(re.search(
        r'(?:改|动|删|修改|删除|新建|重构|迁移|调用|update|delete|remove|change|modify|refactor)\s*\w+',
        text, re.I,
    ))


def _promote_mid_task_constraints(session, process: AgentProcess) -> int:
    """从最近 user 消息中检测中途指令 → 晋升为 task 约束。"""
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
                      if not any(q in content.lower() for q in ["lesson", "规则说", "系统提示", "claude说"])]

        for c in candidates:
            if c not in process.task_constraints:
                process.task_constraints.append(c)
                promoted += 1

    return promoted


def _build_system_message_for_llm(process: AgentProcess,
                                   base_system: str) -> str:
    """L2-ext: 动态拼接中途约束到 system message 末尾。"""
    if not getattr(process, 'task_constraints', None):
        return base_system

    constraint_block = "\n\n## Task-level Constraints (this task only)\n" + "\n".join(
        f"- {c}" for c in process.task_constraints
    )
    return base_system + constraint_block
