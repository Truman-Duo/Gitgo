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

from backend.core.loop.execution_contract import HOST_COMPUTE, NATIVE_PROCESS, data_broker

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from backend.core.loop.agent_tool import AgentTool, ApprovalMode, CancellationMode, ToolEffect
from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.completion_protocol import (
    COMPLETE_TASK_PARAMETERS,
    CompletionClaim,
)
from backend.core.loop.event_bus import EventBus
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.llm_adapter import build_tools_json, parse_tool_calls
from backend.core.loop.loop_guard import LoopGuard
from backend.core.loop.models import AgentProcess, ProcessStatus, RingLevel
from backend.core.loop.outcome import OutcomeStatus, TaskError, TaskOutcome
from backend.core.loop.operation_policy import is_effectful_mutation
from backend.core.loop.context_window import ContextWindow, manage_context
from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType
from backend.core.loop.transcript import TaskTranscriptBuilder
from backend.core.loop.tool_execution import ToolExecution
from backend.core.loop.tool_summary import compact_tool_summary

if TYPE_CHECKING:
    from backend.core.loop.llm import LLMProvider
    from backend.core.loop.session import AgentSession
    from backend.core.dispatch.dispatcher import ToolDispatcher

TOOL_PROMPT_MARKER = "## 可用工具 (B Agent Ring 3)"

# ── v0.44: 流中断恢复 ──
MAX_STREAM_RECOVERIES = 1

# Ephemeral current-turn guidance belongs after the append-only conversation
# history.  This preserves the provider-cacheable prefix while giving the
# generation nearest-token access to the public/private output boundary.  It
# is never persisted as conversation or treated as user-authored content.
_BASE_CURRENT_TURN_ENVELOPE = """[HOST CURRENT-TURN ENVELOPE]
Continue the user's task using the available tools when useful. When you produce
the final public answer, include only the requested result, material caveats,
and useful source citations. Do not report internal search/fetch attempts,
blocked or unused pages, HTTP/provider/network diagnostics, retry or validation
counts, process IDs, receipts, governance steps, or completion gates unless the
user explicitly requested a process audit, or a remaining failure materially
prevents the requested result. Internal execution evidence belongs in the
runtime timeline, not in the final answer. Keep the delivery proportionate:
do not enumerate individual test cases, repeat a code walkthrough, or restate
internal acceptance criteria unless the user asked for those details.
For stable general knowledge that you can answer confidently, answer in the
current model turn without calling search merely to demonstrate diligence. If
an optional detail is uncertain, omit or qualify that detail instead of
expanding the user's request into research. Use external search when freshness,
requested citations, or uncertainty about the core answer materially requires
it, and respect an explicit request for a direct answer.
Treat quoted or user-provided text literally unless the user asks you to decode,
repair, or reconstruct hidden source text. If a literal explanation needs
verification, make one bounded tool call promptly instead of spending the
output budget reverse-engineering an unstated transformation.
"""


def _build_current_turn_envelope(
    process: AgentProcess,
    tools: list[dict] | None,
) -> str:
    """Describe the authoritative live tool surface in the ephemeral tail.

    Tool availability changes after contract compilation, lease issuance,
    provider switching and authored-tool mounting.  Historical assistant text
    can therefore contain a once-true claim that a capability was unavailable.
    The provider schemas are authoritative to the Host, but weaker models may
    continue a recent natural-language claim instead of re-reading that schema.
    Put a compact, versioned snapshot at the non-persisted tail so current truth
    wins without rewriting history or invalidating the cacheable prefix.
    """
    from backend.core.loop.task_contract import get_task_contract

    names: list[str] = []
    for item in tools or []:
        function = item.get("function") if isinstance(item, dict) else None
        name = str(
            (function or {}).get("name")
            if isinstance(function, dict)
            else item.get("name", "") if isinstance(item, dict) else ""
        ).strip()
        if name:
            names.append(name)
    names = sorted(set(names))

    contract = get_task_contract(process)
    compilation = dict(contract.get("routing_compilation") or {})
    execution_mode = str(contract.get("execution_mode") or "undeclared")
    next_action = str(compilation.get("next_action") or "continue_current_task")
    lease = getattr(process, "capability_lease", None)
    lease_profile = str(getattr(lease, "profile_id", "") or "")
    name_set = set(names)
    contract_declared = bool(
        contract.get("revision")
        or execution_mode in {"answer", "self_execute", "delegate", "either"}
    )
    if "declare_task_contract" in name_set and not contract_declared:
        surface_phase = "contract_admission"
        next_action = (
            "classify_the_user_request; if it requires execution, workspace "
            "changes, delegation, tests, or multi-step work, call "
            "declare_task_contract now; otherwise answer directly"
        )
        phase_guidance = (
            "This pre-contract surface keeps safe read-only evidence and public-research "
            "tools available. Missing write, command, test or delegation tools at this "
            "phase is not evidence that Gitgo cannot perform the task. Declare the "
            "semantic contract before protected work and the Host will rebuild the "
            "appropriate surface."
        )
    elif "request_self_execute" in name_set and not lease_profile:
        surface_phase = "lease_admission"
        phase_guidance = (
            "The Host accepted a self-execute contract but has not issued its "
            "task-scoped lease. Call request_self_execute before attempting or "
            "describing the protected work."
        )
    else:
        surface_phase = "active_execution"
        phase_guidance = (
            "Use the listed live tools as needed and do not rely on older "
            "capability claims from the conversation."
        )
    surface_version = ":".join((
        str(getattr(process, "context_version", 0) or 0),
        str(contract.get("revision", 0) or 0),
        str(process.task_kind or "answer"),
        lease_profile or "pre-lease",
    ))
    available = ", ".join(names) if names else "(none)"
    return (
        _BASE_CURRENT_TURN_ENVELOPE.rstrip()
        + "\n\n[HOST AUTHORITATIVE CAPABILITY SNAPSHOT]\n"
        + f"surface_version: {surface_version}\n"
        + f"task_kind: {process.task_kind}\n"
        + f"execution_mode: {execution_mode}\n"
        + f"surface_phase: {surface_phase}\n"
        + f"lease: {lease_profile or 'not-active'}\n"
        + f"next_host_action: {next_action}\n"
        + f"available_tools: {available}\n"
        + "This snapshot is the current Host truth and supersedes earlier "
        + "conversation claims about tool availability. A listed tool is "
        + "callable under its normal policy and approval rules; do not claim it "
        + "is physically unavailable. A missing tool is unavailable in this "
        + "turn. If next_host_action names an available protocol tool, call it "
        + "before attempting the protected work.\n"
        + phase_guidance + "\n"
    )


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


def _bounded_exception_chain(exc: BaseException, limit: int = 400) -> str:
    """Expose a bounded transport diagnostic without leaking request headers."""
    parts = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(parts) < 4:
        seen.add(id(current))
        message = str(current).strip()
        parts.append(type(current).__name__ + (f": {message}" if message else ""))
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)[:max(80, min(limit, 1000))]


def _reasoning_continuation(
    reasoning_content: str,
    reasoning_detail_chunks: list,
) -> dict:
    """Preserve raw provider reasoning without interpreting its authority."""
    continuation = {}
    if reasoning_content:
        continuation["reasoning_content"] = reasoning_content
    if reasoning_detail_chunks:
        if all(isinstance(item, str) for item in reasoning_detail_chunks):
            continuation["reasoning_details"] = "".join(reasoning_detail_chunks)
        else:
            continuation["reasoning_details"] = reasoning_detail_chunks
    return continuation


def _provider_continuation(
    reasoning_content: str,
    reasoning_detail_chunks: list,
    provider_artifacts: list[dict] | None = None,
) -> dict:
    """Build replay state for both normal and interrupted provider streams."""
    continuation = _reasoning_continuation(
        reasoning_content, reasoning_detail_chunks,
    )
    artifacts = list(provider_artifacts or [])
    if not artifacts:
        return continuation
    continuation["provider_artifacts"] = artifacts
    response_items = [
        item["response_output_item"] for item in artifacts
        if isinstance(item, dict) and item.get("response_output_item")
    ]
    anthropic_blocks = [
        item["anthropic_content_block"] for item in artifacts
        if isinstance(item, dict) and item.get("anthropic_content_block")
    ]
    if response_items:
        continuation["response_output_items"] = response_items
    if anthropic_blocks:
        continuation["anthropic_content_blocks"] = anthropic_blocks
    return continuation


def _filter_incomplete_response_artifacts(
    provider_artifacts: list[dict],
    completed_call_ids: set[str],
) -> list[dict]:
    """Drop unanswered native tool calls before replaying provider state.

    The Responses API requires every replayed ``function_call`` to be followed by
    a matching ``function_call_output``; Anthropic has the equivalent invariant
    for ``tool_use``/``tool_result``. An interrupted response can contain a call
    whose wire item is complete even though Gitgo never reached tool execution,
    so only IDs explicitly accepted for execution are replayable. Non-call
    artifacts (reasoning and partial assistant messages) remain replayable.
    """
    filtered: list[dict] = []
    for artifact in provider_artifacts:
        if not isinstance(artifact, dict):
            continue
        response_item = artifact.get("response_output_item")
        if isinstance(response_item, dict) and response_item.get("type") == "function_call":
            call_id = str(response_item.get("call_id", response_item.get("id", "")))
            if call_id and call_id in completed_call_ids:
                filtered.append(artifact)
            continue
        anthropic_block = artifact.get("anthropic_content_block")
        if isinstance(anthropic_block, dict) and anthropic_block.get("type") == "tool_use":
            call_id = str(anthropic_block.get("id", ""))
            if call_id and call_id in completed_call_ids:
                filtered.append(artifact)
            continue
        filtered.append(artifact)
    return filtered


def _iter_provider_events(
    llm_provider,
    session: "AgentSession",
    *,
    tools: list[dict] | None,
    cancel_event,
    cache_intent,
    metadata: dict | None = None,
    timeout: int = 45,
    current_turn_envelope: str = _BASE_CURRENT_TURN_ENVELOPE,
    max_tokens: int | None = None,
):
    """Use canonical events, with a compatibility bridge for test/legacy providers."""
    if hasattr(llm_provider, "stream_events"):
        yield from llm_provider.stream_events(
            session.to_provider_messages(
                dynamic_envelope=current_turn_envelope,
                provider_route=str(getattr(llm_provider, "route_key", "") or ""),
            ), tools=tools,
            cancel_event=cancel_event, cache_intent=cache_intent,
            metadata=metadata,
            timeout=timeout,
            # The loop previously relied on stream_events' 4096-token default
            # even after provider probing had established a larger safe output
            # limit.  Reasoning tokens and long tool arguments share this
            # budget, so the hidden default could truncate ordinary writes.
            max_tokens=max(1, int(max_tokens or getattr(
                llm_provider, "max_output_tokens", 4096,
            ))),
        )
        return
    started: set[int] = set()
    for chunk in llm_provider.stream_chat(
        session.to_openai_messages(
            dynamic_envelope=current_turn_envelope,
            provider_route=str(getattr(llm_provider, "route_key", "") or ""),
        ),
        tools=tools, cancel_event=cancel_event,
        timeout=timeout,
        max_tokens=max(1, int(max_tokens or getattr(
            llm_provider, "max_output_tokens", 4096,
        ))),
    ):
        if chunk.get("usage"):
            from backend.core.loop.provider_protocol import normalize_usage
            yield ProviderEvent(
                ProviderEventType.USAGE, usage=normalize_usage(chunk["usage"]),
            )
        for choice in chunk.get("choices", []) or []:
            delta = choice.get("delta", {}) or {}
            if delta.get("content"):
                yield ProviderEvent(
                    ProviderEventType.TEXT_DELTA, text=str(delta["content"]),
                )
            if delta.get("reasoning_content"):
                yield ProviderEvent(
                    ProviderEventType.REASONING_DELTA,
                    reasoning=str(delta["reasoning_content"]),
                )
            if delta.get("reasoning_details") is not None:
                yield ProviderEvent(
                    ProviderEventType.REASONING_ARTIFACT,
                    artifact={"reasoning_details": delta["reasoning_details"]},
                )
            for call in delta.get("tool_calls", []) or []:
                index = int(call.get("index", 0) or 0)
                function = call.get("function", {}) or {}
                if index not in started:
                    started.add(index)
                    yield ProviderEvent(
                        ProviderEventType.TOOL_CALL_STARTED,
                        tool_call_id=str(call.get("id", "")),
                        tool_name=str(function.get("name", "")),
                        output_index=index,
                    )
                if function.get("arguments"):
                    yield ProviderEvent(
                        ProviderEventType.TOOL_CALL_DELTA,
                        tool_call_id=str(call.get("id", "")),
                        tool_name=str(function.get("name", "")),
                        arguments_delta=str(function["arguments"]),
                        output_index=index,
                    )
    yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)


def _provider_idle_timeout(process, *, cap_seconds: int = 120) -> int:
    """Bound semantic inactivity without limiting a productive long stream.

    The transport renews this allowance only for actual SSE data, not comment
    heartbeats. Reasoning can therefore stream for more than five minutes;
    a connection producing no model data for two minutes enters recovery.
    """
    budget = getattr(process, "task_budget", None)
    if budget is None:
        return cap_seconds
    remaining = float(budget.remaining_seconds())
    # Keep a small tail for cancellation, outcome persistence and A review.
    usable = max(1.0, remaining)
    return max(1, min(cap_seconds, int(usable)))


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
        process.status = ProcessStatus.FAILED
        return _error_result(
            process, "NO_SESSION", "Agent runtime has no session",
        )

    if process.cancel_requested:
        process.status = ProcessStatus.CANCELLED
        return _status_result(process, session)

    # B 级 Agent 由 fork 置为 WAITING；真正开始执行时转入 RUNNING。
    if process.status == ProcessStatus.WAITING:
        process.status = ProcessStatus.RUNNING

    if process.status != ProcessStatus.RUNNING:
        return _status_result(process, session)

    provider_route = str(getattr(llm_provider, "route_key", "") or "")
    provider_route_changed = session.bind_provider_route(provider_route)

    # A submitted round includes every provider/tool/recovery step, not only
    # the last provider request. Monotonic timing is deliberately not restored.
    process._turn_started_monotonic = time.monotonic()
    turn_wall_started = time.time()

    # The runtime provider is authoritative.  Commands restored from older
    # checkpoints may not carry model/provider metadata, which previously made
    # the prompt claim that an actually configured model was "not exposed".
    process.model_id = str(
        getattr(llm_provider, "_model", "") or process.model_id or "unknown"
    )
    provider_protocol = getattr(llm_provider, "protocol", "unknown")
    process.runtime_preferences["provider_protocol"] = str(
        getattr(provider_protocol, "value", provider_protocol) or "unknown"
    )
    provider_capabilities = getattr(llm_provider, "capabilities", None)
    process.runtime_preferences["provider_capabilities"] = (
        provider_capabilities.to_dict()
        if hasattr(provider_capabilities, "to_dict") else {}
    )

    # ── v0.38: 构建运行时环境 ──
    # Capability is the single source for prompt, FC schema, and RingGate.
    dispatcher_tools: dict[str, AgentTool] = (
        dispatcher._executors if dispatcher else {}
    )
    internal_tools = _build_internal_tools(
        process, dispatcher_tools, workspace_path, llm_provider, dispatcher,
        on_stream_event=on_stream_event,
    )
    all_tools = {**dispatcher_tools, **process.dynamic_tools, **internal_tools}
    tools_dict, unavailable_tools = _select_authorized_tools(
        process, all_tools,
    )
    if unavailable_tools:
        process.status = ProcessStatus.FAILED
        return _error_result(
            process,
            "CAPABILITY_TOOL_UNAVAILABLE",
            "Capability references unavailable tools: "
            + ", ".join(unavailable_tools),
        )
    def _notify_event_failure(failure):
        from backend.core.loop.engineering_workflow import EngineeringWorkflow
        EngineeringWorkflow(process, on_stream_event).notice(
            "EVENT_DELIVERY_FAILED", "内部事件的一位订阅者处理失败；其他订阅者继续运行，失败已记录。", **failure,
        )

    event_bus = EventBus(on_error=_notify_event_failure)
    ctx = ExecutionContext(
        process=process,
        session=session,
        workspace_path=workspace_path,
        event_bus=event_bus,
        cancellation=process.cancellation_event,
        storage=process.bound_storage,
    )
    ctx.artifacts["tool_catalog"] = tools_dict
    from backend.core.loop.engineering_workflow import EngineeringWorkflow
    ctx.artifacts["engineering_workflow"] = EngineeringWorkflow(process, on_stream_event)
    transcript = TaskTranscriptBuilder(task_id=process.process_id)
    loop_guard = LoopGuard()
    incomplete_tool_call_counts: dict[str, int] = {}

    # v0.38: EventBus 接线 —— Transcript 通过订阅 ExecutionCompleted 自动写入
    def _on_execution_completed(event):
        for r in (event.results or []):
            transcript.append_tool_call(
                process.steps_used, r.tool_name,
                {}, {"allowed": r.allowed, "error": r.error},
                r.duration_ms,
            )

    event_bus.subscribe("ExecutionCompleted", _on_execution_completed)

    def _on_composite_step(event):
        if on_stream_event is None:
            return
        on_stream_event({
            "event": "composite_step_result",
            "process_id": process.process_id,
            "execution_id": event.execution_id,
            "tool_name": event.tool_name,
            **dict(event.data or {}),
        })

    event_bus.subscribe("CompositeStepCompleted", _on_composite_step)

    # v0.39: 挂 transcript 到 process 上（daemon 的 _build_return_context 需要）
    process._transcript_builder = transcript

    # Provider-neutral prompt is compiled from the effective capability set.
    _refresh_compiled_prompt(session, process, tools_dict, workspace_path)
    session.model_context_limit = max(
        1024, int(getattr(llm_provider, "context_window", 128000)),
    )
    context_window = ContextWindow(session.model_context_limit)

    # Provider identity is durable Host state, not a fact that needs to consume
    # prompt tokens on every request.  Append one model-visible event only when
    # the non-secret route (protocol/base/model) actually changes.  New sessions
    # already receive the current identity in their compiled ROM; old sessions
    # receive ROM deltas through PromptCompiler.upsert.
    if provider_route_changed:
        from backend.core.loop.capability_negotiation import record_provider_changed
        record_provider_changed(
            session,
            model_id=process.model_id,
            protocol=str(process.runtime_preferences.get("provider_protocol") or "unknown"),
            provider_route=provider_route,
        )

    # 追加用户指令
    if instruction:
        session.append_user(instruction, message_type="conversation")
    elif not session.messages:
        process.status = ProcessStatus.FAILED
        return _error_result(
            process, "EMPTY_INSTRUCTION", "Agent task has no instruction",
        )

    # ── 治理信号初始化 ──
    harness, governance_version = process.read_context_snapshot()
    raw_signals = harness.get("signals")
    if raw_signals is not None:
        from backend.core.loop.signals import normalize_governance_signals
        signals = normalize_governance_signals(raw_signals)
    else:
        signals = None
    _use_signal_bus = signals is not None

    def _emit_observation(event: dict, *, detail=None) -> None:
        if on_stream_event is None:
            return
        payload = dict(event)
        payload.setdefault("process_id", process.process_id)
        payload.setdefault("step", process.steps_used + 1)
        if detail is not None:
            payload["_trace_detail"] = detail
        on_stream_event(payload)

    def _on_tool_notice(event):
        _emit_observation({"event": "progress_summary", "phase": "tool_notice", **dict(event.data or {})})

    event_bus.subscribe("ToolNotice", _on_tool_notice)

    if provider_route_changed:
        _emit_observation({
            "event": "provider_route_changed",
            "provider_route": provider_route,
            "continuation_policy": "public_history_only",
        })

    _emit_observation({
        "event": "governance_snapshot",
        "governance_version": governance_version,
        "signal_count": len(signals or []),
        "phase": "admission",
    }, detail=harness)

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

    def _refresh_governance_snapshot() -> None:
        nonlocal harness, governance_version, signals, _use_signal_bus, _signal_bus
        current, current_version = process.read_context_snapshot()
        if current_version == governance_version:
            return
        harness = current
        governance_version = current_version
        raw_signals = harness.get("signals")
        if raw_signals is not None:
            from backend.core.loop.signals import normalize_governance_signals
            signals = normalize_governance_signals(raw_signals)
        else:
            signals = None
        _use_signal_bus = signals is not None
        if _use_signal_bus and _signal_bus is None:
            from backend.core.loop.signal_bus import SignalBus
            _signal_bus = (
                SignalBus.from_contract(workspace_path) if workspace_path
                else SignalBus()
            )
        _emit_observation({
            "event": "governance_snapshot",
            "governance_version": governance_version,
            "signal_count": len(signals or []),
            "phase": "turn_boundary",
        }, detail=harness)

    def _authorize_tool(tool_name: str, tool_args: dict) -> dict:
        _refresh_governance_snapshot()
        if _use_signal_bus and _signal_bus is not None:
            return _signal_bus.check_tool(
                tool_name, tool_args, process, signals=signals,
            )
        return _policy_pre_check(tool_name, tool_args, process)

    ctx.tool_authorizer = _authorize_tool

    def _commit_tool_results(tool_calls: list[dict], results: list, *, rolled_back: bool) -> None:
        """One provider-continuity and audit path for live and resumed calls."""
        results.sort(key=lambda item: int(item.call_index))
        # A provider may issue several independent calls to the same tool in
        # one response (for example, three different files).  A shared Host
        # precondition can make all of them fail identically, but that is one
        # failed strategy turn, not three retry attempts.  Count each
        # (tool,error) at most once per batch so parallel work cannot trip the
        # cross-turn storm guard immediately.  Different arguments are
        # different recovery strategies and must remain independently visible.
        recorded_storm_errors: set[tuple[str, str, str]] = set()
        for r in results:
            tool_call = tool_calls[r.call_index]
            ref_files = _extract_referenced_files(
                r.tool_name, r.data if r.data else {},
            )
            session.append_tool_result(
                r.formatted,
                tool_name=r.tool_name,
                tool_call_id=str(tool_call.get("id", "")),
                is_error=r.is_error,
                data=r.data,
                receipt=r.receipt,
                referenced_files=ref_files,
            )
            process.tool_receipts.append(dict(r.receipt or {}))
            _emit_observation({
                "event": "tool_result",
                "tool_name": r.tool_name,
                "tool_call_id": str(tool_call.get("id", "")),
                "is_error": r.is_error,
                "error": r.error,
                "duration_ms": r.duration_ms,
                "result_preview": str(r.formatted)[:2000],
                "compact_summary": compact_tool_summary(
                    r.tool_name, r.data, is_error=r.is_error,
                    error=str(r.error or ""),
                ),
                "diff": str((r.data or {}).get("diff") or "")[:100000],
                "receipt": dict(r.receipt or {}),
                "diagnostics": dict(r.diagnostics or {}),
                "tool_result": dict(r.spill or {}),
                "rolled_back": rolled_back,
            }, detail={
                "arguments": tool_call.get("args", {}),
                "formatted_result": r.formatted,
                "data": r.data,
                "receipt": dict(r.receipt or {}),
                "diagnostics": dict(r.diagnostics or {}),
            })
            tool = tools_dict.get(r.tool_name)
            effect = getattr(getattr(tool, "effect", "read"), "value", "read")
            loop_guard.record_tool_result(
                r.tool_name, tool_call.get("args", {}), r.is_error,
                effect=effect,
            )
            from backend.core.loop.operation_policy import is_effectful_mutation
            if not r.is_error and is_effectful_mutation(effect):
                process.successful_actions += 1
                if process.parent_id:
                    try:
                        from backend.core.loop.coordination import observe_interface_changes
                        manager = getattr(process, "_manager", None)
                        if manager is not None:
                            observed = observe_interface_changes(
                                manager, process, workspace_path,
                                summary=(
                                    f"Host observed declared interface changes after "
                                    f"{r.tool_name}."
                                ),
                            )
                            for coordination_event in observed.get("events", []):
                                _emit_observation({
                                    "event": "coordination_event",
                                    **coordination_event,
                                }, detail=coordination_event)
                    except (OSError, ValueError) as exc:
                        _emit_observation({
                            "event": "coordination_observation_failed",
                            "tool_name": r.tool_name,
                            "error": str(exc),
                        })
            if r.is_error:
                error_code = _extract_error_code(r)
                error_key = loop_guard.storm_key(
                    r.tool_name, error_code, tool_call.get("args", {}),
                )
                if error_key not in recorded_storm_errors:
                    loop_guard.record_tool_error(
                        r.tool_name, error_code, tool_call.get("args", {}),
                    )
                    recorded_storm_errors.add(error_key)

        ctx.artifacts["engineering_workflow"].invoke({"operation": "status"})

    def _missing_permission(calls: list[dict]):
        """Return the first sensitive call that still lacks an exact grant."""
        from backend.core.loop.operation_policy import decide_tool_operation
        from backend.core.loop.permission_broker import matching_grant
        for call_index, call in enumerate(calls):
            tool = tools_dict.get(str(call.get("name") or ""))
            if tool is None:
                continue
            public_args = {
                key: value for key, value in dict(call.get("args") or {}).items()
                if not str(key).startswith("_")
            }
            policy = decide_tool_operation(tool, public_args)
            if policy.requires_user and matching_grant(
                process, tool.name, public_args,
                per_invocation=bool(getattr(tool, "approval_per_invocation", False)), tool=tool,
            ) is None:
                return call_index, tool, public_args
        return None

    def _suspend_tool_batch_for_permission(
        calls: list[dict], execution_id: str, permission_call_index: int,
        tool, public_args: dict,
    ):
        """Persist the unstarted suffix before presenting its permission card."""
        from backend.core.loop.permission_broker import automatic_permission_request
        decision = automatic_permission_request(
            process, tool, public_args, workspace_path,
        )
        process.pending_tool_batch = {
            "execution_id": execution_id,
            "tool_calls": [dict(item) for item in calls],
            "permission_call_index": int(permission_call_index),
            "decision_id": decision.get("decision_id", ""),
            "created_at": time.time(),
        }
        process.status = ProcessStatus.AWAITING_USER
        _emit_observation({
            "event": "decision_required",
            "decision": decision,
        }, detail={"suspended_tool_calls": calls})
        from backend.core.loop.decision_support import format_decision_request
        result = _make_result(
            process, session, format_decision_request(decision),
            duration_ms=(time.time() - turn_wall_started) * 1000,
            outcome_status=OutcomeStatus.AWAITING_USER,
        )
        process.result = result
        return result

    def _execute_approved_segment(calls: list[dict], execution_id: str) -> list:
        """Execute and commit a permission-complete ordered batch segment."""
        if not calls:
            return []
        execution = ToolExecution(execution_id=execution_id, ctx=ctx, tool_calls=calls)
        execution.begin()
        results = execution.execute_batch(tools_dict)
        if not execution._rolled_back:
            execution.commit()
        returned = {int(item.call_index) for item in results}
        for index, call in enumerate(calls):
            if index not in returned:
                results.append(_host_tool_error_result(
                    tool_name=str(call.get("name") or "unknown"),
                    execution_id=execution_id,
                    call_index=index,
                    error_name="TOOL_NOT_STARTED",
                    details={"execution_state": "not_started"},
                    next_actions=[{"action": "inspect_then_retry"}],
                ))
        _commit_tool_results(calls, results, rolled_back=bool(execution._rolled_back))
        if not execution._rolled_back:
            _apply_host_task_transitions(process, results)
        return results

    def _execute_resumed_batch(batch: dict):
        calls = [dict(item) for item in list(batch.get("tool_calls") or [])]
        execution_id = str(batch.get("execution_id") or "permission-resume")
        action = str(batch.get("decision_action") or "")
        if action == "deny":
            denied_index = min(
                max(0, int(batch.get("permission_call_index") or 0)),
                max(0, len(calls) - 1),
            )
            _execute_approved_segment(calls[:denied_index], execution_id)
            denied_call = calls[denied_index:denied_index + 1]
            if denied_call:
                denied = _host_tool_error_result(
                    tool_name=str(denied_call[0].get("name") or "unknown"),
                    execution_id=execution_id,
                    call_index=0,
                    error_name="TOOL_PERMISSION_DENIED",
                    details={"execution_state": "not_started", "user_decision": "deny"},
                    next_actions=[{"action": "change_strategy"}],
                )
                _commit_tool_results(denied_call, [denied], rolled_back=False)
            calls = calls[denied_index + 1:]
            if not calls:
                return None

        missing = _missing_permission(calls)
        if missing is not None:
            missing_index, tool, public_args = missing
            # Calls before the next approval boundary are already authorized;
            # commit them now, then pause before the first unapproved effect.
            _execute_approved_segment(calls[:missing_index], execution_id)
            remaining = calls[missing_index:]
            return _suspend_tool_batch_for_permission(
                remaining, execution_id, 0, tool, public_args,
            )
        _execute_approved_segment(calls, execution_id)
        return None

    def _apply_mailbox_at_boundary() -> int:
        applied = _apply_pending_instructions(process, session)
        if applied:
            _emit_observation({
                "event": "mailbox_applied",
                "applied_count": applied,
            })
        return applied

    def _run_context_compaction(
        trigger: str, *, force: bool = False, failure_required: bool = False,
        retention_suggestions: list[str] | None = None,
    ) -> tuple[bool, dict | None]:
        """Run one Host-accounted compaction and possibly pause for consent."""
        previous_epoch = session.context_epoch
        if force:
            changed = context_window.force_compact(session, reason=trigger)
        else:
            changed = context_window.compact(
                session, llm_provider, harness_data=harness,
                retention_suggestions=retention_suggestions,
                task_budget=process.task_budget,
                cancel_event=process.cancellation_event,
                budget_process_id=process.process_id,
            )
        reason = str(context_window.last_compaction_error or "")
        no_op = reason == "not_enough_foldable_history" and not failure_required
        if changed:
            session.compaction_failure_count = 0
            session.last_compaction_error = ""
            _emit_observation({
                "event": "context_compaction_completed",
                "trigger": trigger,
                "forced": force,
                "previous_epoch": previous_epoch,
                "context_epoch": session.context_epoch,
                "changed": True,
            }, detail=(session.epoch_archive[-1] if session.epoch_archive else None))
            return True, None
        if no_op:
            _emit_observation({
                "event": "context_compaction_completed",
                "trigger": trigger,
                "forced": force,
                "previous_epoch": previous_epoch,
                "context_epoch": session.context_epoch,
                "changed": False,
                "reason": reason,
            })
            return False, None

        if force:
            process.status = ProcessStatus.FAILED
            return False, _make_result(
                process, session, "The immutable prompt prefix does not fit the configured window.",
                error_code="CONTEXT_LIMIT_CONFIGURATION_INVALID",
                error_message=reason or "Forced compaction could not produce a valid epoch",
            )
        session.compaction_failure_count += 1
        session.last_compaction_error = reason or "compaction_returned_no_epoch"
        from backend.core.errors import error_payload
        failure = error_payload(
            "CONTEXT_COMPACTION_FAILED",
            details={
                "trigger": trigger,
                "attempt": session.compaction_failure_count,
                "max_attempts": 3,
                "reason": session.last_compaction_error,
            },
            next_actions=[{
                "action": "retry_compaction",
                "effect": "Retry ordinary non-destructive compaction.",
            }],
        )
        _emit_observation({
            "event": "context_compaction_failed",
            "trigger": trigger,
            "forced": force,
            "attempt": session.compaction_failure_count,
            "max_attempts": 3,
            "error": failure["error_info"],
        })
        if session.compaction_failure_count < 3:
            return False, None

        from backend.core.loop.decision_support import (
            create_context_force_compaction_decision,
            format_decision_request,
        )
        decision = create_context_force_compaction_decision(process)
        process.status = ProcessStatus.AWAITING_USER
        if on_stream_event:
            on_stream_event({
                "event": "decision_required",
                "process_id": process.process_id,
                "decision": decision,
            })
        result = _make_result(
            process, session, format_decision_request(decision),
            outcome_status=OutcomeStatus.AWAITING_USER,
            error_code="CONTEXT_FORCE_COMPACTION_REQUIRED",
            error_message="Three ordinary context compactions failed",
        )
        process.result = result
        return False, result

    # Permission decisions resume the exact provider function call before any
    # new provider turn. This preserves protocol ordering and avoids spending a
    # model step reconstructing arguments that the Host already has durably.
    if isinstance(process.pending_tool_batch, dict):
        pending_batch = dict(process.pending_tool_batch)
        process.pending_tool_batch = None
        resumed_terminal = _execute_resumed_batch(pending_batch)
        if resumed_terminal is not None:
            return resumed_terminal

    # ── 多步循环 ──
    _stream_recoveries = 0  # v0.44: 流中断恢复计数（局部变量，非 AgentProcess 字段）
    _no_progress_recoveries = 0
    while process.steps_used < process.max_steps:
        _refresh_governance_snapshot()
        # ── 取消检查（kill 置位 cancel_requested，真停线程）──
        if process.cancel_requested:
            return _cancellation_result(process, session)

        if process.task_budget is not None:
            try:
                process.task_budget.check_deadline()
            except Exception as exc:
                from backend.core.loop.budget import TaskBudgetExceeded
                if isinstance(exc, TaskBudgetExceeded):
                    return _budget_exhausted_result(process, session, exc)
                raise

        _apply_mailbox_at_boundary()

        if session.context_abort_requested:
            session.context_abort_requested = False
            process.status = ProcessStatus.FAILED
            return _make_result(
                process, session,
                "The task stopped safely because forced context compaction was declined.",
                error_code="CONTEXT_FORCE_COMPACTION_DECLINED",
                error_message="User declined lossy provider-visible compaction",
                outcome_status=OutcomeStatus.FAILED,
            )

        if session.force_compact_requested:
            session.force_compact_requested = False
            _changed, terminal = _run_context_compaction(
                "user_approved_force_compact", force=True, failure_required=True,
            )
            if terminal is not None:
                return terminal

        # Manual compaction is a durable session request, consumed exactly once
        # at a provider-safe turn boundary. It is intentionally not performed
        # by the Dashboard/native-host thread while a provider call may be live.
        if session.manual_compact_requested:
            try:
                _changed, terminal = _run_context_compaction("manual")
            finally:
                session.manual_compact_requested = False
            if terminal is not None:
                return terminal

        # ── 上下文窗口检查（v0.39: manage_context 五级压缩链）──
        window_check = context_window.check(session)
        auto_compact = bool(process.runtime_preferences.get("auto_compact", True))
        if window_check["action"] != "none":
            _emit_observation({
                "event": "context_window_action",
                **window_check,
                "context_epoch": session.context_epoch,
            })
        if window_check["action"] == "soft_warn":
            pass  # 仅通知，不压缩
        elif window_check["action"] == "force_compact" and not auto_compact:
            process.status = ProcessStatus.FAILED
            return _make_result(
                process, session,
                "Automatic compaction is disabled. Run /compact and retry the task.",
                error_code="MANUAL_COMPACTION_REQUIRED",
                error_message=(
                    "The context reached its hard watermark while automatic "
                    "compaction was disabled"
                ),
                window_action="force_compact",
                window_usage_ratio=float(window_check.get("usage_ratio", 0.0) or 0.0),
            )
        elif window_check["action"] in ("prune", "force_compact") and auto_compact:
            # v0.39: 替代裸调 prune/compact，使用五级优先级链
            need_compact = manage_context(
                session, harness,
                llm_provider if window_check["action"] == "force_compact" else None,
                model_token_limit=session.model_context_limit,
            )
            if need_compact and window_check["action"] == "force_compact":
                retention_suggestions = None
                if _use_signal_bus and _signal_bus is not None:
                    retention_result = _signal_bus.dispatch(
                        signals, process, context="retention",
                    )
                    retention_suggestions = retention_result.suggestions or None
                try:
                    _changed, terminal = _run_context_compaction(
                        "automatic_force_watermark",
                        failure_required=True,
                        retention_suggestions=retention_suggestions,
                    )
                    if terminal is not None:
                        return terminal
                except Exception as exc:
                    from backend.core.loop.budget import TaskBudgetExceeded
                    from backend.core.loop.llm import StreamCancelledError
                    if isinstance(exc, TaskBudgetExceeded):
                        return _budget_exhausted_result(process, session, exc)
                    if isinstance(exc, StreamCancelledError):
                        return _cancellation_result(process, session)
                    raise

        # ── v0.44: 流式 LLM 调用 ──
        tools_param = build_tools_json(tools_dict) if tools_dict else None
        web_mode = str(process.runtime_preferences.get("web_search_mode") or "auto")
        provider_protocol = str(getattr(
            getattr(llm_provider, "protocol", ""), "value",
            getattr(llm_provider, "protocol", ""),
        ))
        if tools_param and web_mode == "disabled":
            tools_param = [item for item in tools_param if str(
                (item.get("function") or {}).get("name") or item.get("name") or ""
            ) not in {"web_search", "web_fetch"}] or None
        current_turn_envelope = _build_current_turn_envelope(
            process, tools_param,
        )
        # Repair provider-valid history at every live turn boundary, not only
        # during cross-daemon recovery. A stream can be interrupted after a
        # native function_call is checkpointed but before Gitgo executes it;
        # replaying that call without an output makes Responses/Anthropic reject
        # the whole next request. The Host records an explicit unknown result
        # and never guesses success or retries a possible side effect.
        sealed_live_calls = session.seal_dangling_tool_calls(
            recovery_code="LIVE_TOOL_RESULT_UNAVAILABLE",
            provider_route=provider_route,
        )
        if sealed_live_calls:
            _emit_observation({
                "event": "provider_tool_calls_sealed",
                "tool_call_ids": sealed_live_calls,
                "execution_state": "unknown",
                "recovery_code": "LIVE_TOOL_RESULT_UNAVAILABLE",
            })
        accumulated_text = ""
        accumulated_reasoning = ""
        accumulated_reasoning_details: list = []
        provider_artifacts: list[dict] = []
        provider_incomplete_reason = ""
        provider_incomplete_detail: Any = None
        pending_tool_calls: dict[int, dict] = {}
        start = time.time()
        content = ""
        tool_calls: list[dict] = []
        # This state belongs to the whole provider attempt, including the
        # bounded synchronous fallback.  Initializing it only after a stream
        # completed left the recovery-success path with an unbound local and
        # crashed the task after several minutes of otherwise valid work.
        invalid_tool_calls: list[dict] = []

        try:
            budget_call_index = 0
            if process.task_budget is not None:
                budget_call_index = process.task_budget.begin_provider_call(
                    process.process_id
                )
            budget_stream_prefix = f"provider-{budget_call_index}:"
            provider_idle_timeout = _provider_idle_timeout(process)
            provider_output_limit = max(
                1, int(getattr(llm_provider, "max_output_tokens", 4096)),
            )
            if process.task_kind == "answer":
                # A direct answer must not spend an action-sized output budget
                # on hidden reasoning. Complex work can be admitted as action,
                # plan or review and retains the configured provider limit.
                provider_output_limit = min(provider_output_limit, 8192)
            if process.steps_used == 0:
                _emit_observation({
                    "event": "progress_summary",
                    "message": "Understanding the request and preparing the next action.",
                    "phase": "provider",
                })
            cache_intent = session.cache_intent(
                tools_param, mode=getattr(
                    getattr(llm_provider, "capabilities", None),
                    "prompt_cache", "automatic",
                ),
            )
            _emit_observation({
                "event": "provider_request_started",
                "task_kind": process.task_kind,
                "protocol": str(getattr(
                    getattr(llm_provider, "protocol", ""), "value",
                    getattr(llm_provider, "protocol", ""),
                )),
                "model": str(getattr(llm_provider, "_model", "")),
                "context_epoch": session.context_epoch,
                "message_count": len(session.messages),
                "tool_count": len(tools_param or []),
                "idle_timeout_seconds": provider_idle_timeout,
                "cache_intent": cache_intent.to_dict(),
            }, detail={
                "representation": "canonical_provider_input",
                "messages": session.to_provider_messages(
                    dynamic_envelope=current_turn_envelope,
                    provider_route=provider_route,
                ),
                "tools": tools_param or [],
            })
            for event in _iter_provider_events(
                llm_provider, session, tools=tools_param,
                cancel_event=process.cancellation_event,
                cache_intent=cache_intent,
                metadata={
                    "web_search_mode": web_mode,
                    # Prefer the provider's mature native search inside this
                    # same model turn. Public search is a low-risk read; when a
                    # compatible endpoint rejects the native tool, LLMProvider
                    # retries once with Gitgo's client SearchBroker schema.
                    "hosted_web_search": bool(
                        web_mode in {"auto", "provider"}
                        and provider_protocol in {"openai_responses", "anthropic_messages"}
                        and getattr(
                            getattr(llm_provider, "capabilities", None),
                            "hosted_web_search", False,
                        )
                    ),
                },
                timeout=provider_idle_timeout,
                current_turn_envelope=current_turn_envelope,
                max_tokens=provider_output_limit,
            ):
                if event.type == ProviderEventType.PROVIDER_CAPABILITY_FALLBACK:
                    artifact = dict(event.artifact or {})
                    _emit_observation({
                        "event": "provider_capability_fallback",
                        "capability": artifact.get("capability", "hosted_web_search"),
                        "from": artifact.get("from", "provider"),
                        "to": artifact.get("to", "searxng"),
                        "status_code": artifact.get("status_code"),
                        "reason": artifact.get("reason", "provider_rejected_capability"),
                    }, detail=artifact)
                    continue
                if event.type in {
                    ProviderEventType.SERVER_TOOL_STARTED,
                    ProviderEventType.SERVER_TOOL_RESULT,
                }:
                    _emit_observation({
                        "event": (
                            "toolcall_start"
                            if event.type == ProviderEventType.SERVER_TOOL_STARTED
                            else "tool_result"
                        ),
                        "tool_name": event.tool_name or "web_search",
                        "tool_call_id": event.tool_call_id,
                        "is_error": False,
                        "result_preview": "Provider-hosted web search completed",
                        "compact_summary": "provider search completed",
                        "server_tool": True,
                    }, detail=event.raw or event.artifact)
                    continue
                text = event.text if event.type == ProviderEventType.TEXT_DELTA else ""
                if text:
                    if process.task_budget is not None:
                        process.task_budget.consume_output_fragment(
                            text, process.process_id,
                            channel=budget_stream_prefix + "text",
                        )
                    accumulated_text += text
                    if on_stream_event:
                        on_stream_event({
                            "event": "text_delta",
                            "process_id": process.process_id,
                            "delta": text,
                            "step": process.steps_used,
                            "visibility": (
                                "public" if process.task_kind == "answer" else "verbose"
                            ),
                        })

                reasoning_delta = (
                    event.reasoning
                    if event.type == ProviderEventType.REASONING_DELTA else ""
                )
                if reasoning_delta:
                    if process.task_budget is not None:
                        process.task_budget.consume_output_fragment(
                            reasoning_delta, process.process_id,
                            channel=budget_stream_prefix + "reasoning",
                        )
                    accumulated_reasoning += reasoning_delta
                    _emit_observation({
                        "event": "reasoning_delta",
                        "delta": reasoning_delta,
                    })
                reasoning_details_delta = (
                    event.artifact
                    if event.type == ProviderEventType.REASONING_ARTIFACT else None
                )
                if reasoning_details_delta is not None:
                    if isinstance(reasoning_details_delta, dict):
                        provider_artifacts.append(reasoning_details_delta)
                        if "reasoning_details" in reasoning_details_delta:
                            detail = reasoning_details_delta["reasoning_details"]
                            if process.task_budget is not None:
                                process.task_budget.consume_output_fragment(
                                    json.dumps(detail, ensure_ascii=False),
                                    process.process_id,
                                    channel=budget_stream_prefix + "reasoning-artifact",
                                )
                            if isinstance(detail, list):
                                accumulated_reasoning_details.extend(detail)
                            else:
                                accumulated_reasoning_details.append(detail)

                if event.type == ProviderEventType.USAGE and event.usage:
                    if process.task_budget is not None:
                        process.task_budget.record_provider_usage(
                            event.usage.to_dict()
                        )
                    session.record_provider_usage(
                        event.usage.to_dict(),
                        protocol=str(getattr(
                            getattr(llm_provider, "protocol", ""), "value",
                            getattr(llm_provider, "protocol", ""),
                        )),
                        cache_intent=cache_intent,
                    )
                    _emit_observation({
                        "event": "provider_usage",
                        "usage": event.usage.to_dict(),
                        "cache": session.cache_summary(),
                    })

                if event.type == ProviderEventType.RESPONSE_INCOMPLETE:
                    artifact = dict(event.artifact or {})
                    provider_incomplete_reason = str(
                        artifact.get("reason", "unknown")
                    )
                    known_item_ids = {
                        str(item.get("response_output_item", {}).get("id", ""))
                        for item in provider_artifacts if isinstance(item, dict)
                    }
                    for item in artifact.get("response_output_items", []) or []:
                        item_id = str(item.get("id", ""))
                        if item_id and item_id in known_item_ids:
                            continue
                        provider_artifacts.append({"response_output_item": item})
                        known_item_ids.add(item_id)
                    # Emit the semantic event once after the stream finishes so
                    # one provider response maps to one timeline row.  Keep the
                    # raw terminal event for that final observation.
                    provider_incomplete_detail = event.raw or artifact

                if event.type in {
                    ProviderEventType.TOOL_CALL_STARTED,
                    ProviderEventType.TOOL_CALL_DELTA,
                    ProviderEventType.TOOL_CALL_DONE,
                }:
                    idx = max(0, event.output_index)
                    if idx not in pending_tool_calls:
                        pending_tool_calls[idx] = {
                            "id": event.tool_call_id,
                            "name": event.tool_name,
                            "partial_json": "",
                            "done": False,
                        }
                        if on_stream_event:
                            on_stream_event({
                                "event": "toolcall_start",
                                "process_id": process.process_id,
                                "tool_call_id": pending_tool_calls[idx]["id"],
                                "tool_name": pending_tool_calls[idx]["name"],
                            })

                    if event.tool_call_id:
                        pending_tool_calls[idx]["id"] = event.tool_call_id
                    if event.tool_name:
                        pending_tool_calls[idx]["name"] = event.tool_name
                    args_fragment = event.arguments_delta
                    if event.type == ProviderEventType.TOOL_CALL_DONE and event.arguments:
                        if not pending_tool_calls[idx]["partial_json"]:
                            args_fragment = event.arguments
                    if event.type == ProviderEventType.TOOL_CALL_DONE:
                        pending_tool_calls[idx]["done"] = True
                    if args_fragment:
                        if process.task_budget is not None:
                            process.task_budget.consume_output_fragment(
                                args_fragment, process.process_id,
                                channel=budget_stream_prefix + f"tool-{idx}",
                            )
                        pending_tool_calls[idx]["partial_json"] += args_fragment
                        if on_stream_event:
                            on_stream_event({
                                "event": "toolcall_delta",
                                "process_id": process.process_id,
                                "tool_call_id": pending_tool_calls[idx]["id"],
                                "delta": args_fragment,
                            })

            # 流成功完成
            duration_ms = (time.time() - start) * 1000
            if process.task_budget is not None:
                process.task_budget.finish_output_fragments(
                    process.process_id, prefix=budget_stream_prefix,
                )
            _stream_recoveries = 0

            # ── 转换 pending_tool_calls → tool_calls ──
            for idx in sorted(pending_tool_calls.keys()):
                p = pending_tool_calls[idx]
                # A normal completed compatibility stream may not expose an
                # explicit TOOL_CALL_DONE event.  For an incomplete Responses
                # stream, however, replaying a merely-started call would make
                # the next request invalid because no output can exist for it.
                if provider_incomplete_reason and not p["done"]:
                    continue
                try:
                    args = json.loads(p["partial_json"]) if p["partial_json"].strip() else {}
                except json.JSONDecodeError as exc:
                    # Never turn malformed provider output into an executable
                    # empty argument object.  Doing so erases the distinction
                    # between "the model omitted path" and "the provider cut a
                    # valid call in half", and can send a write tool into the
                    # rollback loop with entirely fabricated arguments.
                    invalid_tool_calls.append({
                        "name": p["name"],
                        "id": p["id"],
                        "raw": p["partial_json"],
                        "error": str(exc),
                    })
                    _emit_observation({
                        "event": "toolcall_invalid",
                        "tool_call_id": p["id"],
                        "tool_name": p["name"],
                        "output_index": idx,
                        "error_code": "INVALID_TOOL_ARGUMENT_JSON",
                    }, detail={
                        "arguments_raw": p["partial_json"],
                        "parse_error": str(exc),
                    })
                    continue
                if not isinstance(args, dict):
                    invalid_tool_calls.append({
                        "name": p["name"],
                        "id": p["id"],
                        "raw": p["partial_json"],
                        "error": "tool arguments must decode to an object",
                    })
                    continue
                tool_calls.append({
                    "name": p["name"],
                    "id": p["id"],
                    "args": args,
                })
                _emit_observation({
                    "event": "toolcall_done",
                    "tool_call_id": p["id"],
                    "tool_name": p["name"],
                    "output_index": idx,
                }, detail={"arguments": args, "arguments_raw": p["partial_json"]})

            content = accumulated_text
            if content or tool_calls or accumulated_reasoning or accumulated_reasoning_details:
                replay_artifacts = provider_artifacts
                if provider_incomplete_reason or invalid_tool_calls:
                    replay_artifacts = _filter_incomplete_response_artifacts(
                        provider_artifacts,
                        {str(call.get("id", "")) for call in tool_calls},
                    )
                continuation = _provider_continuation(
                    accumulated_reasoning, accumulated_reasoning_details,
                    replay_artifacts,
                )
                session.append_assistant_provider(
                    content,
                    tool_calls=_to_provider_tool_calls(tool_calls),
                    continuation_state=continuation,
                )
            process.steps_used += 1
            _emit_observation({
                "event": (
                    "provider_response_incomplete"
                    if provider_incomplete_reason
                    else "provider_response_completed"
                ),
                "completed_step": process.steps_used,
                "duration_ms": round(duration_ms, 3),
                "text_chars": len(accumulated_text),
                "reasoning_chars": len(accumulated_reasoning),
                "tool_call_count": len(tool_calls),
                "reason": provider_incomplete_reason,
            }, detail=(
                provider_incomplete_detail
                if provider_incomplete_reason else None
            ))

        except Exception as exc:
            if process.task_budget is not None:
                process.task_budget.finish_output_fragments(
                    process.process_id, prefix=budget_stream_prefix,
                )
            from backend.core.loop.budget import TaskBudgetExceeded
            from backend.core.loop.llm import StreamCancelledError, StreamInterruptedError
            if isinstance(exc, TaskBudgetExceeded):
                return _budget_exhausted_result(
                    process, session, exc, response=accumulated_text,
                    duration_ms=(time.time() - start) * 1000,
                )
            if isinstance(exc, StreamCancelledError):
                if accumulated_text or accumulated_reasoning or accumulated_reasoning_details:
                    replay_artifacts = _filter_incomplete_response_artifacts(
                        provider_artifacts, set(),
                    )
                    session.append_assistant_provider(
                        accumulated_text,
                        continuation_state=_provider_continuation(
                            accumulated_reasoning, accumulated_reasoning_details,
                            replay_artifacts,
                        ),
                    )
                return _cancellation_result(
                    process, session, accumulated_text,
                    duration_ms=(time.time() - start) * 1000,
                )
            if isinstance(exc, StreamInterruptedError):
                if exc.is_context_overflow:
                    from backend.core.errors import error_payload
                    overflow = error_payload(
                        "CONTEXT_WINDOW_EXCEEDED",
                        details={
                            "configured_limit": session.model_context_limit,
                            "estimated_tokens": session.estimate_tokens(),
                            "provider_status": exc.status_code,
                        },
                        next_actions=[{
                            "action": "compact",
                            "effect": "Start a new bounded context epoch, then retry.",
                        }],
                    )
                    _emit_observation({
                        "event": "context_window_exceeded",
                        "error": overflow["error_info"],
                    })
                    changed, terminal = _run_context_compaction(
                        "provider_overflow", failure_required=True,
                    )
                    if terminal is not None:
                        return terminal
                    # A successful epoch retries with a smaller request. A
                    # failed ordinary attempt also retries, but only until the
                    # third failure promotes the explicit user decision above.
                    continue
                # ── 流中断恢复 ──
                if _stream_recoveries < MAX_STREAM_RECOVERIES:
                    _stream_recoveries += 1

                    if accumulated_text or accumulated_reasoning or accumulated_reasoning_details:
                        replay_artifacts = _filter_incomplete_response_artifacts(
                            provider_artifacts, set(),
                        )
                        session.append_assistant_provider(
                            accumulated_text,
                            continuation_state=_provider_continuation(
                                accumulated_reasoning, accumulated_reasoning_details,
                                replay_artifacts,
                            ),
                        )
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
                            "diagnostic": _bounded_exception_chain(exc),
                        })

                    # 不修改 steps_used —— 流中断时尚未递增
                    continue  # 重试本轮

                # 恢复次数用尽 → 降级为同步 chat()
                try:
                    if process.task_budget is not None:
                        process.task_budget.begin_provider_call(process.process_id)
                    response = llm_provider.chat(
                        (
                            session.to_provider_messages(
                                dynamic_envelope=current_turn_envelope,
                                provider_route=provider_route,
                            )
                            if hasattr(llm_provider, "stream_events")
                            else session.to_openai_messages(
                                dynamic_envelope=current_turn_envelope,
                            )
                        ),
                        tools=tools_param,
                        preserve_provider_state=True,
                        cancel_event=process.cancellation_event,
                        timeout=_provider_idle_timeout(process),
                        # Streaming already had one continuation attempt. A
                        # bounded synchronous fallback must not hide another
                        # five network retries inside one tree-budget charge.
                        max_retries=0,
                    )
                    duration_ms = (time.time() - start) * 1000
                    if isinstance(response, dict):
                        content = response.get("content", "") or ""
                        tool_calls = parse_tool_calls(response)
                    else:
                        content = response or ""
                        tool_calls = parse_tool_calls(content)
                    if process.task_budget is not None:
                        process.task_budget.consume_output(content, process.process_id)
                    if content or tool_calls:
                        continuation = {}
                        if isinstance(response, dict) and response.get("reasoning_content"):
                            continuation["reasoning_content"] = response["reasoning_content"]
                        if isinstance(response, dict) and response.get("reasoning_details"):
                            continuation["reasoning_details"] = response["reasoning_details"]
                        if isinstance(response, dict) and response.get("provider_artifacts"):
                            continuation.update(_provider_continuation(
                                str(response.get("reasoning_content") or ""),
                                list(response.get("reasoning_details") or []),
                                response["provider_artifacts"],
                            ))
                        if isinstance(response, dict) and response.get("usage"):
                            if process.task_budget is not None:
                                process.task_budget.record_provider_usage(
                                    response["usage"]
                                )
                            session.record_provider_usage(
                                response["usage"],
                                protocol=str(getattr(
                                    getattr(llm_provider, "protocol", ""), "value",
                                    getattr(llm_provider, "protocol", ""),
                                )),
                            )
                        session.append_assistant_provider(
                            content,
                            tool_calls=_to_provider_tool_calls(tool_calls),
                            continuation_state=continuation,
                        )
                    process.steps_used += 1
                    _stream_recoveries = 0
                except Exception as chat_exc:
                    if isinstance(chat_exc, TaskBudgetExceeded):
                        return _budget_exhausted_result(
                            process, session, chat_exc, response=accumulated_text,
                            duration_ms=(time.time() - start) * 1000,
                        )
                    if isinstance(chat_exc, StreamCancelledError):
                        return _cancellation_result(
                            process, session, accumulated_text,
                            duration_ms=(time.time() - start) * 1000,
                        )
                    process.status = ProcessStatus.FAILED
                    return _make_result(process, session, "", duration_ms=0,
                                      error_code="STREAM_FALLBACK_FAILED",
                                      error_message=str(chat_exc),
                                      outcome_status=OutcomeStatus.FAILED)
            else:
                # 非流中断异常 → task failure
                process.status = ProcessStatus.FAILED
                return _make_result(process, session, "", duration_ms=0,
                                  error_code="LLM_STREAM_FAILED",
                                  error_message=str(exc),
                                  outcome_status=OutcomeStatus.FAILED)

        if invalid_tool_calls:
            for invalid in invalid_tool_calls:
                tool_name = str(invalid.get("name") or "unknown")
                loop_guard.record_tool_result(
                    tool_name,
                    {"invalid_json": True},
                    True,
                    effect="read",
                )
                loop_guard.record_tool_error(
                    tool_name, "INVALID_TOOL_ARGUMENT_JSON",
                    {"invalid_json": True},
                )

        if provider_incomplete_reason and not tool_calls:
            if process.task_kind == "answer" and not accumulated_text.strip():
                if _no_progress_recoveries < 1:
                    _no_progress_recoveries += 1
                    session.append_user(
                        "[HOST NO-PROGRESS RECOVERY] The prior direct-answer turn "
                        "spent its output budget without public text or a tool call. "
                        "Do not repeat or extend the hidden analysis. Treat quoted input "
                        "literally unless the user explicitly asked for decoding. Either "
                        "make one bounded evidence call now, or provide the concise public "
                        "answer immediately.",
                        message_type="host_provider_continuation",
                    )
                    _emit_observation({
                        "event": "provider_no_progress_recovery",
                        "attempt": _no_progress_recoveries,
                        "max_attempts": 1,
                        "reason": provider_incomplete_reason,
                    })
                    continue
                session.quarantine_failed_provider_turn(
                    "answer_incomplete_without_public_text",
                )
                process.status = ProcessStatus.FAILED
                return _make_result(
                    process,
                    session,
                    "The model exhausted two bounded direct-answer attempts without "
                    "producing a reply.",
                    duration_ms=duration_ms,
                    error_code="PROVIDER_NO_PROGRESS",
                    error_message=(
                        "A direct-answer provider turn remained incomplete after one "
                        "bounded recovery; the failed turn was isolated from future input"
                    ),
                    outcome_status=OutcomeStatus.FAILED,
                )
            unfinished_names = sorted({
                str(item.get("name") or "unknown")
                for item in pending_tool_calls.values()
                if not item.get("done")
            })
            for tool_name in unfinished_names:
                incomplete_tool_call_counts[tool_name] = (
                    incomplete_tool_call_counts.get(tool_name, 0) + 1
                )
            repeated = [
                name for name in unfinished_names
                if incomplete_tool_call_counts.get(name, 0) >= 3
            ]
            if repeated:
                process.status = ProcessStatus.FAILED
                return _make_result(
                    process,
                    session,
                    accumulated_text,
                    duration_ms=duration_ms,
                    error_code="PROVIDER_TOOL_CALL_TRUNCATION_STORM",
                    error_message=(
                        "Provider repeatedly truncated function arguments for: "
                        + ", ".join(repeated)
                    ),
                    outcome_status=OutcomeStatus.FAILED,
                )
            tool_hint = (
                " The unfinished function call was discarded and MUST NOT be "
                "replayed. Use a substantially smaller payload or split the "
                "work into bounded incremental tool calls."
                if unfinished_names else ""
            )
            session.append_user(
                "[HOST PROVIDER CONTINUATION] The provider reached "
                f"{provider_incomplete_reason}. Continue from the saved partial "
                "reasoning/output without repeating prior analysis. Finish concisely "
                f"within the remaining task budget.{tool_hint}",
                message_type="host_provider_continuation",
            )
            continue

        if invalid_tool_calls and not tool_calls:
            names = ", ".join(sorted({
                str(item.get("name") or "unknown") for item in invalid_tool_calls
            }))
            session.append_user(
                "[HOST TOOL ARGUMENT ERROR] The provider returned malformed JSON "
                f"for {names}. No tool was executed. Reissue a smaller valid JSON "
                "object; do not repeat the same payload.",
                message_type="host_tool_argument_error",
            )
            continue

        # Provider work can overlap a workspace watcher update. Refresh before
        # any tool gate or completion decision consumes governance signals.
        _refresh_governance_snapshot()

        # ── 工具调用 → ToolExecution 批次事务 ──
        if tool_calls:
            # Storm handling remains a loop concern.  Tool governance itself is
            # owned by ToolPipeline so expanded composite steps use the exact
            # same path and wrapper/component checks are never duplicated.
            import uuid
            execution_id = str(uuid.uuid4())
            missing_approval = _missing_permission(tool_calls)
            if missing_approval is not None:
                permission_call_index, pending_tool, public_args = missing_approval
                return _suspend_tool_batch_for_permission(
                    tool_calls, execution_id, permission_call_index,
                    pending_tool, public_args,
                )
            filtered_calls = []
            filtered_to_provider: list[int] = []
            results = []
            deferred_nudges: list[str] = []
            for provider_index, tc in enumerate(tool_calls):
                tool_name = tc["name"]
                # v0.45: Storm break
                storm_nudge = loop_guard.check_storm_break(
                    tool_name,
                    _latest_error_code(process, tool_name),
                    tc.get("args", {}),
                )
                if storm_nudge:
                    results.append(_host_tool_error_result(
                        tool_name=tool_name,
                        execution_id=execution_id,
                        call_index=provider_index,
                        error_name="TOOL_CALL_STORM_BLOCKED",
                        details={
                            "execution_state": "not_started",
                            "last_error_code": _latest_error_code(process, tool_name),
                            "guidance": storm_nudge,
                        },
                        next_actions=[{
                            "action": "change_strategy",
                            "effect": "Inspect the prior error and materially change the call before retrying.",
                        }],
                    ))
                    deferred_nudges.append(storm_nudge)
                    continue

                filtered_calls.append(tc)
                filtered_to_provider.append(provider_index)

            execution = None
            if filtered_calls:
                execution = ToolExecution(
                    execution_id=execution_id,
                    ctx=ctx,
                    tool_calls=filtered_calls,
                )
                execution.begin()
                executed_results = execution.execute_batch(tools_dict)
                for result in executed_results:
                    local_index = int(result.call_index)
                    if 0 <= local_index < len(filtered_to_provider):
                        result.call_index = filtered_to_provider[local_index]
                    results.append(result)
                if not execution._rolled_back:
                    execution.commit()

            rolled_back = bool(execution and execution._rolled_back)
            returned_indexes = {int(result.call_index) for result in results}
            # A crash can cancel calls that were already emitted by the
            # provider but never started. They still require explicit outputs.
            for provider_index, tool_call in enumerate(tool_calls):
                if provider_index in returned_indexes:
                    continue
                results.append(_host_tool_error_result(
                    tool_name=str(tool_call.get("name") or "unknown"),
                    execution_id=execution_id,
                    call_index=provider_index,
                    error_name="TOOL_NOT_STARTED",
                    details={
                        "execution_state": "not_started",
                        "rolled_back": rolled_back,
                    },
                    next_actions=[{
                        "action": "inspect_then_retry",
                        "effect": "Confirm current state before retrying a possibly related operation.",
                    }],
                ))

            # Provider-turn atomicity: all tool outputs are contiguous and in
            # call order. Only after this loop may Host/user messages be added.
            _commit_tool_results(tool_calls, results, rolled_back=rolled_back)

            if rolled_back:
                rollback_reason = str(execution._rollback_reason or "tool execution failed")
                session.append_user(
                    "[HOST ROLLBACK] The preceding tool batch was rolled back: "
                    + rollback_reason
                    + ". Inspect current state before retrying.",
                    message_type="governance_nudge",
                )
                if on_stream_event:
                    on_stream_event({
                        "event": "rollback_notification",
                        "process_id": process.process_id,
                        "execution_id": execution.execution_id,
                        "reason": rollback_reason,
                    })
            for nudge in dict.fromkeys(deferred_nudges):
                session.append_user(nudge, message_type="governance_nudge")

            task_kind_changed = False
            if not rolled_back:
                task_kind_changed = _apply_host_task_transitions(process, results)

            if rolled_back:
                continue

            if process.pending_decision is not None:
                from backend.core.loop.decision_support import format_decision_request
                decision = dict(process.pending_decision)
                process.status = ProcessStatus.AWAITING_USER
                if on_stream_event:
                    on_stream_event({
                        "event": "decision_required",
                        "process_id": process.process_id,
                        "decision": decision,
                    })
                result = _make_result(
                    process,
                    session,
                    format_decision_request(decision),
                    duration_ms=(time.time() - start) * 1000,
                    outcome_status=OutcomeStatus.AWAITING_USER,
                )
                process.result = result
                return result

            # ``complete_task`` and ``complete_review`` are explicit semantic
            # completion claims.  Once their tool result is committed, every
            # remaining check is deterministic Host work (governance, receipts,
            # tests, review and mailbox state).  Do not spend another provider
            # turn asking the model to repeat the same claim as TASK_COMPLETE.
            completion_tool_result = next((
                r for r in results
                if r.tool_name in {
                    "complete_task", "complete_review", "complete_supervision",
                }
                and not r.is_error
                and isinstance(r.data, dict)
                and r.data.get("accepted") is True
            ), None)
            if completion_tool_result is not None:
                if (
                    completion_tool_result.tool_name == "complete_supervision"
                    and completion_tool_result.data.get("terminal_status") == "failed"
                ):
                    from backend.core.loop.completion_protocol import HostCompletionEvaluator
                    failure_report = str(
                        completion_tool_result.data.get("result", "")
                    )
                    failure_evaluation = HostCompletionEvaluator.evaluate_supervisor_failure(
                        process, failure_report,
                    )
                    if failure_evaluation.allowed:
                        _emit_observation({
                            "event": "completion_gate",
                            "accepted": True,
                            "source": "complete_supervision",
                            "terminal_status": "failed",
                        })
                        _capture_completion_recovery(process)
                        process.status = ProcessStatus.FAILED
                        return _make_result(
                            process, session, failure_report,
                            duration_ms=(time.time() - start) * 1000,
                            error_code="SUPERVISED_DELIVERY_FAILED",
                            error_message=failure_report,
                            outcome_status=OutcomeStatus.FAILED,
                            window_action=window_check["action"],
                            window_usage_ratio=window_check["usage_ratio"],
                        )
                    failure_reason = "; ".join(failure_evaluation.reasons)
                    _emit_observation({
                        "event": "completion_gate",
                        "accepted": False,
                        "source": "complete_supervision",
                        "terminal_status": "failed",
                        "reason": failure_reason,
                    })
                    _inc_nudge_counter(process, "completion_tool_rejection")
                    session.append_host_steering(
                        failure_reason,
                        steering_type="completion_tool_rejection",
                        version=str(process.steps_used),
                    )
                    continue
                guard_result = loop_guard.check(
                    process, "TASK_COMPLETE", session, _signal_bus, signals,
                )
                if guard_result.is_complete:
                    _emit_observation({
                        "event": "completion_gate",
                        "accepted": True,
                        "source": completion_tool_result.tool_name,
                    })
                    if process.mailbox is not None and not process.mailbox.close_if_empty(
                        "task completed",
                    ):
                        _apply_mailbox_at_boundary()
                    else:
                        _capture_completion_recovery(process)
                        process.status = ProcessStatus.COMPLETED
                        if completion_tool_result.tool_name == "complete_supervision":
                            final_response = str(
                                completion_tool_result.data.get("result", "")
                            )
                        elif process.completion_claim is not None:
                            final_response = process.completion_claim.result
                        elif isinstance(process.review_claim, dict):
                            final_response = str(process.review_claim.get("summary", ""))
                        else:
                            final_response = content or "Task completed"
                        result = _make_result(
                            process, session, final_response,
                            duration_ms=(time.time() - start) * 1000,
                            outcome_status=(
                                OutcomeStatus.DEGRADED if guard_result.degraded else None
                            ),
                            window_action=window_check["action"],
                            window_usage_ratio=window_check["usage_ratio"],
                        )
                        process.result = result
                        return result
                elif guard_result.blocked:
                    _emit_observation({
                        "event": "completion_gate",
                        "accepted": False,
                        "source": completion_tool_result.tool_name,
                        "reason": guard_result.nudge_text,
                    })
                    _inc_nudge_counter(process, "completion_tool_rejection")
                    from backend.core.loop.completion_protocol import HostCompletionEvaluator
                    gate_status = HostCompletionEvaluator.outstanding_gates(
                        process, signals,
                    )
                    fingerprint = "|".join(sorted(
                        str(item.get("gate_id") or "")
                        for item in gate_status.get("gates", [])
                    ))
                    previous = str(process.runtime_preferences.get(
                        "completion_gate_fingerprint", ""
                    ))
                    repeats = int(process.runtime_preferences.get(
                        "completion_gate_repeats", 0
                    ) or 0)
                    repeats = repeats + 1 if fingerprint and fingerprint == previous else 1
                    process.runtime_preferences["completion_gate_fingerprint"] = fingerprint
                    process.runtime_preferences["completion_gate_repeats"] = repeats
                    if fingerprint and repeats >= 2 and process.pending_decision is None:
                        from backend.core.loop.decision_support import (
                            create_completion_exception_decision,
                            format_decision_request,
                        )
                        decision = create_completion_exception_decision(
                            process, gate_status,
                        )
                        process.status = ProcessStatus.AWAITING_USER
                        _emit_observation({
                            "event": "decision_required",
                            "process_id": process.process_id,
                            "decision": decision,
                        })
                        result = _make_result(
                            process, session, format_decision_request(decision),
                            duration_ms=(time.time() - start) * 1000,
                            outcome_status=OutcomeStatus.AWAITING_USER,
                        )
                        process.result = result
                        return result
                    session.append_host_steering(
                        guard_result.nudge_text,
                        steering_type="completion_tool_rejection",
                        version=str(process.steps_used),
                    )

            if (
                process.task_kind == "action"
                and process.completion_claim is None
                and not process._nudge_counters.get("evidence_ready", 0)
            ):
                from backend.core.loop.completion_protocol import HostCompletionEvidence
                evidence = HostCompletionEvidence.collect(process)
                if evidence.factual_ready:
                    process._nudge_counters["evidence_ready"] = 1
                    session.append_host_steering(
                        "[HOST EVIDENCE READY]\n"
                        "The required Host-observed receipts and tests are present. "
                        "Do not repeat the work. Review the result once, then either "
                        "call complete_task or provide the final result followed by "
                        "a standalone TASK_COMPLETE line.",
                        steering_type="completion_evidence_ready",
                        version=str(process.steps_used),
                    )

            if task_kind_changed or any(
                r.tool_name in {
                    "declare_task_contract", "request_self_execute", "define_tool",
                    "author_tool",
                    "delegate_task", "delegate_task_dag", "delegate_task_bundle",
                    "promote_agent_changes",
                } and not r.is_error
                for r in results
            ):
                previous_tool_names = set(tools_dict)
                internal_tools = _build_internal_tools(
                    process, dispatcher_tools, workspace_path,
                    llm_provider, dispatcher,
                    on_stream_event=on_stream_event,
                )
                all_tools = {
                    **dispatcher_tools, **process.dynamic_tools, **internal_tools,
                }
                tools_dict, unavailable_tools = _select_authorized_tools(process, all_tools)
                if unavailable_tools:
                    process.status = ProcessStatus.FAILED
                    return _error_result(
                        process, "CAPABILITY_TOOL_UNAVAILABLE",
                        "Capability references unavailable tools: "
                        + ", ".join(unavailable_tools),
                    )
                ctx.artifacts["tool_catalog"] = tools_dict
                newly_authorized = set(tools_dict).difference(previous_tool_names)
                if newly_authorized:
                    # Capability failures from the old surface are no longer
                    # evidence of a retry storm on the newly authorized one.
                    loop_guard.reset_tool_errors(newly_authorized)
                _refresh_compiled_prompt(
                    session, process, tools_dict, workspace_path,
                )

            _track_step(process, content[:200])
            continue

        # ── 检查完成 ──
        guard_result = loop_guard.check(
            process, content, session, _signal_bus, signals,
        )
        if guard_result.is_complete:
            _emit_observation({
                "event": "completion_gate",
                "accepted": True,
                "source": "text_marker",
            })
            if process.mailbox is not None and not process.mailbox.close_if_empty(
                "task completed",
            ):
                _apply_mailbox_at_boundary()
                continue
            _capture_completion_recovery(process)
            process.status = ProcessStatus.COMPLETED
            from backend.core.loop.completion_protocol import CompletionClaim
            public_content = CompletionClaim.from_response(
                content, step=process.steps_used,
            ).result
            result = _make_result(
                process, session, public_content,
                duration_ms=duration_ms,
                window_action=window_check["action"],
                window_usage_ratio=window_check["usage_ratio"],
            )
            process.result = result
            return result
        if guard_result.blocked:
            _emit_observation({
                "event": "completion_gate",
                "accepted": False,
                "source": "text_marker",
                "reason": guard_result.nudge_text,
            })
            nudge_code = guard_result.reason_code or "completion_rejection"
            _inc_nudge_counter(process, nudge_code)
            if _get_nudge_count(process, nudge_code) >= 3:
                process.status = ProcessStatus.FAILED
                return _make_result(process, session, content,
                                  error_code="NUDGE_ESCALATION",
                                  error_message="Completion requirements remained blocked",
                                  outcome_status=OutcomeStatus.FAILED)
            session.append_user(
                guard_result.nudge_text,
                message_type="governance_nudge",
            )
            continue

        # ── 普通文本（无 tool_call 无 TASK_COMPLETE）──
        # Once every delegated fact is terminal and approved, a non-empty A
        # response is already the semantic delivery. The Host can close it
        # without spending repeated provider turns asking for a password/tool.
        if process.task_kind == "supervisor" and content.strip():
            from backend.core.loop.completion_protocol import HostCompletionEvaluator
            supervisor_evaluation = HostCompletionEvaluator.evaluate(
                process, content, signals,
            )
            if supervisor_evaluation.allowed:
                _emit_observation({
                    "event": "completion_gate",
                    "accepted": True,
                    "source": "supervisor_plain_text",
                })
                if process.mailbox is not None and not process.mailbox.close_if_empty(
                    "task completed",
                ):
                    _apply_mailbox_at_boundary()
                    continue
                _capture_completion_recovery(process)
                process.status = ProcessStatus.COMPLETED
                result = _make_result(
                    process, session, content,
                    duration_ms=duration_ms,
                    window_action=window_check["action"],
                    window_usage_ratio=window_check["usage_ratio"],
                )
                process.result = result
                return result
        # After the Host has observed a committed action receipt (and every
        # declared required test), the next non-tool response is the semantic
        # delivery.  Compile it into the same CompletionClaim used by
        # ``complete_task`` instead of spending provider turns on a password or
        # allowing protocol wording to strand completed work.  This state is
        # set only by HostCompletionEvidence, never inferred from user prose.
        if (
            process.task_kind == "action"
            and content.strip()
            and process.completion_claim is None
            and process._nudge_counters.get("evidence_ready", 0)
        ):
            from backend.core.loop.completion_protocol import (
                CompletionClaim, HostCompletionEvaluator,
            )
            process.completion_claim = CompletionClaim.from_response(
                content, step=process.steps_used,
            )
            action_evaluation = HostCompletionEvaluator.evaluate(
                process, content, signals,
            )
            if action_evaluation.allowed:
                _emit_observation({
                    "event": "completion_gate",
                    "accepted": True,
                    "source": "host_evidence_plain_text",
                })
                if process.mailbox is not None and not process.mailbox.close_if_empty(
                    "task completed",
                ):
                    process.completion_claim = None
                    _apply_mailbox_at_boundary()
                    continue
                _capture_completion_recovery(process)
                process.status = ProcessStatus.COMPLETED
                result = _make_result(
                    process, session, process.completion_claim.result,
                    duration_ms=duration_ms,
                    window_action=window_check["action"],
                    window_usage_ratio=window_check["usage_ratio"],
                )
                process.result = result
                return result
            # The claim remains useful to the outstanding-gates projection;
            # normal LoopGuard handling below will provide the deterministic
            # recovery path rather than silently accepting incomplete facts.
        _track_step(process, content[:200])

        # v0.42: doom_loop 检测已接入 LoopGuard.check()（通过 check_doom_loop_safe）。
        # 普通文本路径中，LoopGuard 在 agent_step 的完成检查分支中调用，
        # 此处保留冗余检测作为最后防线（check_budget_continuity 也在这里）。
        # 如果 LoopGuard missed 了一个 doom_loop（例如 completion markers 出现在非完成上下文中），
        # 这个直接调用是最后的安全网。

        # 纯文本死循环检测
        if _repeated_plain_text(process, content):
            process.status = ProcessStatus.FAILED
            return _make_result(process, session, content,
                              duration_ms=duration_ms,
                              error_code="PLAIN_TEXT_LOOP_DETECTED",
                              error_message="Agent repeated the same plain-text response",
                              outcome_status=OutcomeStatus.FAILED)

        # Token budget 延续检测
        budget_check = context_window.check_budget_continuity(session)
        if budget_check["stagnant"]:
            session.append_user(
                "[系统提示] 最近几轮对话无实质进展（无工具调用或完成信号）。"
                "请调用工具或回复 TASK_COMPLETE 结束任务。"
            )
            continue

    # max_steps 耗尽
    process.status = ProcessStatus.FAILED
    return _make_result(process, session, "", duration_ms=0,
                       error_code="MAX_STEPS_EXHAUSTED",
                       error_message="Agent exhausted its step budget",
                       outcome_status=OutcomeStatus.FAILED)


# ── Tool Prompt ──────────────────────────────────────────────

def _apply_pending_instructions(
    process: AgentProcess,
    session: "AgentSession",
) -> int:
    if process.mailbox is None:
        return 0
    messages = process.mailbox.drain(
        task_id=process.active_task_id or process.process_id,
        step=process.steps_used,
    )
    for message in messages:
        message_type = {
            "governance_update": "governance_nudge",
            "coordination_update": "host_coordination_update",
        }.get(message.kind, "conversation")
        session.append_user(message.content, message_type=message_type)
        session.messages[-1]["mailbox_message_id"] = message.message_id
    return len(messages)


def _build_internal_tools(
    process: AgentProcess,
    available_tools: dict[str, AgentTool] | None = None,
    workspace_path: str = "",
    llm_provider=None,
    dispatcher=None,
    on_stream_event=None,
) -> dict[str, AgentTool]:
    """Build host protocol tools bound to this process, not to business code."""
    from backend.core.loop.context_store import build_context_tools
    tools: dict[str, AgentTool] = build_context_tools(process, workspace_path)
    available_tools = available_tools or {}

    from backend.core.loop.engineering_workflow import EngineeringWorkflow
    engineering = EngineeringWorkflow(process, on_stream_event)
    tools["engineering_workflow"] = AgentTool(
        execution_contract=data_broker("host.runtime.engineering_workflow"),
        name="engineering_workflow",
        description=(
            "Manage Host-enforced engineering practices, not prompt-only skills. "
            "Built-in presets accept profiles plus concrete test_id/target_files, "
            "questions(id,state_topic,depends_on), glossary_path or agent_document_path; "
            "check_argv/check_cwd bind non-pytest checks; preparation_files permits "
            "building evidence harnesses before red without permitting product fixes. "
            "omit nodes to compile a preset. "
            "configure accepts a bounded dependency graph with profiles alignment, "
            "domain_modeling, diagnosis, tdd, architecture, retrospective or agent_documentation. "
            "Nodes use decision(state_topic), observation(tools), check(test_id,passed), "
            "change(files), document(path), or report(format). depends_on controls the ready "
            "frontier; before_mutation enforces prerequisites. status returns outstanding "
            "evidence; ask routes one ready decision through the existing user broker. "
            "record saves a document hash or structured report. Receipts and user answers "
            "are observed by Host. retrospective derives findings from current task receipts. "
            "Existing permissions remain mandatory; unavailable checks never become passes. "
            "propose_amendment(plan,reason) offers a concrete scope change to the user; "
            "only the matching user decision can weaken an active plan."
        ),
        parameters={"type": "object", "properties": {
            "operation": {"type": "string", "enum": ["configure", "status", "ask", "record", "retrospective", "propose_amendment"]},
            "plan": {"type": "object"}, "node_id": {"type": "string"},
            "content": {"type": "object"}, "request": {"type": "object"}, "reason": {"type": "string"},
        }, "required": []},
        execute=engineering.invoke, read_only=True, effect=ToolEffect.READ,
        idempotent=False,
    )

    if workspace_path:
        from backend.core.loop.code_dossier import (
            build_code_dossier,
            build_shard_dossier,
            suggest_task_shards,
        )
        from backend.core.loop.context_store import ContextObjectStore

        dossier_store = ContextObjectStore(workspace_path)

        def code_dossier(args: dict) -> dict:
            dossier = build_code_dossier(
                workspace_path,
                [str(item) for item in (args.get("target_files", []) or [])],
                max_files=int(args.get("max_files", 240) or 240),
            )
            refs = dossier_store.put(
                f"code-dossiers/{dossier['digest']}",
                dossier,
                metadata={
                    "authority": "deterministic_host_facts",
                    "semantic_conclusion": False,
                },
            )
            return {
                **refs,
                "file_count": dossier["file_count"],
                "estimated_tokens": dossier["estimated_tokens"],
                "size_class": dossier["size_class"],
                "truncated": dossier["truncated"],
                "instruction": (
                    "Open the pinned ref when file-level facts are needed; do not "
                    "repeat whole-workspace enumeration."
                ),
            }

        tools["code_dossier"] = AgentTool(
            execution_contract=data_broker("host.runtime.code_dossier"),
            name="code_dossier",
            description=(
                "Build a deterministic, content-addressed code dossier containing "
                "file sizes, symbols, imports, tests and dependency evidence. It makes "
                "no semantic audit conclusion."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target_files": {"type": "array", "items": {"type": "string"}},
                    "max_files": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "required": ["target_files"],
            },
            execute=code_dossier,
            effect=ToolEffect.READ,
            idempotent=True,
        )

    from backend.core.loop.decision_support import (
        collect_decision_evidence,
        create_user_decision,
        safe_calculate,
    )
    tools["calculate"] = AgentTool(
        execution_contract=HOST_COMPUTE,
        name="calculate",
        description=(
            "Deterministically evaluate bounded arithmetic or numeric comparisons; "
            "use this instead of doing calculation mentally."
        ),
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        execute=lambda args: {
            "expression": str(args.get("expression", "")),
            "result": safe_calculate(str(args.get("expression", ""))),
        },
        effect=ToolEffect.READ,
        idempotent=True,
    )
    tools["decision_evidence"] = AgentTool(
        execution_contract=data_broker("host.runtime.decision_evidence"),
        name="decision_evidence",
        description=(
            "Ask the Host to aggregate completion, child, test, governance, or budget "
            "facts before making a semantic decision."
        ),
        parameters={
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "enum": ["all", "completion", "gates", "children", "tests", "governance", "budget", "capabilities", "engineering"],
                },
            },
        },
        execute=lambda args: collect_decision_evidence(
            process, str(args.get("focus", "all"))
        ),
        effect=ToolEffect.READ,
        idempotent=True,
    )

    def capability_status(_args: dict) -> dict:
        from backend.core.loop.capability_negotiation import build_capability_snapshot

        protocol = str(process.runtime_preferences.get("provider_protocol") or "unknown")
        return build_capability_snapshot(
            model_id=str(process.model_id or "unknown"),
            protocol=protocol,
            capabilities=process.runtime_preferences.get("provider_capabilities") or {},
            runtime_preferences=process.runtime_preferences,
            available_tools=set(process.tool_registry.list_all()),
        )

    tools["capability_status"] = AgentTool(
        execution_contract=data_broker("host.runtime.capability_status"),
        name="capability_status",
        description=(
            "Read the Host's current model, provider protocol and capability/fallback "
            "status without exposing credentials. Use this before claiming that Gitgo "
            "or the active provider cannot perform a feature."
        ),
        parameters={
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string", "enum": ["all", "web_access"],
                },
            },
        },
        execute=capability_status,
        effect=ToolEffect.READ,
        idempotent=True,
    )

    def configure_capability(args: dict) -> dict:
        """Apply one approved, deterministic Host capability configuration."""
        from urllib.parse import urlparse

        from backend.core.config import ConfigManager
        from backend.core.errors import error_payload
        from backend.core.loop.capability_negotiation import build_capability_snapshot

        capability = str(args.get("capability") or "").strip().lower()
        if capability != "web_search":
            return error_payload(
                "PROVIDER_CAPABILITY_UNAVAILABLE",
                message=f"Gitgo has no configurable adapter for capability {capability!r}.",
                details={"capability": capability, "supported": ["web_search"]},
                next_actions=[{"action": "choose_supported_capability"}],
            )
        mode = str(args.get("mode") or "").strip().lower()
        if mode not in {"auto", "provider", "searxng", "disabled"}:
            return error_payload(
                "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
                message="Choose auto, provider, searxng, or disabled for web search.",
                details={"mode": mode},
                next_actions=[{"action": "choose_search_mode"}],
            )
        cfg = ConfigManager.load()
        endpoint = str(args.get("endpoint") or cfg.web_search_endpoint or "").strip()
        engine = str(args.get("engine") or cfg.web_search_engine or "duckduckgo").lower()
        if endpoint:
            parsed = urlparse(endpoint)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return error_payload(
                    "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
                    message="The SearXNG endpoint must be an absolute HTTP(S) URL.",
                    details={"field": "endpoint"},
                    next_actions=[{"action": "provide_valid_endpoint"}],
                )
        if engine not in {"google", "bing", "baidu", "yandex", "duckduckgo"}:
            return error_payload(
                "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
                message="The selected SearXNG engine is not supported.",
                details={"engine": engine},
                next_actions=[{"action": "choose_search_engine"}],
            )
        caps = dict(process.runtime_preferences.get("provider_capabilities") or {})
        native = bool(caps.get("hosted_web_search", False))
        if mode == "provider" and not native:
            return error_payload(
                "PROVIDER_CAPABILITY_UNAVAILABLE",
                message=(
                    f"Model {process.model_id or 'unknown'} does not expose hosted web "
                    "search under the current API capability probe."
                ),
                details={
                    "capability": "hosted_web_search", "model_id": process.model_id,
                    "possible_provider_plan_difference": True,
                },
                next_actions=[
                    {"action": "switch_provider"},
                    {"action": "configure_searxng"},
                    {"action": "continue_offline"},
                ],
            )
        if mode == "searxng" and not endpoint:
            return error_payload(
                "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
                message="SearXNG mode requires an endpoint supplied by the user.",
                details={"missing": ["endpoint"]},
                next_actions=[{"action": "configure_searxng"}],
            )
        if mode == "auto" and not native and not endpoint:
            return error_payload(
                "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
                message=(
                    "Auto search needs either provider-hosted search or a configured "
                    "SearXNG endpoint."
                ),
                details={"model_id": process.model_id, "missing": ["fallback_endpoint"]},
                next_actions=[
                    {"action": "switch_provider"},
                    {"action": "configure_searxng"},
                    {"action": "continue_offline"},
                ],
            )
        cfg.web_search_mode = mode
        cfg.web_search_endpoint = endpoint
        cfg.web_search_engine = engine
        ConfigManager.save(cfg)
        process.runtime_preferences.update({
            "web_search_mode": mode,
            "web_search_endpoint": endpoint,
            "web_search_engine": engine,
        })
        snapshot = build_capability_snapshot(
            model_id=str(process.model_id or "unknown"),
            protocol=str(process.runtime_preferences.get("provider_protocol") or "unknown"),
            capabilities=caps,
            runtime_preferences=process.runtime_preferences,
            available_tools=set(process.tool_registry.list_all()),
        )
        return {
            "configured": True,
            "capability": capability,
            "mode": mode,
            "engine": engine,
            "endpoint_configured": bool(endpoint),
            "capability_snapshot": snapshot,
        }

    tools["configure_capability"] = AgentTool(
        execution_contract=data_broker("host.runtime.configure_capability"),
        name="configure_capability",
        description=(
            "Apply a Host capability strategy only after the user has explicitly chosen "
            "it. The Host presents an approval card before changing global configuration. "
            "This never accepts API keys; sensitive provider credentials remain in /config."
        ),
        parameters={
            "type": "object",
            "properties": {
                "capability": {"type": "string", "enum": ["web_search"]},
                "mode": {
                    "type": "string",
                    "enum": ["auto", "provider", "searxng", "disabled"],
                },
                "endpoint": {"type": "string"},
                "engine": {
                    "type": "string",
                    "enum": ["google", "bing", "baidu", "yandex", "duckduckgo"],
                },
            },
            "required": ["capability", "mode"],
        },
        execute=configure_capability,
        read_only=False,
        effect=ToolEffect.PROCESS,
        approval=ApprovalMode.ASK,
        approval_per_invocation=True,
        resources=["config:provider-capabilities"],
        idempotent=True,
    )

    def acknowledge_governance_signal(args: dict) -> dict:
        """Record the model's semantic resolution against Host-owned identity."""
        signal_id = str(args.get("signal_id", "")).strip()
        resolution = str(args.get("resolution", "")).strip()
        if not signal_id or not resolution:
            return {
                "accepted": False,
                "error": "signal_id and resolution are required",
            }
        context, _version = process.read_context_snapshot()
        matching = None
        for signal in context.get("signals", []) or []:
            candidate_id = (
                str(getattr(signal, "signal_id", ""))
                if not isinstance(signal, dict)
                else str(signal.get("signal_id", ""))
            )
            if candidate_id == signal_id:
                matching = signal
                break
        source = (
            str(getattr(matching, "source", ""))
            if matching is not None and not isinstance(matching, dict)
            else str((matching or {}).get("source", ""))
        )
        if matching is None or source != "rejection":
            return {
                "accepted": False,
                "error": "signal is not an active rejection signal",
            }

        evidence_ids = [
            str(item) for item in (args.get("evidence_receipt_ids", []) or [])
            if str(item)
        ]
        known_receipts = {
            str(receipt.get("receipt_id", ""))
            for receipt in process.tool_receipts
            if receipt.get("receipt_id")
            and receipt.get("succeeded") is True
            and receipt.get("committed") is True
        }
        unknown = [item for item in evidence_ids if item not in known_receipts]
        if unknown:
            return {
                "accepted": False,
                "error": "unknown or unsuccessful evidence receipts",
                "receipt_ids": unknown,
            }
        process.governance_resolutions[signal_id] = {
            "signal_id": signal_id,
            "resolution": resolution,
            "evidence_receipt_ids": list(dict.fromkeys(evidence_ids)),
            "resolved_at_step": process.steps_used,
        }
        return {
            "accepted": True,
            **process.governance_resolutions[signal_id],
        }

    tools["acknowledge_governance_signal"] = AgentTool(
        execution_contract=data_broker("host.runtime.acknowledge_governance_signal"),
        name="acknowledge_governance_signal",
        description=(
            "Acknowledge one active rejection signal after semantically addressing it. "
            "Use its exact signal_id and cite any successful Host receipt IDs that prove "
            "the correction; this replaces copying or keyword-matching the instruction."
        ),
        parameters={
            "type": "object",
            "properties": {
                "signal_id": {"type": "string"},
                "resolution": {"type": "string"},
                "evidence_receipt_ids": {
                    "type": "array", "items": {"type": "string"},
                },
            },
            "required": ["signal_id", "resolution", "evidence_receipt_ids"],
        },
        execute=acknowledge_governance_signal,
        effect=ToolEffect.READ,
        idempotent=True,
    )
    if process.actor_kind in {"supervisor", "worker", "reviewer"}:
        tools["request_user_decision"] = AgentTool(
            execution_contract=data_broker("host.runtime.request_user_decision"),
            name="request_user_decision",
            description=(
                "Ask one classified, durable user question after checking Host facts and "
                "already-confirmed state. Use clarification/preference/direction/verification "
                "questions whenever user judgement materially improves the product; questions "
                "may be incremental across the task. Every option must explain principle, "
                "immediate and downstream effects, risks, and reversibility. Use 2-3 business "
                "options; the Host supplies Discuss/Amend separately. Preference, direction, "
                "verification, and checkpoint questions require a stable state_topic."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "clarification", "preference", "direction", "verification",
                            "choice", "recovery", "checkpoint",
                        ],
                    },
                    "state_topic": {
                        "type": "string",
                        "description": (
                            "Stable semantic topic used to replace stale confirmed state; "
                            "required for preference/direction/verification/checkpoint."
                        ),
                    },
                    "supersedes_decision_id": {"type": "string"},
                    "question": {"type": "string"},
                    "why_user_must_decide": {"type": "string"},
                    "options": {
                        "type": "array", "minItems": 2, "maxItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "principle": {"type": "string"},
                                "immediate_effect": {"type": "string"},
                                "downstream_effect": {"type": "string"},
                                "risks": {"type": "string"},
                                "reversibility": {"type": "string"},
                                "recommended": {"type": "boolean"},
                            },
                            "required": [
                                "label", "principle", "immediate_effect",
                                "downstream_effect", "risks", "reversibility",
                            ],
                        },
                    },
                    "allow_free_form": {"type": "boolean"},
                },
                "required": ["question", "why_user_must_decide", "options"],
            },
            execute=lambda args: create_user_decision(process, args),
            effect=ToolEffect.READ,
            idempotent=False,
        )
        from backend.core.loop.permission_broker import create_permission_request
        tools["request_permission"] = AgentTool(
            execution_contract=data_broker("host.runtime.request_permission"),
            name="request_permission",
            description=(
                "Request authority only when a structured Host error explicitly prescribes "
                "this shortcut for an out-of-scope resource or capability expansion. Do not "
                "call it before an ordinary or public-read tool: invoke that tool once and let "
                "the Host allow it or suspend it atomically. Describe the intended outcome in "
                "user terms; the Host binds exact API, target, scope and expiry."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "purpose": {"type": "string"},
                    "tool_name": {"type": "string"},
                    "arguments": {"type": "object"},
                    "resource": {"type": "string"},
                },
                "required": ["purpose", "tool_name", "arguments"],
            },
            # Resolve the tool surface at invocation time.  Authored tools are
            # mounted after this broker tool is constructed, so capturing a
            # merged dictionary here would make every later permission request
            # report the freshly mounted tool as unknown.
            execute=lambda args: create_permission_request(
                process,
                args,
                {**available_tools, **tools, **process.dynamic_tools},
                workspace_path,
            ),
            effect=ToolEffect.READ,
            idempotent=False,
        )

    manager = getattr(process, "_manager", None)
    if process.actor_kind in {"worker", "reviewer"}:
        def publish_interface_update(args: dict) -> dict:
            if manager is None:
                return {"accepted": False, "error": "AgentProcessManager is unavailable"}
            from backend.core.loop.coordination import observe_interface_changes
            try:
                result = observe_interface_changes(
                    manager,
                    process,
                    workspace_path or process.worktree_path,
                    summary=str(args.get("summary") or ""),
                    compatibility=str(args.get("compatibility") or "unknown"),
                    requested_refs=[
                        str(item) for item in list(args.get("interfaces") or [])
                    ],
                )
            except (OSError, ValueError) as exc:
                return {"accepted": False, "error": str(exc)}
            if on_stream_event is not None:
                for event in result.get("events", []):
                    on_stream_event({
                        "event": "coordination_event",
                        "process_id": process.process_id,
                        **event,
                        "_trace_detail": event,
                    })
            return {"accepted": True, **result}

        tools["publish_interface_update"] = AgentTool(
            execution_contract=data_broker("host.runtime.publish_interface_update"),
            name="publish_interface_update",
            description=(
                "Inspect this B's declared output interfaces and publish any changed "
                "version through the Host. The Host updates interface documentation, "
                "notifies A and affected dependent B Agents, and blocks stale downstream "
                "work until A resolves the revision. This never opens peer chat."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "interfaces": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                    "compatibility": {
                        "type": "string",
                        "enum": ["compatible", "breaking", "unknown"],
                    },
                },
                "required": ["summary", "compatibility"],
            },
            execute=publish_interface_update,
            read_only=True,
            effect=ToolEffect.READ,
            idempotent=True,
        )

        def escalate_to_supervisor(args: dict) -> dict:
            if manager is None:
                return {"accepted": False, "error": "AgentProcessManager is unavailable"}
            from backend.core.loop.coordination import publish_coordination_event
            try:
                event = publish_coordination_event(
                    manager,
                    process,
                    kind=str(args.get("kind") or "coordination_request"),
                    summary=str(args.get("summary") or ""),
                    details={
                        "evidence": list(args.get("evidence") or []),
                        "proposed_action": str(args.get("proposed_action") or ""),
                        "requires_user_decision": bool(args.get("requires_user_decision", False)),
                        "user_question": str(args.get("user_question") or ""),
                    },
                    affected_interfaces=[
                        str(item) for item in list(args.get("affected_interfaces") or [])
                    ],
                    requires_supervisor_action=True,
                    block_affected=str(args.get("kind") or "") == "dependency_change",
                )
            except ValueError as exc:
                return {"accepted": False, "error": str(exc)}
            if on_stream_event is not None:
                on_stream_event({
                    "event": "coordination_event",
                    "process_id": process.process_id,
                    **event,
                    "_trace_detail": event,
                })
            return {"accepted": True, **event}

        tools["escalate_to_supervisor"] = AgentTool(
            execution_contract=data_broker("host.runtime.escalate_to_supervisor"),
            name="escalate_to_supervisor",
            description=(
                "Escalate a semantic boundary, dependency, blocking, or stale-intent issue "
                "to the governing A through the Host. Supply evidence and a proposed action. "
                "Set requires_user_decision only when A may need to ask the user; B never "
                "messages a sibling or changes another task directly."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": [
                        "dependency_change", "interface_question", "requirement_ambiguity",
                        "blocked", "stale_user_intent", "coordination_request",
                    ]},
                    "summary": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "object"}},
                    "affected_interfaces": {"type": "array", "items": {"type": "string"}},
                    "proposed_action": {"type": "string"},
                    "requires_user_decision": {"type": "boolean"},
                    "user_question": {"type": "string"},
                },
                "required": ["kind", "summary", "evidence", "proposed_action"],
            },
            execute=escalate_to_supervisor,
            read_only=True,
            effect=ToolEffect.READ,
            idempotent=False,
        )

    if process.actor_kind == "supervisor":
        def list_coordination_events(args: dict) -> dict:
            from backend.core.loop.coordination import current_coordination_events
            status_filter = str(args.get("status") or "pending")
            events = current_coordination_events(process.session.host_ledger)
            if status_filter == "pending":
                events = [
                    item for item in events
                    if str(item.get("status") or "pending") in {
                        "pending", "awaiting_supervisor", "awaiting_user",
                        "rework_requested",
                    }
                ]
            elif status_filter != "all":
                events = [
                    item for item in events
                    if str(item.get("status") or "") == status_filter
                ]
            return {"events": events[-64:], "count": len(events)}

        tools["list_coordination_events"] = AgentTool(
            execution_contract=data_broker("host.runtime.list_coordination_events"),
            name="list_coordination_events",
            description=(
                "List Host-routed worker escalation and dependency-interface events owned "
                "by this A. Use pending for decisions still needed or all for audit history."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending", "all", "awaiting_supervisor", "awaiting_user",
                            "rework_requested", "accepted", "resolved", "restored",
                            "dismissed",
                        ],
                    },
                },
                "required": [],
            },
            execute=list_coordination_events,
            read_only=True,
            effect=ToolEffect.READ,
            idempotent=True,
        )

        def resolve_coordination(args: dict) -> dict:
            if manager is None:
                return {"accepted": False, "error": "AgentProcessManager is unavailable"}
            from backend.core.errors import error_payload
            from backend.core.loop.coordination import resolve_coordination_event
            try:
                event = resolve_coordination_event(
                    manager,
                    process,
                    str(args.get("event_id") or ""),
                    disposition=str(args.get("disposition") or ""),
                    note=str(args.get("note") or ""),
                )
            except KeyError:
                return {"accepted": False, **error_payload(
                    "COORDINATION_EVENT_NOT_FOUND",
                    next_actions=[{"action": "list_coordination_events", "effect": "Refresh owned event IDs."}],
                )}
            except ValueError:
                return {"accepted": False, **error_payload(
                    "COORDINATION_RESOLUTION_INVALID",
                    next_actions=[{
                        "action": "choose_resolution",
                        "effect": "Use accept_revision/request_rework/ask_user for interface revisions; acknowledge or dismiss other events.",
                    }],
                )}
            if on_stream_event is not None:
                on_stream_event({
                    "event": "coordination_event_resolved",
                    "process_id": process.process_id,
                    **event,
                    "_trace_detail": event,
                })
            response = {"accepted": True, **event}
            if event.get("status") == "awaiting_user":
                response["next_actions"] = [{
                    "action": "request_user_decision",
                    "effect": "Ask the user with concrete options, principles, immediate effects and downstream effects, then resolve this event from the answer.",
                }]
            elif event.get("status") == "rework_requested":
                response["next_actions"] = [{
                    "action": "send_feedback_or_continue_original_worker",
                    "effect": "Use send_feedback while the source B is running; otherwise continue_process_id preserves its session for rework.",
                }]
            return response

        tools["resolve_coordination_event"] = AgentTool(
            execution_contract=data_broker("host.runtime.resolve_coordination_event"),
            name="resolve_coordination_event",
            description=(
                "Resolve a Host-routed coordination event. Interface revisions require "
                "accept_revision, request_rework, or ask_user; accepting mechanically "
                "updates the versioned interface document and releases affected downstream "
                "B Agents. This is the only route for changing a cross-B task boundary."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "disposition": {"type": "string", "enum": [
                        "accept_revision", "request_rework", "ask_user",
                        "acknowledge", "dismiss",
                    ]},
                    "note": {"type": "string"},
                },
                "required": ["event_id", "disposition", "note"],
            },
            execute=resolve_coordination,
            read_only=True,
            effect=ToolEffect.READ,
            idempotent=False,
        )

        def declare_task_contract(args: dict) -> dict:
            from backend.core.loop.task_contract import (
                publish_contract,
                validate_contract_proposal,
            )
            try:
                proposal = validate_contract_proposal(
                    args, workspace_path or process.worktree_path,
                )
                adopted = proposal.get("adopt_process_ids", [])
                if len(adopted) > 64:
                    raise ValueError("at most 64 previous executions may be adopted")
                for child_id in adopted:
                    if manager is None or not manager.owns_child(process, manager.get(child_id)):
                        raise ValueError("adopt_process_ids must identify owned executions from list_agents")
                    if child_id not in process.delegated_contracts or process.delegated_contracts[child_id].get("superseded_by"):
                        raise ValueError("cannot adopt a missing or superseded task contract")
            except (TypeError, ValueError) as exc:
                return {
                    "error": "INVALID_TASK_CONTRACT",
                    "detail": str(exc),
                    "accepted": False,
                }
            try:
                contract = publish_contract(process, proposal)
            except ValueError as exc:
                return {
                    "error": "INVALID_TASK_ROUTING",
                    "detail": str(exc),
                    "accepted": False,
                    "routing_advice": {
                        "recommended": "self_execute",
                        "next_action": "request_self_execute",
                    },
                }
            from backend.core.loop.task_contract import routing_advice
            if proposal.get("engineering_workflow"):
                engineering.notice("WORKFLOW_CONFIGURED", "工程工作流已随任务契约启用；未完成证据会进入 Host 完成检查。")
            with process._coordination_lock:
                for child_id in adopted:
                    process.delegated_contracts[child_id]["required_for_parent_completion"] = True
            return {
                "accepted": True,
                "contract": contract,
                "host_validated": True,
                "authority_granted": False,
                "routing_advice": routing_advice(contract),
                "routing_compilation": dict(contract.get("routing_compilation") or {}),
                "budget_card": (
                    process.task_budget.decision_card(process.process_id)
                    if process.task_budget is not None else None
                ),
            }

        tools["declare_task_contract"] = AgentTool(
            execution_contract=data_broker("host.runtime.declare_task_contract"),
            name="declare_task_contract",
            description=(
                "Compile your semantic understanding of a non-trivial user request "
                "into a Host-validated task contract before effectful or delegated "
                "work. This is not keyword classification and grants no authority. "
                "Do not call it for ordinary conversation. Record whether delegation "
                "or concrete workspace artifacts are genuine requirements; list any "
                "unresolved product choice so request_user_decision can handle it. "
                "For bounded single-owner work, choose self_execute. Continue directly "
                "when the delivery is only a response or uses observation-only tools; "
                "request_self_execute only before an actual mutating delivery. Delegation "
                "is valid only for truthful complexity, "
                "multiple workstreams, explicit handoff/parallelization, or a user's "
                "manual B request. When the user explicitly asks for a subprocess/B, "
                "set user_requested_delegation=true and quote the relevant request in "
                "user_request_evidence; do not infer this flag merely because delegation "
                "would be convenient. If the wording is ambiguous, put that ambiguity in "
                "uncertainties and ask the user instead of guessing. "
                "The result includes a deterministic Budget Card. Treat a B as having "
                "setup and coordination cost: persisting or testing an answer A already "
                "derived is still bounded single-owner work, not a separate workstream. "
                "For continuing/reviewing prior work, select adopt_process_ids from "
                "list_agents to make those outcomes required for this turn. Unrelated "
                "new tasks must not inherit old delivery obligations automatically."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "execution_mode": {
                        "type": "string",
                        "enum": [
                            "answer", "delegate", "self_execute", "either",
                            "plan", "review",
                        ],
                    },
                    "delegation_required": {"type": "boolean"},
                    "adopt_process_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
                    "minimum_delegated_outcomes": {
                        "type": "integer", "minimum": 0, "maximum": 8,
                    },
                    "deliverables": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": ["workspace_file", "response", "other"],
                                },
                                "path": {"type": "string"},
                                "description": {"type": "string"},
                                "required": {"type": "boolean"},
                                "allow_inline_substitution": {"type": "boolean"},
                            },
                            "required": ["kind", "required"],
                        },
                    },
                    "acceptance_criteria": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "uncertainties": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "requires_user_decision": {"type": "boolean"},
                    "engineering_workflow": {"type": "object", "description": "Optional Host-enforced practice plan: profiles and evidence nodes with depends_on; use engineering_workflow status to discover supported practices."},
                    "estimated_complexity": {
                        "type": "string", "enum": ["bounded", "moderate", "high"],
                    },
                    "independent_workstreams": {
                        "type": "integer", "minimum": 1, "maximum": 8,
                    },
                    "delegation_rationale": {"type": "string"},
                    "user_requested_delegation": {"type": "boolean"},
                    "user_request_evidence": {"type": "string"},
                    "routing_transition": {
                        "type": "string",
                        "enum": [
                            "initial", "keep_supervisor", "delegate_initial",
                            "handoff_to_worker", "continue_owner", "parallelize",
                        ],
                    },
                    "handoff_summary": {"type": "string"},
                    "required_tool_calls": {
                        "type": "array",
                        "description": (
                            "Optional domain-tool receipt requirements. Never list complete_task, "
                            "complete_supervision, or complete_review: completion protocols are "
                            "selected by current Host state and cannot be acceptance evidence."
                        ),
                        "maxItems": 32,
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool_name": {"type": "string"},
                                "min_calls": {"type": "integer", "minimum": 0, "maximum": 100},
                                "max_calls": {"type": "integer", "minimum": 0, "maximum": 100},
                                "include_composite_steps": {"type": "boolean"},
                            },
                            "required": ["tool_name"],
                        },
                    },
                },
                "required": [
                    "goal", "execution_mode", "delegation_required",
                    "deliverables", "acceptance_criteria", "uncertainties",
                    "requires_user_decision", "user_requested_delegation",
                    "user_request_evidence",
                ],
            },
            execute=declare_task_contract,
            read_only=False,
            effect=ToolEffect.PROCESS,
            idempotent=False,
        )

        def complete_supervision(args: dict) -> dict:
            result = str(args.get("result", "")).strip()
            if not result:
                return {"accepted": False, "error": "result is required"}
            # Keep the public synthesis separate from process evidence even
            # though supervisor completion is checked from authoritative Host
            # child/review facts.  Persisting the structured claim makes the
            # boundary inspectable without printing it to the user.
            try:
                process.completion_claim = CompletionClaim.from_args(
                    {
                        "result": result,
                        "verification": list(args.get("verification") or []),
                        "files": list(args.get("files") or []),
                        "diff_summary": str(args.get("diff_summary") or ""),
                    },
                    step=process.steps_used,
                )
            except ValueError as exc:
                return {"accepted": False, "error": str(exc)}
            requested = str(args.get("status") or "auto").strip().lower()
            terminal_status = "completed"
            if requested == "failed":
                terminal_status = "failed"
            elif requested == "auto":
                manager = getattr(process, "_manager", None)
                _child_ids, contracts, reviews = process.coordination_snapshot()
                for child_id, contract in contracts.items():
                    if not contract.get("required_for_parent_completion", True):
                        continue
                    if contract.get("superseded_by"):
                        continue
                    child = manager.get(child_id) if manager else None
                    review = reviews.get(child_id, {})
                    if (
                        child is not None
                        and child.status.value not in {"running", "waiting"}
                        and (
                            child.status.value != "completed"
                            or review.get("verdict") == "changes_required"
                        )
                    ):
                        terminal_status = "failed"
                        break
            response = {"accepted": True, "result": result}
            if terminal_status == "failed":
                response["terminal_status"] = "failed"
            return response

        tools["complete_supervision"] = AgentTool(
            execution_contract=data_broker("host.runtime.complete_supervision"),
            name="complete_supervision",
            description=(
                "Submit the final A-level synthesis. The Host verifies every "
                "required B is terminal and explicitly reviewed, then closes the "
                "supervisor task without a TASK_COMPLETE password. If a required "
                "delivery failed, review it as changes_required and submit the "
                "failure report here; status=auto lets the Host derive failure."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "result": {"type": "string", "description": (
                        "User-facing outcome: what was delivered and any material limitation. "
                        "Do not enumerate tests, searches, retries, receipts, hashes, process IDs "
                        "or governance procedures. Put those facts in verification unless the user "
                        "explicitly requested a process audit."
                    )},
                    "verification": {
                        "type": "array", "items": {"type": "object"},
                        "description": (
                            "Internal verification and audit facts. This field is persisted but "
                            "is not rendered as the final public answer."
                        ),
                    },
                    "files": {"type": "array", "items": {"type": "string"}},
                    "diff_summary": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["auto", "completed", "failed"],
                        "default": "auto",
                    },
                },
                "required": ["result"],
            },
            execute=complete_supervision,
            read_only=True,
            effect=ToolEffect.READ,
            idempotent=False,
        )

        def request_self_execute(args: dict) -> dict:
            requested_profile = str(args.get("profile_id") or "").strip()
            profile_id = requested_profile
            try:
                requested_definition = (
                    CapabilityProfiles.get(requested_profile)
                    if requested_profile else None
                )
            except ValueError:
                requested_definition = None
            if (
                requested_definition is None
                or not requested_definition.allows_self_execution
            ):
                from backend.core.loop.task_contract import get_task_contract
                contract = get_task_contract(process)
                workspace_delivery = any(
                    str(item.get("kind") or "") == "workspace_file"
                    and bool(item.get("required", True))
                    for item in list(contract.get("deliverables") or [])
                    if isinstance(item, dict)
                )
                profile_id = (
                    "development.workspace"
                    if workspace_delivery else "governance.operate"
                )
            try:
                lease = CapabilityProfiles.issue_self_execute_lease(
                    actor_kind=process.actor_kind,
                    task_id=process.active_task_id or process.process_id,
                    requested_by=process.process_id,
                    profile_id=profile_id,
                    reason=str(args.get("reason", "")),
                    intended_actions=list(args.get("intended_actions", []) or []),
                )
            except (ValueError, PermissionError) as exc:
                return {"error": str(exc), "granted": False}
            process.capability_lease = lease
            # A lease grants a capability; it is not evidence that an action
            # happened.  The Host promotes the task only after a committed
            # mutating receipt is observed.  This keeps research/answer turns
            # out of action completion and reviewer loops while preserving
            # strict evidence gates for real writes and external mutations.
            names = CapabilityProfiles.resolve_tools(
                process.capability_profile_id, lease=lease,
            )
            from backend.core.loop.tools import ToolRegistry
            process.tool_registry = ToolRegistry(names)
            response = {
                "lease_id": lease.lease_id,
                "profile_id": lease.profile_id,
                "task_id": lease.task_id,
                "granted": True,
            }
            if requested_profile and requested_profile != profile_id:
                response["profile_normalized_from"] = requested_profile
            return response

        tools["request_self_execute"] = AgentTool(
            execution_contract=data_broker("host.runtime.request_self_execute"),
            name="request_self_execute",
            description=(
                "Explicitly request the task-scoped capability lease selected by an "
                "accepted self_execute contract. Call this when the Host routing result "
                "says request_self_execute: the current pre-lease surface intentionally "
                "does not yet contain file mutation, command or test tools. A successful "
                "lease rebuilds the surface with those tools. Use development.workspace "
                "for files, commands or tests; governance.operate for governance-only "
                "state changes. This explicit call is required and never implied by prose."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "profile_id": {
                        "type": "string",
                        "enum": ["development.workspace", "governance.operate"],
                        "description": (
                            "Optional least-privilege lease. Use development.workspace "
                            "for files, commands or tests; governance.operate for a "
                            "governance-only mutation. The Host derives a safe default "
                            "from the structured contract when omitted."
                        ),
                    },
                    "reason": {"type": "string"},
                    "intended_actions": {
                        "type": "array", "items": {"type": "string"},
                    },
                },
                "required": ["reason", "intended_actions"],
            },
            execute=request_self_execute,
            read_only=True,
            effect=ToolEffect.READ,
            idempotent=False,
        )

        def prepare_task_bundle(args: dict) -> dict:
            goal = str(args.get("goal", "")).strip()
            if not goal:
                return {"error": "goal is required"}
            dossier_result = code_dossier(args)
            dossier = json.loads(
                dossier_store.resolve(dossier_result["pinned"]).content
            )
            plan = suggest_task_shards(
                dossier,
                goal=goal,
                max_tokens_per_shard=int(
                    args.get("max_tokens_per_shard", 12000) or 12000
                ),
                max_files_per_shard=int(
                    args.get("max_files_per_shard", 24) or 24
                ),
                max_shards=int(args.get("max_shards", 8) or 8),
            )
            return {**dossier_result, "bundle": plan}

        tools["prepare_task_bundle"] = AgentTool(
            execution_contract=data_broker("host.runtime.prepare_task_bundle"),
            name="prepare_task_bundle",
            description=(
                "Ask the Host to size a code task and deterministically suggest stable "
                "file shards. Use before manually classifying or copying large file lists."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "target_files": {"type": "array", "items": {"type": "string"}},
                    "max_files": {"type": "integer"},
                    "max_tokens_per_shard": {"type": "integer"},
                    "max_files_per_shard": {"type": "integer"},
                    "max_shards": {"type": "integer"},
                },
                "required": ["goal", "target_files"],
            },
            execute=prepare_task_bundle,
            effect=ToolEffect.READ,
            idempotent=True,
        )

        def _owned_child(process_id: str):
            target = manager.get(process_id) if manager else None
            if manager is None or not manager.owns_child(process, target):
                return None
            return target

        def read_child_artifact(args: dict) -> dict:
            if manager is None:
                return {"error": "AgentProcessManager is unavailable"}
            process_id = str(args.get("process_id") or "")
            child = _owned_child(process_id)
            if child is None:
                raise PermissionError("artifact process must be an owned child task")
            if child.status != ProcessStatus.COMPLETED:
                raise ValueError("child result is not complete")
            if child.worktree.get("isolated"):
                return manager.read_sealed_artifact(
                    process,
                    process_id,
                    str(args.get("path") or ""),
                    sha256=str(args.get("sha256") or ""),
                    offset=int(args.get("offset", 0) or 0),
                    max_chars=int(args.get("max_chars", 24000) or 24000),
                )
            # Non-Git projects deliberately use the shared workspace.  Keep
            # the same owner/completion boundary, then reuse the canonical
            # bounded artifact reader instead of advertising an immutable
            # worktree API that can only fail in this mode.
            result = tools["artifact_read"].execute({
                "path": str(args.get("path") or ""),
                "sha256": str(args.get("sha256") or ""),
                "offset": int(args.get("offset", 0) or 0),
                "max_chars": int(args.get("max_chars", 24000) or 24000),
            })
            return {
                "process_id": child.process_id,
                "authority": "shared_workspace_current",
                **result,
            }

        tools["read_child_artifact"] = AgentTool(
            execution_contract=data_broker("host.runtime.read_child_artifact"),
            name="read_child_artifact",
            description=(
                "Read a file produced by an owned completed subprocess before review. "
                "The Host uses its immutable sealed result in isolated Git worktrees and "
                "the current bounded workspace artifact in shared non-Git projects."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "process_id": {"type": "string"},
                    "path": {"type": "string"},
                    "sha256": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "max_chars": {"type": "integer", "minimum": 1000},
                },
                "required": ["process_id", "path"],
            },
            execute=read_child_artifact,
            read_only=True,
            effect=ToolEffect.READ,
            idempotent=True,
        )

        def _spawn_child(
            *,
            task_description: str,
            profile_id: str,
            task_kind: str,
            max_steps: int,
            target_files: list[str],
            acceptance_criteria: list[str],
            allowed_tools: list[str] | None = None,
            tool_scope_mode: str = "profile",
            required_test_ids: list[str] | None = None,
            required_tool_calls: list[dict] | None = None,
            depends_on: list[str] | None = None,
            input_interfaces: list[str] | None = None,
            output_interfaces: list[str] | None = None,
            interface_contract: dict | None = None,
            required_for_parent_completion: bool = True,
            supersedes_process_id: str = "",
            continue_process_id: str = "",
            display_name: str = "",
            actor_kind: str = "worker",
            review_target_id: str = "",
            budget_request: dict | None = None,
        ):
            if manager is None:
                raise RuntimeError("AgentProcessManager is unavailable")
            if llm_provider is None or dispatcher is None:
                raise RuntimeError("delegate runtime is missing provider or tool dispatcher")
            dependency_ids = list(dict.fromkeys(
                str(item) for item in (depends_on or []) if str(item)
            ))
            for upstream_id in dependency_ids:
                if _owned_child(upstream_id) is None:
                    raise PermissionError(
                        "depends_on must reference an owned sibling task"
                    )
            profile = CapabilityProfiles.get(profile_id)
            if profile.actor_kind.value != actor_kind:
                raise PermissionError(
                    f"profile {profile_id} is not valid for {actor_kind}"
                )
            previous_contract = None
            continuation = None
            if continue_process_id:
                from backend.core.loop.task_contract import get_task_contract
                requirements = get_task_contract(process).get("host_requirements") or {}
                if continue_process_id in requirements.get("excluded_process_ids", []):
                    raise ValueError("This manual create request requires a new B session; omit continue_process_id")
                continuation = _owned_child(continue_process_id)
                if continuation is None or continuation.actor_kind != actor_kind:
                    raise ValueError("continue_process_id must identify an owned agent of the same kind; inspect list_agents")
                if supersedes_process_id and supersedes_process_id != continue_process_id:
                    raise ValueError("continuation must supersede the same previous execution")
                if continuation.worktree.get("isolated") and not continuation.worktree.get("promoted"):
                    raise ValueError("CONTINUATION_WORKTREE_UNPUBLISHED: review and promote the previous result, or explicitly choose a fresh task; unmerged edits will not be silently discarded")
                if continuation.worktree.get("isolated") and process.worktree.get("snapshot_commit"):
                    raise ValueError("CONTINUATION_SNAPSHOT_PINNED: this DAG still pins its original snapshot; start a new supervisor turn after promotion to continue from the updated workspace")
                supersedes_process_id = continue_process_id
            if supersedes_process_id:
                previous = _owned_child(supersedes_process_id)
                previous_contract = process.delegated_contracts.get(supersedes_process_id)
                if previous is None or previous_contract is None:
                    raise ValueError("supersedes_process_id is not an owned delegated task")
                if previous.status in {
                    ProcessStatus.RUNNING, ProcessStatus.WAITING,
                    ProcessStatus.CANCELLING, ProcessStatus.AWAITING_USER,
                }:
                    raise ValueError("cannot supersede a non-terminal delegated task")
            normalised_targets = []
            workspace_root = Path(workspace_path or process.worktree_path).resolve()
            for item in target_files:
                value = str(item).strip().replace("\\", "/")
                while value.startswith("./"):
                    value = value[2:]
                if value == "*":
                    normalised_targets.append("*")
                    continue
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = workspace_root / candidate
                try:
                    value = str(
                        candidate.resolve(strict=False).relative_to(workspace_root)
                    ).replace("\\", "/")
                except ValueError as exc:
                    raise PermissionError(
                        f"target file escapes workspace: {item}"
                    ) from exc
                if value and value not in normalised_targets:
                    normalised_targets.append(value)
            claims = [f"filesystem:{item}" for item in normalised_targets]
            if not claims and profile_id == "development.workspace":
                claims = ["filesystem:*"]
            if not claims and actor_kind == "reviewer":
                # A review needs a stable workspace snapshot.  The exclusive
                # wildcard waits for all active writers and blocks new ones.
                claims = ["filesystem:*"]
            profile_tools = CapabilityProfiles.resolve_tools(profile_id)
            requested_tools = [
                str(item).strip() for item in (allowed_tools or [])
                if str(item).strip()
            ]
            unknown_tools = sorted(set(requested_tools) - set(profile_tools))
            if unknown_tools:
                raise PermissionError(
                    "task-scoped tools exceed capability profile: "
                    + ", ".join(unknown_tools)
                )
            tool_scope_mode = str(tool_scope_mode or "profile").strip().lower()
            if tool_scope_mode not in {"profile", "test_only"}:
                raise PermissionError("tool_scope_mode must be profile or test_only")
            if requested_tools:
                effective_tools = list(dict.fromkeys(requested_tools))
                tool_scope_source = "supervisor_allowlist"
            elif tool_scope_mode == "test_only":
                if not required_test_ids or profile_id != "development.workspace":
                    raise PermissionError(
                        "test_only scope requires a development.workspace contract "
                        "with required_test_ids"
                    )
                # A registered-test contract is mechanically executable.  Do
                # not give the worker broad inspection/edit tools that invite
                # preliminary scans or unrelated workspace changes.
                effective_tools = ["run_test"]
                tool_scope_source = "host_registered_test_contract"
            elif normalised_targets and profile_id == "governance.observe":
                # Exact target files make full-workspace scan/status/list calls
                # unnecessary.  Retain the bounded read and governance queries.
                bounded_observe = {
                    "read_file", "search_text", "artifact_read", "dependency_query",
                    "context_open", "context_search", "contract_detect_drift",
                    "contract_get_impact", "contract_get_changed_symbols",
                    "lesson_search", "lesson_list", "calculate", "decision_evidence",
                }
                effective_tools = [
                    name for name in profile_tools if name in bounded_observe
                ]
                tool_scope_source = "host_bounded_target_contract"
            else:
                effective_tools = profile_tools
                tool_scope_source = "capability_profile"
            if required_test_ids and "run_test" not in effective_tools:
                raise PermissionError(
                    "a required-test contract must authorize run_test"
                )
            normalized_required_calls: list[dict] = []
            for raw_requirement in list(required_tool_calls or []):
                requirement = dict(raw_requirement or {})
                name = str(requirement.get("tool_name") or "").strip()
                if not name:
                    raise ValueError("required_tool_calls.tool_name is required")
                minimum = int(requirement.get("min_calls", 1) or 0)
                maximum_raw = requirement.get("max_calls")
                maximum = (
                    int(maximum_raw) if maximum_raw is not None else None
                )
                if minimum < 0 or minimum > 100:
                    raise ValueError("required_tool_calls.min_calls must be between 0 and 100")
                if maximum is not None and (maximum < minimum or maximum > 100):
                    raise ValueError(
                        "required_tool_calls.max_calls must be between min_calls and 100"
                    )
                # A dynamic name is legal when this task can define tools.  The
                # Host validates its actual existence through execution receipts.
                if name not in effective_tools and "define_tool" not in effective_tools:
                    raise PermissionError(
                        f"required tool is not authorized by this task: {name}"
                    )
                normalized_required_calls.append({
                    "tool_name": name,
                    "min_calls": minimum,
                    **({"max_calls": maximum} if maximum is not None else {}),
                    "include_composite_steps": bool(
                        requirement.get("include_composite_steps", False)
                    ),
                })
            if "run_test" in effective_tools:
                for claim in (
                    "process:pytest", "filesystem:.gitgo/test_manifest.json",
                ):
                    if claim not in claims:
                        claims.append(claim)
            parent_context, _parent_context_version = process.read_context_snapshot()
            from backend.core.loop.context_policy import context_admission_policy
            from backend.core.loop.coordination import relationship_policy
            relation = relationship_policy(
                owner_process_id=process.process_id,
                depends_on=dependency_ids,
                continuation_process_id=continue_process_id,
                required_for_parent_completion=required_for_parent_completion,
                capability_profile_id=profile_id,
                task_kind=task_kind,
            )
            child_context = {
                "signals": list(parent_context.get("signals", []) or []),
                "evidence_candidates": list(parent_context.get("evidence_candidates", []) or []),
                "brief": str(parent_context.get("brief", "")),
                "project_name": str(parent_context.get("project_name", "")),
                "workspace_path": str(parent_context.get("workspace_path", "")),
                "context_refs": dict(parent_context.get("context_refs", {}) or {}),
                "context_policy": context_admission_policy(actor_kind, task_kind),
                "relationship_policy": relation,
                "task_contract": {
                    "acceptance_criteria": acceptance_criteria,
                    "target_files": target_files,
                    "parent_process_id": process.process_id,
                    "required_test_ids": list(required_test_ids or []),
                    "required_tool_calls": normalized_required_calls,
                    "allowed_tools": list(effective_tools),
                    "tool_scope_source": tool_scope_source,
                    "tool_scope_mode": tool_scope_mode,
                    "depends_on": dependency_ids,
                    "input_interfaces": list(input_interfaces or []),
                    "output_interfaces": list(output_interfaces or []),
                    "interface_contract": dict(interface_contract or {}),
                    "relationship_policy": relation,
                },
            }
            child = manager.fork(
                parent_id=process.process_id,
                role="reviewer" if actor_kind == "reviewer" else "executor",
                tool_registry=__import__(
                    "backend.core.loop.tools", fromlist=["ToolRegistry"]
                ).ToolRegistry(effective_tools),
                max_steps=max(1, min(max_steps, 200)),
                ring_level=RingLevel.RING_3,
                context_snapshot=child_context,
                task_description=task_description,
                task_id=process.allocate_child_task_id(),
                workspace_path=workspace_path or process.worktree_path,
                provider_id=process.provider_id,
                model_id=process.model_id,
                actor_kind=actor_kind,
                capability_profile_id=profile_id,
                task_kind=task_kind,
                required_test_ids=list(required_test_ids or []),
                depends_on=dependency_ids,
                child_budget_request=dict(budget_request or {}),
                session=continuation.session if continuation is not None else None,
            )
            if display_name:
                # A may name its worker; control characters cannot reach the TUI.
                name = " ".join("".join(c for c in display_name if c.isprintable()).split())[:48]
                if name:
                    child.session.display_name = name
            child.review_target_id = review_target_id
            contract = {
                "task_kind": task_kind,
                "acceptance_criteria": list(acceptance_criteria),
                "target_files": list(target_files),
                "required_test_ids": list(required_test_ids or []),
                "required_tool_calls": normalized_required_calls,
                "allowed_tools": list(effective_tools),
                "tool_scope_source": tool_scope_source,
                "tool_scope_mode": tool_scope_mode,
                "required_for_parent_completion": bool(required_for_parent_completion),
                "supersedes_process_id": supersedes_process_id,
                "depends_on": dependency_ids,
                "input_interfaces": list(input_interfaces or []),
                "output_interfaces": list(output_interfaces or []),
                "interface_contract": dict(interface_contract or {}),
                "relationship_policy": relation,
                "budget_lease": dict(child.budget_lease or {}),
            }
            process.register_child_contract(
                child.process_id, contract,
                supersedes_process_id=supersedes_process_id,
            )

            def emit_child_event(event: dict) -> None:
                if on_stream_event is None:
                    return
                enriched = dict(event)
                enriched.setdefault("process_id", child.process_id)
                enriched["parent_process_id"] = process.process_id
                enriched["root_process_id"] = (
                    process.process_id if process.parent_id is None else process.parent_id
                )
                enriched["task_id"] = child.active_task_id
                enriched["actor_kind"] = child.actor_kind
                on_stream_event(enriched)

            def run_child():
                execution_workspace = workspace_path or process.worktree_path
                lifecycle_stage = "start"
                emit_child_event({
                    "event": "agent_started",
                    "status": child.status.value,
                    "capability_profile_id": child.capability_profile_id,
                })
                result = None
                try:
                    lifecycle_stage = "prepare_worktree"
                    review_target = manager.get(review_target_id) if review_target_id else None
                    review_has_snapshot = bool(
                        review_target
                        and (review_target.worktree or {}).get("result_commit")
                    )
                    isolated_code_action = (
                        task_kind == "action"
                        and profile_id == "development.workspace"
                    )
                    if manager.worktree_manager is not None and (
                        isolated_code_action
                        or (actor_kind == "reviewer" and review_has_snapshot)
                    ):
                        execution_workspace = manager.materialize_process_worktree(
                            child, review_target_id=review_target_id,
                        )
                        emit_child_event({
                            "event": "worktree_leased",
                            "worktree": dict(child.worktree),
                        })
                    from backend.core.loop.interface_contract import (
                        verify_declared_contract,
                    )
                    lifecycle_stage = "verify_input_interfaces"
                    boundary_context, _ = child.read_context_snapshot()
                    boundary_contract = dict(
                        dict(boundary_context.get("task_contract") or {}).get("interface_contract")
                        or interface_contract or {}
                    )
                    input_violations = verify_declared_contract(
                        boundary_contract,
                        execution_workspace,
                        list(input_interfaces or []),
                    )
                    if input_violations:
                        child.status = ProcessStatus.FAILED
                        child.result = {
                            "status": "failed",
                            "process_id": child.process_id,
                            "code": "INTERFACE_CONTRACT_VIOLATION",
                            "error": "upstream interface contract was not satisfied",
                            "violations": input_violations,
                        }
                        result = child.result
                        return result
                    lifecycle_stage = "assemble_upstream_context"
                    child_context_now, _ = child.read_context_snapshot()
                    upstream_context = []
                    for upstream in manager.dependency_closure(child):
                        upstream_context.append({
                            "process_id": upstream.process_id,
                            "task_id": upstream.active_task_id,
                            "status": upstream.status.value,
                            "outcome": upstream.result,
                            "result_commit": str(
                                (upstream.worktree or {}).get("result_commit") or ""
                            ),
                        })
                    child_context_now["upstream_outcomes"] = upstream_context
                    if child.worktree:
                        child_context_now["worktree"] = dict(child.worktree)
                    child.replace_context_snapshot(child_context_now)
                    lifecycle_stage = "agent_loop"
                    result = agent_step(
                        process=child,
                        llm_provider=llm_provider,
                        instruction=task_description,
                        dispatcher=dispatcher,
                        workspace_path=execution_workspace,
                        on_stream_event=emit_child_event,
                    )
                    lifecycle_stage = "verify_output_interfaces"
                    boundary_context, _ = child.read_context_snapshot()
                    boundary_contract = dict(
                        dict(boundary_context.get("task_contract") or {}).get("interface_contract")
                        or interface_contract or {}
                    )
                    output_violations = verify_declared_contract(
                        boundary_contract,
                        execution_workspace,
                        list(output_interfaces or []),
                    )
                    if output_violations and child.status == ProcessStatus.COMPLETED:
                        child.status = ProcessStatus.FAILED
                        child.result = {
                            "status": "failed",
                            "process_id": child.process_id,
                            "code": "INTERFACE_CONTRACT_VIOLATION",
                            "error": "declared output interface was not satisfied",
                            "violations": output_violations,
                        }
                        result = child.result
                        return result
                    if (
                        isolated_code_action
                        and child.status == ProcessStatus.COMPLETED
                        and child.worktree.get("isolated")
                    ):
                        lifecycle_stage = "seal_worktree"
                        manager.seal_process_worktree(child)
                        if isinstance(result, dict):
                            result.setdefault("metadata", {})["worktree"] = dict(
                                child.worktree
                            )
                        emit_child_event({
                            "event": "worktree_sealed",
                            "worktree": dict(child.worktree),
                        })
                    if actor_kind == "reviewer" and review_target_id:
                        lifecycle_stage = "record_review_approval"
                        target = manager.get(review_target_id)
                        if (
                            target is not None
                            and child.status == ProcessStatus.COMPLETED
                            and isinstance(child.review_claim, dict)
                            and child.review_claim.get("verdict") == "approved"
                        ):
                            target.review_approvals.append(child.process_id)
                    return result
                except Exception as exc:
                    # The child-owned terminal event is the persistence/UI
                    # barrier.  Convert failures here, before that event is
                    # emitted, rather than relying on Manager's outer thread
                    # wrapper to change status after a false completed event.
                    child.status = ProcessStatus.FAILED
                    child.result = {
                        "status": "failed",
                        "process_id": child.process_id,
                        "code": "CHILD_LIFECYCLE_FAILED",
                        "error": str(exc),
                        "metadata": {
                            "automatic_replay_suppressed": True,
                            "lifecycle_stage": lifecycle_stage,
                            "worktree": dict(child.worktree or {}),
                        },
                    }
                    result = child.result
                    return result
                finally:
                    if actor_kind == "reviewer" and child.worktree.get("isolated"):
                        try:
                            manager.dispose_process_worktree(child, keep_ref=False)
                        except Exception as cleanup_exc:
                            emit_child_event({
                                "event": "worktree_cleanup_failed",
                                "reason": str(cleanup_exc),
                            })
                    emit_child_event({
                        "event": "agent_terminal",
                        "status": child.status.value,
                        "outcome": result or child.result,
                    })

            resource_mode = (
                "exclusive"
                if actor_kind == "reviewer" or profile.allows_self_execution
                else "shared"
            )
            review_process = manager.get(review_target_id) if review_target_id else None
            review_uses_isolated = bool(
                actor_kind == "reviewer"
                and review_process
                and (review_process.worktree or {}).get("result_commit")
            )
            if manager.worktree_manager is not None and (
                (task_kind == "action" and profile_id == "development.workspace")
                or review_uses_isolated
            ):
                # File claims protect shared workspaces. Isolated linked
                # worktrees have distinct data planes, so unrelated writers
                # must not be serialized by their logical target paths.
                claims = [f"worktree:{child.process_id}"]
            manager.start(
                child.process_id,
                run_child,
                resources=claims,
                resource_mode=resource_mode,
            )
            return child

        def delegate_task(args: dict) -> dict:
            from backend.core.errors import error_payload
            from backend.core.loop.task_contract import (
                delegation_intent_id,
                finish_delegation_attempt,
                record_delegation_attempt,
            )
            task_description = str(args.get("task_description", "")).strip()
            criteria = [
                str(item) for item in (args.get("acceptance_criteria", []) or [])
                if str(item).strip()
            ]
            target_files = [
                str(item) for item in (args.get("target_files", []) or [])
                if str(item).strip()
            ]
            profile_id = str(
                args.get("capability_profile_id") or "development.workspace"
            )
            raw_task_kind = str(args.get("task_kind", "")).strip()
            required_for_parent = bool(
                args.get("required_for_parent_completion", True)
            )
            intent_id = delegation_intent_id(
                task_description, target_files, criteria,
            )
            record_delegation_attempt(
                process,
                intent_id=intent_id,
                required=required_for_parent,
                profile_id=profile_id,
                task_kind=raw_task_kind,
                target_files=target_files,
            )

            def reject(name: str, message: str, *, details: dict | None = None,
                       next_actions: list[dict] | None = None) -> dict:
                payload = error_payload(
                    name, message=message, details=details,
                    next_actions=next_actions, state_changed=True,
                )
                finish_delegation_attempt(
                    process, intent_id=intent_id, state="admission_failed",
                    error_info=payload["error_info"],
                )
                return {
                    "delegated": False,
                    "intent_recorded": True,
                    "intent_id": intent_id,
                    **payload,
                }

            try:
                from backend.core.loop.task_contract import get_task_contract, routing_advice
                current_contract = get_task_contract(process)
                host_requirements = current_contract.get("host_requirements") or {}
                if (
                    process.task_kind == "answer"
                    and not current_contract.get("revision")
                    and not bool(host_requirements.get("manual_B_creation"))
                ):
                    return reject(
                        "DELEGATION_ADMISSION_FAILED",
                        "Automatic delegation requires a Host-validated task contract first; declare the semantic contract, then follow its routing advice.",
                        details={
                            "reason": "task_contract_not_declared",
                            "execution_state": "not_started",
                        },
                        next_actions=[{
                            "action": "declare_task_contract",
                            "reason": (
                                "Describe the deliverables, acceptance criteria, complexity, "
                                "independent workstreams and delegation rationale before routing."
                            ),
                        }],
                    )
                route = routing_advice(current_contract)
                if (
                    current_contract.get("revision")
                    and
                    route.get("recommended") == "self_execute"
                    and not bool(host_requirements.get("manual_B_creation"))
                ):
                    return reject(
                        "DELEGATION_ADMISSION_FAILED",
                        "The current structured contract describes bounded single-owner work; use request_self_execute, or revise the contract with a concrete complexity rationale.",
                        details={"routing_advice": route},
                        next_actions=[
                            {"action": "request_self_execute", "profile_id": "development.workspace"},
                            {"action": "revise_task_contract", "required": ["estimated_complexity", "delegation_rationale"]},
                        ],
                    )
                profile_error = CapabilityProfiles.worker_error(profile_id)
                if profile_error:
                    info = profile_error["error_info"]
                    return reject(info["name"], info["message"], details=info["details"],
                                  next_actions=info["next_actions"])
                profile = CapabilityProfiles.get(profile_id)
                task_kind = raw_task_kind or (
                    "action" if profile.allows_self_execution else "answer"
                )
                if task_kind not in {"answer", "plan", "action"}:
                    return reject(
                        "DELEGATION_ADMISSION_FAILED",
                        "delegate_task only accepts answer, plan, or action task kinds.",
                        details={
                            "attempted_value": task_kind,
                            "valid_task_kinds": ["answer", "plan", "action"],
                        },
                        next_actions=[{
                            "action": "retry",
                            "patch": {"task_kind": (
                                "action" if profile.allows_self_execution else "answer"
                            )},
                        }],
                    )
                if task_kind == "action" and not profile.allows_self_execution:
                    return reject(
                        "DELEGATION_ADMISSION_FAILED",
                        f"Capability profile {profile_id} cannot satisfy an action task.",
                        details={"profile_id": profile_id, "task_kind": task_kind},
                        next_actions=[{
                            "action": "retry",
                            "patch": {
                                "capability_profile_id": "development.workspace",
                                "task_kind": "action",
                            },
                        }],
                    )
                if not task_description:
                    return reject(
                        "DELEGATION_ADMISSION_FAILED",
                        "task_description is required.",
                        details={"field": "task_description"},
                    )
                if criteria:
                    task_description += "\n\nAcceptance criteria:\n- " + "\n- ".join(criteria)
                explicit_required_tests = [
                    str(item) for item in (args.get("required_test_ids", []) or [])
                    if str(item).strip()
                ]
                from backend.core.loop.test_manifest import extract_declared_test_ids
                declared_required_tests = extract_declared_test_ids(task_description)
                inherited_required_tests = (
                    list(process.required_test_ids)
                    if task_kind == "action" else []
                )
                required_tests = list(dict.fromkeys(
                    explicit_required_tests
                    + declared_required_tests
                    + inherited_required_tests
                ))
                required_tool_calls = [
                    dict(item or {})
                    for item in (args.get("required_tool_calls", []) or [])
                ]
                allowed_tools = [
                    str(item) for item in (args.get("allowed_tools", []) or [])
                    if str(item).strip()
                ]
                from backend.core.loop.interface_contract import (
                    capture_declared_contract,
                    normalise_interface_ref,
                )
                input_interfaces = list(dict.fromkeys(
                    normalise_interface_ref(item)
                    for item in (args.get("input_interfaces") or [])
                ))
                output_interfaces = list(dict.fromkeys(
                    normalise_interface_ref(item)
                    for item in (args.get("output_interfaces") or [])
                ))
                interface_contract = dict(args.get("_interface_contract") or {})
                if not interface_contract and (input_interfaces or output_interfaces):
                    interface_contract = capture_declared_contract(
                        Path(workspace_path or process.worktree_path).resolve(),
                        [{
                            "node_id": "direct",
                            "input_interfaces": input_interfaces,
                            "output_interfaces": output_interfaces,
                        }],
                    )
                child = _spawn_child(
                    task_description=task_description,
                    profile_id=profile_id,
                    task_kind=task_kind,
                    max_steps=int(args.get("max_steps", 50) or 50),
                    target_files=target_files,
                    acceptance_criteria=criteria,
                    allowed_tools=allowed_tools,
                    tool_scope_mode=str(args.get("tool_scope_mode", "profile")),
                    required_test_ids=required_tests,
                    required_tool_calls=required_tool_calls,
                    depends_on=[
                        str(item) for item in (args.get("depends_on", []) or [])
                    ],
                    input_interfaces=input_interfaces,
                    output_interfaces=output_interfaces,
                    interface_contract=interface_contract,
                    required_for_parent_completion=required_for_parent,
                    supersedes_process_id=str(args.get("supersedes_process_id", "")),
                    continue_process_id=str(args.get("continue_process_id", "")),
                    display_name=str(args.get("display_name", "")),
                    budget_request=dict(args.get("budget_request") or {}),
                )
                finish_delegation_attempt(
                    process, intent_id=intent_id, state="running",
                    child_process_id=child.process_id,
                )
                return {
                    "delegated": True,
                    "intent_id": intent_id,
                    "process_id": child.process_id,
                    "session_id": child.session.session_id,
                    "display_name": child.session.display_name,
                    "status": child.status.value,
                    "profile_id": profile_id,
                    "budget_lease": dict(child.budget_lease or {}),
                }
            except (ValueError, PermissionError, RuntimeError) as exc:
                return reject(
                    "DELEGATION_ADMISSION_FAILED",
                    str(exc),
                    details={
                        "profile_id": profile_id,
                        "task_kind": raw_task_kind,
                        "valid_worker_profiles": CapabilityProfiles.describe(
                            actor_kind="worker"
                        ),
                    },
                )

        tools["delegate_task"] = AgentTool(
            execution_contract=data_broker("host.runtime.delegate_task"),
            name="delegate_task",
            description=(
                "Delegate an executor-ready task contract to an independent B Agent. "
                "The host starts it asynchronously and serializes overlapping writes. "
                "Every new B escrows part of the task-tree provider/output budget. "
                "Do not delegate routine file persistence or testing after A has already "
                "derived the solution; request a self-execution lease instead. "
                + ("Default routing: for related changes/iterations inspect list_agents and "
                   "set continue_process_id to the original terminal B, preserving its context. "
                   "Use send_feedback if it is still running. Only create a new B for distinct "
                   "responsibility or when explicitly justified. "
                   if process.runtime_preferences.get("agent_routing", "owner") == "owner"
                   else "The user prefers a fresh B for each new task; explicit continuation remains available. ")
                + "display_name is an optional short human-readable worker name."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_description": {"type": "string"},
                    "capability_profile_id": {
                        "type": "string",
                        "enum": list(CapabilityProfiles.ids(actor_kind="worker")),
                        "default": "development.workspace",
                    },
                    "task_kind": {
                        "type": "string",
                        "enum": ["answer", "plan", "action"],
                    },
                    "target_files": {"type": "array", "items": {"type": "string"}},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "required_test_ids": {"type": "array", "items": {"type": "string"}},
                    "required_tool_calls": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool_name": {"type": "string"},
                                "min_calls": {"type": "integer"},
                                "max_calls": {"type": "integer"},
                                "include_composite_steps": {"type": "boolean"},
                            },
                            "required": ["tool_name"],
                        },
                    },
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "input_interfaces": {"type": "array", "items": {"type": "string"}},
                    "output_interfaces": {"type": "array", "items": {"type": "string"}},
                    "allowed_tools": {"type": "array", "items": {"type": "string"}},
                    "tool_scope_mode": {
                        "type": "string", "enum": ["profile", "test_only"],
                    },
                    "required_for_parent_completion": {"type": "boolean"},
                    "supersedes_process_id": {"type": "string"},
                    "continue_process_id": {"type": "string"},
                    "display_name": {"type": "string", "maxLength": 48},
                    "max_steps": {"type": "integer"},
                    "budget_request": {
                        "type": "object",
                        "description": (
                            "Optional child escrow request. Omit for the deterministic default; "
                            "request more only when the task contract explains why."
                        ),
                        "properties": {
                            "provider_calls": {"type": "integer", "minimum": 1},
                            "output_tokens": {"type": "integer", "minimum": 256},
                            "steps": {"type": "integer", "minimum": 1, "maximum": 200},
                        },
                        "additionalProperties": False,
                    },
                },
                "required": ["task_description", "target_files", "acceptance_criteria"],
            },
            execute=delegate_task,
            read_only=True,
            effect=ToolEffect.READ,
        )

        def delegate_task_dag(args: dict) -> dict:
            """Validate local node ids, then admit one durable sibling DAG."""
            nodes = [dict(item or {}) for item in (args.get("nodes") or [])]
            if not 1 <= len(nodes) <= 8:
                return {"delegated": False, "error": "nodes must contain 1..8 tasks"}
            node_ids = [str(item.get("node_id") or "").strip() for item in nodes]
            if any(not item for item in node_ids) or len(set(node_ids)) != len(node_ids):
                return {"delegated": False, "error": "node_id values must be unique"}
            by_id = dict(zip(node_ids, nodes))
            from backend.core.loop.interface_contract import (
                capture_declared_contract,
                normalise_interface_ref,
            )
            producers: dict[str, str] = {}
            try:
                for node_id, node in by_id.items():
                    node["input_interfaces"] = list(dict.fromkeys(
                        normalise_interface_ref(item)
                        for item in (node.get("input_interfaces") or [])
                    ))
                    node["output_interfaces"] = list(dict.fromkeys(
                        normalise_interface_ref(item)
                        for item in (node.get("output_interfaces") or [])
                    ))
                    for ref in node["output_interfaces"]:
                        previous = producers.get(ref)
                        if previous and previous != node_id:
                            return {
                                "delegated": False,
                                "error": f"interface {ref} has multiple owners",
                            }
                        producers[ref] = node_id
                # Interface declarations are control-plane data.  Derive the
                # happens-before edge mechanically instead of asking A to copy
                # the same relation into depends_on and risking disagreement.
                for node_id, node in by_id.items():
                    dependencies = list(node.get("depends_on") or [])
                    for ref in node["input_interfaces"]:
                        owner = producers.get(ref)
                        if not owner:
                            return {
                                "delegated": False,
                                "error": f"input interface has no DAG owner: {ref}",
                            }
                        if owner == node_id:
                            return {
                                "delegated": False,
                                "error": f"node {node_id} consumes its own interface: {ref}",
                            }
                        dependencies.append(owner)
                    node["depends_on"] = list(dict.fromkeys(
                        str(item) for item in dependencies if str(item)
                    ))
            except ValueError as exc:
                return {"delegated": False, "error": str(exc)}
            indegree = {node_id: 0 for node_id in node_ids}
            downstream: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
            for node_id, node in by_id.items():
                dependencies = list(dict.fromkeys(
                    str(item) for item in (node.get("depends_on") or []) if str(item)
                ))
                if node_id in dependencies:
                    return {"delegated": False, "error": f"self dependency: {node_id}"}
                unknown = sorted(set(dependencies) - set(node_ids))
                if unknown:
                    return {
                        "delegated": False,
                        "error": f"unknown dependencies for {node_id}: {', '.join(unknown)}",
                    }
                node["depends_on"] = dependencies
                indegree[node_id] = len(dependencies)
                for upstream in dependencies:
                    downstream[upstream].append(node_id)
            ready = sorted(item for item, degree in indegree.items() if degree == 0)
            order: list[str] = []
            while ready:
                node_id = ready.pop(0)
                order.append(node_id)
                for target in sorted(downstream[node_id]):
                    indegree[target] -= 1
                    if indegree[target] == 0:
                        ready.append(target)
                        ready.sort()
            if len(order) != len(nodes):
                return {"delegated": False, "error": "DAG contains a cycle"}
            # Preflight every deterministic contract property before the first
            # child is admitted.  This keeps malformed DAG calls atomic: a bad
            # later node cannot leave earlier workers running or consume task
            # budget merely because validation happened in topological order.
            workspace_root = Path(workspace_path or process.worktree_path).resolve()
            for node_id in order:
                node = by_id[node_id]
                description = str(node.get("task_description") or "").strip()
                if not description:
                    return {
                        "delegated": False,
                        "error": f"node {node_id}: task_description is required",
                    }
                profile_id = str(
                    node.get("capability_profile_id") or "development.workspace"
                )
                profile_error = CapabilityProfiles.worker_error(profile_id)
                if profile_error:
                    return {"delegated": False, "node_id": node_id, **profile_error}
                profile = CapabilityProfiles.get(profile_id)
                task_kind = str(node.get("task_kind") or "").strip() or (
                    "action" if profile.allows_self_execution else "answer"
                )
                if task_kind not in {"answer", "plan", "action"}:
                    return {
                        "delegated": False,
                        "error": f"node {node_id}: invalid task_kind {task_kind}",
                    }
                if task_kind == "action" and not profile.allows_self_execution:
                    return {
                        "delegated": False,
                        "error": (
                            f"node {node_id}: profile {profile_id} cannot execute actions"
                        ),
                    }
                profile_tools = set(CapabilityProfiles.resolve_tools(profile_id))
                requested_tools = {
                    str(item).strip() for item in (node.get("allowed_tools") or [])
                    if str(item).strip()
                }
                unknown_tools = sorted(requested_tools - profile_tools)
                if unknown_tools:
                    return {
                        "delegated": False,
                        "error": (
                            f"node {node_id}: tools exceed profile: "
                            + ", ".join(unknown_tools)
                        ),
                    }
                scope_mode = str(node.get("tool_scope_mode") or "profile").lower()
                required_tests = [
                    str(item) for item in (node.get("required_test_ids") or [])
                    if str(item).strip()
                ]
                if scope_mode not in {"profile", "test_only"}:
                    return {
                        "delegated": False,
                        "error": f"node {node_id}: invalid tool_scope_mode",
                    }
                if scope_mode == "test_only" and (
                    profile_id != "development.workspace" or not required_tests
                ):
                    return {
                        "delegated": False,
                        "error": (
                            f"node {node_id}: test_only needs development.workspace "
                            "and required_test_ids"
                        ),
                    }
                try:
                    max_steps = int(node.get("max_steps", 50) or 50)
                except (TypeError, ValueError):
                    return {
                        "delegated": False,
                        "error": f"node {node_id}: max_steps must be an integer",
                    }
                if max_steps < 1:
                    return {
                        "delegated": False,
                        "error": f"node {node_id}: max_steps must be positive",
                    }
                for raw_target in (node.get("target_files") or []):
                    value = str(raw_target).strip().replace("\\", "/")
                    while value.startswith("./"):
                        value = value[2:]
                    if value == "*":
                        continue
                    candidate = Path(value)
                    if not candidate.is_absolute():
                        candidate = workspace_root / candidate
                    try:
                        candidate.resolve(strict=False).relative_to(workspace_root)
                    except ValueError:
                        return {
                            "delegated": False,
                            "error": f"node {node_id}: target escapes workspace",
                        }
            try:
                interface_contract = capture_declared_contract(
                    workspace_root, [by_id[item] for item in order],
                )
            except ValueError as exc:
                return {"delegated": False, "error": str(exc)}
            for node in nodes:
                node["_interface_contract"] = interface_contract
            if process.task_budget is not None:
                snapshot = process.task_budget.snapshot()
                remaining = (
                    int(snapshot["limits"]["max_agents"])
                    - int(snapshot["used"]["agents"])
                )
                if len(nodes) > remaining:
                    return {
                        "delegated": False,
                        "error": f"DAG needs {len(nodes)} agents but budget has {remaining}",
                    }
            admitted: dict[str, str] = {}
            created: list[str] = []
            for node_id in order:
                node = by_id[node_id]
                result = delegate_task({
                    **node,
                    "depends_on": [admitted[item] for item in node["depends_on"]],
                })
                if result.get("delegated") is False or result.get("error"):
                    for process_id in created:
                        manager.kill(process_id, reason="dag_admission_rollback")
                        contract = process.delegated_contracts.get(process_id)
                        if contract is not None:
                            contract["required_for_parent_completion"] = False
                            contract["admission_rolled_back"] = True
                    return {
                        "delegated": False,
                        "error": f"node {node_id} admission failed: {result.get('error')}",
                        "rolled_back_process_ids": created,
                    }
                admitted[node_id] = str(result["process_id"])
                created.append(str(result["process_id"]))
            if on_stream_event is not None:
                on_stream_event({
                    "event": "agent_dag_admitted",
                    "process_id": process.process_id,
                    "task_id": process.active_task_id,
                    "nodes": admitted,
                    "order": order,
                })
            return {
                "delegated": True,
                "nodes": admitted,
                "topological_order": order,
            }

        tools["delegate_task_dag"] = AgentTool(
            execution_contract=data_broker("host.runtime.delegate_task_dag"),
            name="delegate_task_dag",
            description=(
                "Admit a complete sibling DAG in one Host-validated operation. Use "
                "local node_id/depends_on values; the Host resolves process ids, "
                "schedules ready nodes and injects upstream outcomes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "nodes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "node_id": {"type": "string"},
                                "task_description": {"type": "string"},
                                "depends_on": {"type": "array", "items": {"type": "string"}},
                                "capability_profile_id": {
                                    "type": "string",
                                    "enum": list(CapabilityProfiles.ids(actor_kind="worker")),
                                    "default": "development.workspace",
                                },
                                "task_kind": {
                                    "type": "string",
                                    "enum": ["answer", "plan", "action"],
                                },
                                "target_files": {"type": "array", "items": {"type": "string"}},
                                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                                "required_test_ids": {"type": "array", "items": {"type": "string"}},
                                "required_tool_calls": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "tool_name": {"type": "string"},
                                            "min_calls": {"type": "integer"},
                                            "max_calls": {"type": "integer"},
                                            "include_composite_steps": {"type": "boolean"},
                                        },
                                        "required": ["tool_name"],
                                    },
                                },
                                "input_interfaces": {"type": "array", "items": {"type": "string"}},
                                "output_interfaces": {"type": "array", "items": {"type": "string"}},
                                "allowed_tools": {"type": "array", "items": {"type": "string"}},
                                "max_steps": {"type": "integer"},
                                "required_for_parent_completion": {"type": "boolean"},
                            },
                            "required": [
                                "node_id", "task_description", "depends_on",
                                "target_files", "acceptance_criteria",
                            ],
                        },
                    },
                },
                "required": ["nodes"],
            },
            execute=delegate_task_dag,
            read_only=True,
            effect=ToolEffect.READ,
        )

        def delegate_task_bundle(args: dict) -> dict:
            """Deterministically shard and start a bounded set of worker contracts."""
            children = []
            try:
                profile_id = str(args.get("capability_profile_id") or "governance.observe")
                profile_error = CapabilityProfiles.worker_error(profile_id)
                if profile_error:
                    return {"delegated": False, **profile_error}
                prepared = prepare_task_bundle(args)
                if prepared.get("error"):
                    return {"delegated": False, **prepared}
                plan = prepared["bundle"]
                if plan.get("requires_semantic_partition"):
                    return {
                        "delegated": False,
                        "requires_semantic_partition": True,
                        "reason": (
                            "At least one deterministic shard is still oversized; "
                            "refine target_files or ask the user if the partition changes scope."
                        ),
                        **prepared,
                    }
                shards = list(plan.get("shards", []))
                if not shards:
                    from backend.core.errors import error_payload
                    return {
                        "delegated": False,
                        **error_payload(
                            "DELEGATION_EMPTY_RESULT",
                            message=(
                                "The deterministic bundle found no existing source "
                                "files to shard; no B Agent was started."
                            ),
                            details={
                                "target_files": list(args.get("target_files") or []),
                                "dossier_ref": prepared.get("pinned", ""),
                            },
                            next_actions=[{
                                "action": "use_delegate_task",
                                "reason": (
                                    "Use direct delegation for creating a new file or "
                                    "for any task without existing source shards."
                                ),
                                "patch": {
                                    "capability_profile_id": "development.workspace",
                                    "task_kind": "action",
                                },
                            }],
                        ),
                        **prepared,
                    }
                if process.task_budget is not None:
                    snapshot = process.task_budget.snapshot()
                    remaining = (
                        int(snapshot["limits"]["max_agents"])
                        - int(snapshot["used"]["agents"])
                    )
                    if len(shards) > remaining:
                        return {
                            "delegated": False,
                            "error": (
                                f"bundle needs {len(shards)} agents but task-tree "
                                f"budget has {remaining} remaining"
                            ),
                            **prepared,
                        }
                criteria = [
                    str(item) for item in (args.get("acceptance_criteria", []) or [])
                    if str(item).strip()
                ]
                criteria = list(dict.fromkeys(criteria + [
                    "Report semantic findings with precise file:line evidence",
                    "Do not claim files outside the assigned deterministic shard were inspected",
                    "Use the pinned shard handoff before requesting any additional context",
                ]))
                task_kind = str(args.get("task_kind", "answer"))
                full_dossier = json.loads(
                    dossier_store.resolve(prepared["pinned"]).content
                )
                shard_dossiers = [(shard, build_shard_dossier(full_dossier, shard)) for shard in shards]
                for shard, shard_dossier in shard_dossiers:
                    if not shard_dossier["source_complete"]:
                        return {
                            "delegated": False,
                            "requires_semantic_partition": True,
                            "reason": (
                                "At least one source file exceeds the bounded snapshot; "
                                "split that file semantically before delegation."
                            ),
                            **prepared,
                        }
                # Validate all bounded sources before the first worker starts.
                for shard, shard_dossier in shard_dossiers:
                    shard_refs = dossier_store.put(
                        f"code-dossiers/{prepared['digest']}/{shard['shard_id']}",
                        shard_dossier,
                        metadata={
                            "authority": "deterministic_host_snapshot",
                            "semantic_conclusion": False,
                            "parent_dossier": prepared["pinned"],
                        },
                    )
                    description = (
                        str(shard["task_description"])
                        + "\n\nOpen this pinned shard handoff first with context_open "
                        + f"(raise max_chars if it continues): {shard_refs['pinned']}"
                        + "\nIt contains numbered source and dependency facts for the "
                        + "assigned files. Do not reread or enumerate the workspace. "
                        + "Stop gathering once the acceptance criteria can be answered."
                    )
                    child = _spawn_child(
                        task_description=description,
                        profile_id=profile_id,
                        task_kind=task_kind,
                        max_steps=int(args.get("max_steps", 35) or 35),
                        target_files=list(shard["target_files"]),
                        acceptance_criteria=criteria,
                        allowed_tools=[
                            "context_open", "context_search", "artifact_read",
                            "calculate", "decision_evidence",
                        ],
                        required_for_parent_completion=bool(
                            args.get("required_for_parent_completion", True)
                        ),
                    )
                    children.append({
                        "shard_id": shard["shard_id"],
                        "process_id": child.process_id,
                        "target_files": shard["target_files"],
                        "estimated_tokens": shard["estimated_tokens"],
                        "handoff_ref": shard_refs["pinned"],
                    })
                if on_stream_event is not None:
                    on_stream_event({
                        "event": "task_bundle_delegated",
                        "process_id": process.process_id,
                        "dossier_ref": prepared["pinned"],
                        "shard_count": len(children),
                        "children": children,
                    })
                return {
                    "delegated": True,
                    "dossier_ref": prepared["pinned"],
                    "children": children,
                    "recommended_parallelism": plan["recommended_parallelism"],
                }
            except (ValueError, OSError, RuntimeError, KeyError) as exc:
                from backend.core.errors import error_payload
                cancelled = []
                cancellation_errors = {}
                for item in children:
                    child_id = item["process_id"]
                    try:
                        manager.kill(child_id, reason="bundle_admission_failed")
                        cancelled.append(child_id)
                    except Exception as cancel_exc:
                        cancellation_errors[child_id] = str(cancel_exc)
                    contract = process.delegated_contracts.get(child_id)
                    if contract is not None:
                        contract["required_for_parent_completion"] = False
                        contract["admission_rolled_back"] = True
                return {"delegated": False, "children": children,
                        **error_payload("DELEGATION_ADMISSION_FAILED", message=str(exc),
                                        state_changed=bool(children), details={
                                            "partial_admission": bool(children),
                                            "cancel_requested_process_ids": cancelled,
                                            "cancellation_errors": cancellation_errors,
                                            "side_effects_rolled_back": False,
                                        })}

        tools["delegate_task_bundle"] = AgentTool(
            execution_contract=data_broker("host.runtime.delegate_task_bundle"),
            name="delegate_task_bundle",
            description=(
                "Build one deterministic dossier, shard a large task, and start all "
                "bounded B contracts in one call. Host handles enumeration, copying, "
                "budget checks and dispatch; A remains responsible for semantic review."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "target_files": {"type": "array", "items": {"type": "string"}},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "capability_profile_id": {
                        "type": "string",
                        "enum": list(CapabilityProfiles.ids(actor_kind="worker")),
                        "default": "governance.observe",
                    },
                    "task_kind": {
                        "type": "string",
                        "enum": ["answer", "plan", "action"],
                        "default": "answer",
                    },
                    "max_files": {"type": "integer"},
                    "max_tokens_per_shard": {"type": "integer"},
                    "max_files_per_shard": {"type": "integer"},
                    "max_shards": {"type": "integer"},
                    "max_steps": {"type": "integer"},
                    "required_for_parent_completion": {"type": "boolean"},
                },
                "required": ["goal", "target_files", "acceptance_criteria"],
            },
            execute=delegate_task_bundle,
            effect=ToolEffect.READ,
            idempotent=False,
        )

        def list_agents(_args: dict) -> dict:
            children = [
                child for child_id in process.coordination_snapshot()[0]
                if manager is not None and (child := manager.get(child_id)) is not None
            ]
            return {
                "agents": [
                    {
                        "process_id": child.process_id,
                        "session_id": child.session.session_id,
                        "display_name": child.session.display_name,
                        **(manager.presentation.read(child.process_id) if getattr(manager, "presentation", None) else {}),
                        "actor_kind": child.actor_kind,
                        "status": child.status.value,
                        "task": child.task_description[:300],
                        "target_files": list(process.delegated_contracts.get(child.process_id, {}).get("target_files", [])),
                        "superseded_by": process.delegated_contracts.get(child.process_id, {}).get("superseded_by", ""),
                        "review_target_id": child.review_target_id,
                        "depends_on": list(child.depends_on),
                        "worktree": dict(child.worktree or {}),
                        "error": (child.result or {}).get("error", ""),
                    }
                    for child in children
                ]
            }

        tools["list_agents"] = AgentTool(
            execution_contract=data_broker("host.runtime.list_agents"),
            name="list_agents",
            description="List this supervisor's B Agents and their durable execution states.",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=list_agents, read_only=True, effect=ToolEffect.READ, idempotent=True,
        )

        def wait_agents(args: dict) -> dict:
            ids = [str(item) for item in (args.get("process_ids", []) or process.child_ids)]
            timeout = max(0.0, min(float(args.get("timeout", 30) or 30), 300.0))
            return_when = str(args.get("return_when", "all_terminal"))
            owned_ids = []
            results = {}
            for child_id in ids:
                if _owned_child(child_id) is None:
                    results[child_id] = {
                        "status": "not_found_or_not_owned", "result": None,
                    }
                else:
                    owned_ids.append(child_id)
            waited = manager.wait_many(
                owned_ids,
                timeout=timeout,
                cancel_event=process.cancellation_event,
                return_when=return_when,
            ) if manager else {
                "processes": {}, "all_terminal": False,
                "state_version": 0, "state_changed": False,
            }
            for child_id, state in waited["processes"].items():
                child = _owned_child(child_id)
                outcome = (
                    dict(child.result) if child is not None
                    and isinstance(child.result, dict) else {}
                )
                outcome_ref = ""
                if outcome:
                    refs = dossier_store.put(
                        f"agent-outcomes/{process.active_task_id or process.process_id}/{child_id}",
                        outcome,
                        metadata={"authority": "host_runtime_outcome"},
                    )
                    outcome_ref = refs["pinned"]
                response = str(outcome.get("response", ""))
                metadata = dict(outcome.get("metadata") or {})
                results[child_id] = {
                    "status": state.get("status", "not_found"),
                    "outcome_ref": outcome_ref,
                    "response_excerpt": response[:1800],
                    "response_truncated": len(response) > 1800,
                    "error": outcome.get("error"),
                    "steps_used": outcome.get("steps_used", 0),
                    "tool_calls_executed": outcome.get("tool_calls_executed", 0),
                    "cache_summary": metadata.get("cache_summary"),
                }
            return {
                "agents": results,
                "all_terminal": waited["all_terminal"],
                "state_version": waited["state_version"],
                "state_changed": waited["state_changed"],
                "wait_mode": return_when,
            }

        tools["wait_agents"] = AgentTool(
            execution_contract=data_broker("host.runtime.wait_agents"),
            name="wait_agents",
            description="Wait for selected B Agents with a bounded, cancellation-aware timeout.",
            parameters={
                "type": "object",
                "properties": {
                    "process_ids": {"type": "array", "items": {"type": "string"}},
                    "timeout": {"type": "number"},
                    "return_when": {
                        "type": "string",
                        "enum": ["all_terminal", "any_terminal"],
                    },
                },
                "required": [],
            },
            execute=wait_agents, read_only=True, effect=ToolEffect.READ,
            prepare_args=lambda raw: {
                **dict(raw or {}),
                **({"timeout": float(raw["timeout"])}
                   if isinstance((raw or {}).get("timeout"), str)
                   and str((raw or {}).get("timeout", "")).strip().replace(".", "", 1).isdigit()
                   else {}),
            },
        )

        def review_child_outcome(args: dict) -> dict:
            child_id = str(args.get("process_id", ""))
            child = _owned_child(child_id)
            if child is None:
                return {"accepted": False, "error": "process is not an owned child"}
            if child.status in {
                ProcessStatus.RUNNING, ProcessStatus.WAITING,
                ProcessStatus.CANCELLING, ProcessStatus.AWAITING_USER,
            }:
                return {"accepted": False, "error": "child outcome is not terminal"}
            raw_verdict = str(args.get("verdict", "")).strip().casefold()
            verdict_key = raw_verdict.replace("-", "_").replace(" ", "_")
            verdict = {
                "approve": "approved",
                "approved": "approved",
                "changes_required": "changes_required",
                "change_required": "changes_required",
                "request_changes": "changes_required",
                "reject": "changes_required",
                "rejected": "changes_required",
            }.get(verdict_key, "")
            summary = str(args.get("summary", "")).strip()
            if not verdict:
                return {
                    "accepted": False,
                    "error": "verdict must be approved or changes_required",
                }
            if not summary:
                return {"accepted": False, "error": "non-empty summary is required"}
            if verdict == "approved" and child.status != ProcessStatus.COMPLETED:
                return {
                    "accepted": False,
                    "error": "a failed or cancelled child cannot be approved",
                }
            known_receipts = {
                str(item.get("receipt_id", "")) for item in child.tool_receipts
                if item.get("receipt_id")
            }
            committed_action_receipts = {
                str(item.get("receipt_id", "")) for item in child.tool_receipts
                if item.get("receipt_id")
                and item.get("succeeded") is True
                and item.get("committed") is True
                and is_effectful_mutation(str(item.get("effect", "read")))
            }
            # Receipt selection is mechanical host work.  The reviewer judges
            # semantics; it should not spend provider turns copying opaque IDs
            # that the process tree already owns.  Keep explicit IDs accepted
            # for backwards compatibility, but auto-link every committed action
            # receipt when the field is omitted.
            explicit_receipts = args.get("receipt_ids") if "receipt_ids" in args else None
            receipt_ids = (
                [str(item) for item in (explicit_receipts or []) if str(item)]
                if explicit_receipts is not None
                else (
                    sorted(committed_action_receipts)
                    if verdict == "approved" and child.task_kind == "action"
                    else []
                )
            )
            unknown = [item for item in receipt_ids if item not in known_receipts]
            if unknown:
                return {
                    "accepted": False,
                    "error": "unknown child receipts: " + ", ".join(unknown),
                }
            if verdict == "approved" and child.task_kind == "action" and not receipt_ids:
                return {
                    "accepted": False,
                    "error": "approving an action child requires cited receipt_ids",
                }
            if (
                verdict == "approved"
                and child.task_kind == "action"
                and not committed_action_receipts.intersection(receipt_ids)
            ):
                return {
                    "accepted": False,
                    "error": (
                        "approving an action child requires at least one cited "
                        "successful committed action receipt"
                    ),
                }
            review = {
                "verdict": verdict,
                "summary": summary,
                "receipt_ids": receipt_ids,
                "receipt_selection": (
                    "explicit" if explicit_receipts is not None else "host_auto"
                ),
                "required_test_ids": list(child.required_test_ids),
                "reviewed_at_step": process.steps_used,
            }
            if (
                verdict == "approved"
                and child.worktree.get("state") == "sealed"
                and not child.worktree.get("own_commit")
                and manager is not None
            ):
                try:
                    manager.dispose_process_worktree(child, keep_ref=True)
                except Exception as exc:
                    return {
                        "accepted": False,
                        "error": "approved no-change worktree cleanup failed: " + str(exc),
                    }
                review["worktree_cleanup"] = "disposed_no_changes"
                if on_stream_event is not None:
                    on_stream_event({
                        "event": "worktree_disposed",
                        "process_id": child.process_id,
                        "reason": "approved_no_changes",
                        "worktree": dict(child.worktree),
                    })
            process.child_reviews[child_id] = review
            return {"accepted": True, "process_id": child_id, "review": review}

        tools["review_child_outcome"] = AgentTool(
            execution_contract=data_broker("host.runtime.review_child_outcome"),
            name="review_child_outcome",
            description=(
                "Record A's structured review of a terminal owned B outcome. "
                "Approval is required before any required delegated outcome can satisfy "
                "the supervisor completion gate. The Host automatically links the "
                "child's qualifying receipts; do not copy internal receipt IDs."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "process_id": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["approved", "changes_required"],
                    },
                    "summary": {"type": "string"},
                },
                "required": ["process_id", "verdict", "summary"],
            },
            execute=review_child_outcome, read_only=True, effect=ToolEffect.READ,
        )

        def promote_agent_changes(args: dict) -> dict:
            if manager is None:
                return {"promoted": False, "error": "AgentProcessManager is unavailable"}
            process_ids = [
                str(item) for item in (args.get("process_ids") or []) if str(item)
            ]
            if not process_ids:
                return {"promoted": False, "error": "process_ids is required"}
            try:
                result = manager.promote_process_results(process, process_ids)
                if on_stream_event is not None:
                    on_stream_event({
                        "event": "worktree_promoted",
                        "process_id": process.process_id,
                        "task_id": process.active_task_id,
                        "promotion": result,
                    })
                return result
            except (ValueError, RuntimeError) as exc:
                return {"promoted": False, "error": str(exc)}

        tools["promote_agent_changes"] = AgentTool(
            execution_contract=data_broker("host.runtime.promote_agent_changes"),
            name="promote_agent_changes",
            description=(
                "Promote an A-approved, sealed DAG result into the user working "
                "repository. The Host includes transitive predecessors, checks the "
                "task snapshot and blocks overlapping workspace drift."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "process_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["process_ids"],
            },
            execute=promote_agent_changes,
            read_only=False,
            effect=ToolEffect.WORKSPACE_WRITE,
            resources=["filesystem:*"],
            idempotent=False,
        )

        def send_feedback(args: dict) -> dict:
            if manager is None:
                return {"error": "AgentProcessManager is unavailable"}
            child_id = str(args.get("process_id", ""))
            if _owned_child(child_id) is None:
                return {"accepted": False, "error": "process is not an owned child"}
            child = _owned_child(child_id)
            if child.status in {ProcessStatus.COMPLETED, ProcessStatus.FAILED,
                                ProcessStatus.TIMED_OUT, ProcessStatus.CANCELLED}:
                from backend.core.errors import error_payload
                return {"accepted": False, **error_payload(
                    "DELEGATION_ADMISSION_FAILED",
                    message="This execution has ended. Continue its durable session with delegate_task; a closed mailbox cannot restart execution.",
                    next_actions=[{"action": "delegate_task", "patch": {"continue_process_id": child_id},
                                   "reason": "Keep the original agent context and provide the updated task contract."}],
                )}
            return manager.send_instruction(
                child_id, str(args.get("message", "")),
            )

        tools["send_feedback"] = AgentTool(
            execution_contract=data_broker("host.runtime.send_feedback"),
            name="send_feedback",
            description=(
                "Send corrective or additional instructions to a B Agent mailbox. "
                "Delivery is applied at the next safe turn boundary."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "process_id": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["process_id", "message"],
            },
            execute=send_feedback, read_only=True, effect=ToolEffect.READ,
        )

        def cancel_agent(args: dict) -> dict:
            child_id = str(args.get("process_id", ""))
            if manager and _owned_child(child_id) is None:
                return {"requested": False, "status": "not_owned"}
            return manager.kill(child_id) if manager else {
                "requested": False, "status": "manager_unavailable",
            }

        tools["cancel_agent"] = AgentTool(
            execution_contract=data_broker("host.runtime.cancel_agent"),
            name="cancel_agent",
            description="Request cancellation of one delegated B Agent without faking a terminal state.",
            parameters={
                "type": "object",
                "properties": {"process_id": {"type": "string"}},
                "required": ["process_id"],
            },
            execute=cancel_agent, read_only=True, effect=ToolEffect.READ,
        )

        def request_review(args: dict) -> dict:
            if manager is None:
                return {"error": "AgentProcessManager is unavailable"}
            target_id = str(args.get("process_id", ""))
            target = manager.get(target_id)
            if target is None or (target is not process and _owned_child(target_id) is None):
                return {"error": "review target not found or not owned"}
            if target is not process and target.status in {
                ProcessStatus.RUNNING, ProcessStatus.WAITING,
                ProcessStatus.CANCELLING, ProcessStatus.AWAITING_USER,
            }:
                return {"error": "review target is not terminal"}
            if target is process:
                from backend.core.loop.verification_policy import verification_plan
                plan = verification_plan(process)
                if not plan.requires_independent_review:
                    return {
                        "accepted": False,
                        "code": "REVIEW_NOT_REQUIRED",
                        "detail": (
                            "The Host selected verification level "
                            f"{plan.level}; use existing receipts/tests and submit "
                            "complete_task instead of creating a Reviewer B."
                        ),
                        "verification_plan": plan.to_dict(),
                        "next_action": "complete_task",
                    }
            evidence = {
                "task": target.task_description,
                "status": target.status.value,
                "result": target.result,
                "tool_receipts": target.tool_receipts,
                "completion_claim": (
                    target.completion_claim.to_dict()
                    if getattr(target.completion_claim, "to_dict", None) else None
                ),
            }
            review_files = []
            if getattr(target.completion_claim, "files", None):
                review_files.extend(str(item) for item in target.completion_claim.files)
            instruction = (
                "Independently review the following task result. Inspect the workspace, "
                "run relevant registered tests when useful, and call complete_review with "
                "verdict approved or changes_required. Do not modify product files.\n\n"
                f"Review focus: {str(args.get('focus', '')).strip()}\n"
                f"Evidence bundle:\n{json.dumps(evidence, ensure_ascii=False, default=str)[:24000]}"
            )
            child = _spawn_child(
                task_description=instruction,
                profile_id="review.independent",
                task_kind="review",
                max_steps=int(args.get("max_steps", 40) or 40),
                target_files=review_files,
                acceptance_criteria=["Issue a structured complete_review verdict"],
                required_for_parent_completion=False,
                actor_kind="reviewer",
                review_target_id=target_id,
            )
            return {"reviewer_process_id": child.process_id, "target_process_id": target_id}

        tools["request_review"] = AgentTool(
            execution_contract=data_broker("host.runtime.request_review"),
            name="request_review",
            description=(
                "Start an independent read-only Reviewer B for a completed B result or "
                "for this supervisor's Level-2 self-executed work. For bounded self-work, "
                "submit complete_task first: the Host will request an independent review "
                "only when the verification tier requires one. Hidden reasoning is not shared."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "process_id": {"type": "string"},
                    "focus": {"type": "string"},
                    "max_steps": {"type": "integer"},
                },
                "required": ["process_id", "focus"],
            },
            execute=request_review, read_only=True, effect=ToolEffect.READ,
        )

    if process.tool_registry is not None and process.tool_registry.has("define_tool"):
        def define_tool(args: dict) -> dict:
            from backend.core.tools.dynamic_tools import validate_definition

            operation = str(args.get("operation", "define") or "define").strip().lower()
            if operation == "list":
                return {
                    "operation": "list",
                    "tools": [
                        {
                            "name": item.name,
                            "effect": item.effect.value,
                            "version": (item.composite_spec or {}).get("version", 1),
                            "digest": (item.composite_spec or {}).get("digest", ""),
                            "steps": [
                                step.get("id")
                                for step in (item.composite_spec or {}).get("steps", [])
                            ],
                        }
                        for item in process.dynamic_tools.values()
                    ],
                }
            if operation == "revoke":
                name = str(args.get("name", "")).strip()
                if name not in process.dynamic_tools:
                    return {"revoked": False, "error": "dynamic tool not found", "name": name}
                previous = process.dynamic_tools.pop(name)
                process.tool_registry.remove_scoped(name)
                return {
                    "revoked": True,
                    "name": name,
                    "version": (previous.composite_spec or {}).get("version", 1),
                    "digest": (previous.composite_spec or {}).get("digest", ""),
                }
            if operation not in {"define", "replace"}:
                return {"defined": False, "error": "operation must be define, replace, list, or revoke"}

            authorized_base = {
                name: tool for name, tool in available_tools.items()
                if process.tool_registry is not None and process.tool_registry.has(name)
            }
            authorized_base.update(process.dynamic_tools)
            name = str(args.get("name", "")).strip()
            existing = process.dynamic_tools.get(name)
            if operation == "replace":
                if existing is None:
                    return {"defined": False, "error": "only a dynamic tool can be replaced"}
                authorized_base.pop(name, None)
            elif name in process.dynamic_tools:
                return {"defined": False, "error": "dynamic tool already exists; use operation=replace"}
            compiled_input = dict(args)
            compiled_input["_version"] = (
                int((existing.composite_spec or {}).get("version", 1)) + 1
                if existing is not None else 1
            )
            try:
                spec = validate_definition(compiled_input, authorized_base)
            except (ValueError, PermissionError) as exc:
                return {"error": str(exc), "defined": False}

            dynamic = AgentTool(
                execution_contract=data_broker("host.composite"),
                name=spec["name"],
                description=spec["description"],
                parameters=spec["parameters"],
                execute=lambda _args: {"error": "DYNAMIC_TOOL_REQUIRES_PIPELINE"},
                read_only=spec["effect"] == "read",
                resources=spec["resources"],
                timeout=spec["timeout"],
                effect=spec["effect"],
                cancellation=CancellationMode.COOPERATIVE,
                composite_spec=spec,
            )
            process.dynamic_tools[dynamic.name] = dynamic
            process.tool_registry.add_scoped(dynamic.name)
            return {
                "defined": True,
                "name": dynamic.name,
                "effect": spec["effect"],
                "resources": spec["resources"],
                "version": spec["version"],
                "digest": spec["digest"],
                "execution_mode": spec["execution_mode"],
                "steps": [item["id"] for item in spec["steps"]],
            }

        tools["define_tool"] = AgentTool(
            execution_contract=data_broker("host.runtime.define_tool"),
            name="define_tool",
            description=(
                "Define one task-scoped composite tool from tools already authorized in "
                "this capability. It cannot add authority or execute arbitrary Python."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["define", "replace", "list", "revoke"],
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "parameters": {"type": "object"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "tool": {"type": "string"},
                                "arguments": {"type": "object"},
                            },
                            "required": ["tool", "arguments"],
                        },
                    },
                },
                "required": [],
            },
            execute=define_tool,
            read_only=False,
            effect=ToolEffect.PROCESS,
        )

    if process.tool_registry is not None and process.tool_registry.has("author_tool"):
        def author_tool(args: dict) -> dict:
            from backend.core.errors import error_payload
            from backend.core.tools.dynamic_tools import (
                validate_authored_definition, validate_privileged_authored_definition,
            )
            from backend.core.storage import get_storage
            operation = str(args.get("operation", "register") or "register").lower()
            storage = getattr(getattr(process, "_manager", None), "storage", None)
            if storage is None:
                storage = get_storage(workspace_path)

            def authored_error(name: str, *, message: str = "", details: dict | None = None,
                               next_actions: list[dict] | None = None, **state) -> dict:
                return {
                    **error_payload(
                        name, message=message, details=details,
                        next_actions=next_actions,
                    ),
                    **state,
                }

            def stored_error(exc: BaseException, **state) -> dict:
                raw = str(exc).strip("'\"")
                symbolic = raw.split(":", 1)[0]
                name = symbolic if symbolic in {
                    "CUSTOM_TOOL_NOT_FOUND", "CUSTOM_TOOL_ARCHIVED",
                    "CUSTOM_TOOL_PRIVACY_BLOCKED",
                    "CUSTOM_TOOL_SOURCE_INTEGRITY_FAILED",
                } else "CUSTOM_TOOL_INVALID"
                actions = {
                    "CUSTOM_TOOL_NOT_FOUND": [{"action": "list_saved_tools"}],
                    "CUSTOM_TOOL_ARCHIVED": [{"action": "restore_then_mount"}],
                    "CUSTOM_TOOL_PRIVACY_BLOCKED": [{"action": "remove_sensitive_content"}],
                    "CUSTOM_TOOL_SOURCE_INTEGRITY_FAILED": [{"action": "inspect_storage"}],
                }.get(name, [{"action": "revise_definition_then_retry"}])
                return authored_error(name, message=raw, next_actions=actions, **state)

            def mount(saved: dict) -> dict:
                spec = dict(saved.get("spec") or {})
                spec["source_ref"] = str(saved.get("source_ref") or "")
                name = str(spec.get("name") or saved.get("name") or "")
                privileged = spec.get("execution_mode") == "authored_privileged_python"
                effect = ToolEffect(str(spec.get("effect") or ("process" if privileged else "read")))
                dynamic = AgentTool(
                    execution_contract=NATIVE_PROCESS,
                    name=name, description=str(spec.get("description") or ""),
                    parameters=spec.get("parameters"),
                    execute=lambda _args: {"error": "AUTHORED_TOOL_REQUIRES_PIPELINE"},
                    read_only=effect == ToolEffect.READ, effect=effect,
                    resources=list(spec.get("resources") or []),
                    timeout=float(spec.get("timeout", 30) or 30),
                    cancellation=CancellationMode.ISOLATED_PROCESS,
                    approval=ApprovalMode.ASK if privileged else ApprovalMode.ALLOW,
                    # Privileged authored code is an authority-expanding edge:
                    # each concrete argument set needs its own exact grant.
                    approval_per_invocation=privileged,
                    composite_spec=spec,
                )
                process.dynamic_tools[name] = dynamic
                process.tool_registry.add_scoped(name)
                return {
                    "mounted": True, "name": name,
                    "version": int(saved.get("version") or spec.get("version") or 1),
                    "digest": str(saved.get("digest") or spec.get("digest") or ""),
                    "scope": "current_task",
                    "authority": "user_approved_privileged" if privileged else "pure_transform_only",
                }

            if operation == "list":
                catalog = storage.list_custom_tools(
                    include_archived=bool(args.get("include_archived", False)),
                )
                mounted = {
                    name for name, tool in process.dynamic_tools.items()
                    if (tool.composite_spec or {}).get("execution_mode") in {
                        "authored_pure_python", "authored_privileged_python",
                    }
                }
                for item in catalog.get("tools", []):
                    item["mounted"] = item.get("name") in mounted
                return catalog
            if operation in {"revoke", "detach"}:
                name = str(args.get("name") or "")
                tool = process.dynamic_tools.get(name)
                if tool is None or (tool.composite_spec or {}).get("execution_mode") not in {
                    "authored_pure_python", "authored_privileged_python",
                }:
                    return authored_error(
                        "CUSTOM_TOOL_NOT_FOUND",
                        message="This custom tool is not mounted in the current task.",
                        next_actions=[{"action": "list_mounted_tools"},
                                      {"action": "use_saved_tool"}],
                        revoked=False,
                    )
                process.dynamic_tools.pop(name, None)
                process.tool_registry.remove_scoped(name)
                return {"revoked": True, "detached": True, "name": name,
                        "saved_asset_retained": True}
            if operation == "use":
                try:
                    saved = storage.load_custom_tool(
                        str(args.get("name") or ""),
                        version=(int(args["version"]) if args.get("version") else None),
                    )
                    return mount(saved)
                except (KeyError, ValueError, RuntimeError, OSError) as exc:
                    return stored_error(exc, mounted=False)
            if operation in {"delete", "archive", "restore"}:
                name = str(args.get("name") or "")
                try:
                    changed = storage.set_custom_tool_archived(
                        name, archived=operation != "restore",
                    )
                except (KeyError, ValueError, RuntimeError) as exc:
                    return stored_error(exc, changed=False)
                if operation != "restore":
                    process.dynamic_tools.pop(name, None)
                    process.tool_registry.remove_scoped(name)
                return {"changed": True, **changed,
                        "versions_retained": True}
            if operation not in {"register", "replace"}:
                return authored_error(
                    "CUSTOM_TOOL_INVALID",
                    message=("operation must be register, replace, use, list, detach, "
                             "delete, archive, or restore"),
                    next_actions=[{"action": "retry", "valid_operations": [
                        "register", "replace", "use", "list", "detach",
                        "delete", "archive", "restore",
                    ]}],
                )
            name = str(args.get("name") or "")
            catalog = storage.list_custom_tools(include_archived=True)
            persistent = next(
                (item for item in catalog.get("tools", []) if item.get("name") == name),
                None,
            )
            if operation == "register" and persistent is not None:
                return authored_error(
                    "CUSTOM_TOOL_EXISTS", next_actions=[{"action": "replace"}],
                    registered=False,
                )
            if operation == "replace" and persistent is None:
                return authored_error(
                    "CUSTOM_TOOL_NOT_FOUND", next_actions=[{"action": "register"}],
                    registered=False,
                )
            compiled = dict(args)
            compiled["_version"] = int((persistent or {}).get("version", 0) or 0) + 1
            try:
                privileged = str(args.get("authority_mode") or "pure") == "privileged"
                spec = (
                    validate_privileged_authored_definition(compiled, workspace_path)
                    if privileged else validate_authored_definition(compiled, workspace_path)
                )
            except (ValueError, PermissionError, OSError) as exc:
                return authored_error(
                    "CUSTOM_TOOL_INVALID", message=str(exc),
                    next_actions=[{"action": "revise_definition_then_retry"}],
                    registered=False,
                )
            if privileged:
                from backend.core.loop.permission_broker import consume_exact_grant
                grant = consume_exact_grant(process, "author_tool", args)
                if grant is None:
                    return authored_error(
                        "CUSTOM_TOOL_PRIVILEGED_APPROVAL_REQUIRED",
                        message=(
                            "Privileged source can access the process, filesystem, imports or network. "
                            "Ask the user to approve this exact author_tool registration before running tests."
                        ),
                        details={
                            "name": name, "source_sha256": spec["source_sha256"],
                            "effect": spec["effect"], "resources": spec["resources"],
                        },
                        next_actions=[{
                            "action": "request_permission", "tool_name": "author_tool",
                            "arguments": {
                                key: value for key, value in args.items()
                                if not str(key).startswith("_")
                            },
                            "resource": "capability://author_tool",
                            "purpose": str(spec.get("purpose") or "register and test this privileged custom tool"),
                        }],
                        registered=False,
                    )
            # Registration tests run in the cancellable subprocess boundary;
            # model-authored code never executes in the daemon process.
            from backend.core.loop.process_tool_runner import ProcessToolRunner
            for index, test in enumerate(spec["tests"], 1):
                checked = ProcessToolRunner(timeout=spec["timeout"]).run(
                    "authored_privileged_python" if privileged else "authored_python",
                    {**dict(test["input"]), "_workspace": workspace_path,
                     "_dynamic_spec": spec},
                    cancellation_event=process.cancellation_event,
                )
                if not checked.success:
                    return authored_error(
                        "CUSTOM_TOOL_INVALID",
                        message="The authored tool registration test crashed or timed out.",
                        details={"test": index, "diagnostic": checked.error},
                        next_actions=[{"action": "revise_source_then_retry"}],
                        test=index, registered=False,
                    )
                actual = checked.data or {}
                if actual != test["expected"]:
                    return authored_error(
                        "CUSTOM_TOOL_INVALID",
                        message="The authored tool registration test returned an unexpected result.",
                        details={"test": index, "expected": test["expected"],
                                 "actual": actual},
                        next_actions=[{"action": "revise_source_or_expected_result"}],
                        test=index, expected=test["expected"], actual=actual,
                        registered=False,
                    )
            source = str(spec.pop("_source"))
            try:
                saved = storage.save_custom_tool(
                    spec, source, replace=operation == "replace",
                )
                loaded = storage.load_custom_tool(name)
            except Exception as exc:
                return stored_error(exc, registered=False, saved=False)
            mounted = mount(loaded)
            return {"registered": True, **saved, **mounted,
                    "tests_passed": len(spec["tests"])}

        tools["author_tool"] = AgentTool(
            execution_contract=data_broker("host.runtime.author_tool"),
            name="author_tool",
            description=(
                "Manage project-saved Python shortcuts. Pure mode is statically confined. "
                "Privileged mode may use imports, files, processes or network, but binds the "
                "source digest and registration contract to explicit user approval, and every "
                "later invocation requires a new exact approval. register/replace creates an "
                "immutable tested version; use mounts it for this task; detach keeps the asset; "
                "delete archives it; restore reactivates it."
            ),
            parameters={"type": "object", "properties": {
                "operation": {"type": "string", "enum": [
                    "register", "replace", "use", "list", "detach", "revoke",
                    "delete", "archive", "restore",
                ]},
                "name": {"type": "string"}, "description": {"type": "string"},
                "parameters": {"type": "object"}, "source_path": {"type": "string"},
                "source_sha256": {"type": "string"},
                "authority_mode": {"type": "string", "enum": ["pure", "privileged"]},
                "purpose": {"type": "string"},
                "effect": {"type": "string", "enum": ["workspace_write", "process", "external"]},
                "resources": {"type": "array", "items": {"type": "string"}},
                "tests": {"type": "array", "items": {"type": "object"}},
                "timeout": {"type": "number"}, "version": {"type": "integer"},
                "include_archived": {"type": "boolean"},
            }, "required": []},
            execute=author_tool, read_only=False, effect=ToolEffect.PROCESS,
            approval_per_invocation=True,
        )

    if process.actor_kind == "reviewer":
        def complete_review(args: dict) -> dict:
            verdict = str(args.get("verdict", "")).strip()
            summary = str(args.get("summary", "")).strip()
            if verdict not in {"approved", "changes_required"}:
                return {"error": "verdict must be approved or changes_required"}
            if not summary:
                return {"error": "review summary is required"}
            process.review_claim = {
                "verdict": verdict,
                "summary": summary,
                "findings": list(args.get("findings", []) or []),
                "evidence": list(args.get("evidence", []) or []),
                "target_process_id": process.review_target_id,
            }
            return {"accepted": True, **process.review_claim}

        tools["complete_review"] = AgentTool(
            execution_contract=data_broker("host.runtime.complete_review"),
            name="complete_review",
            description="Submit the independent review verdict and evidence to the host gate.",
            parameters={
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": ["approved", "changes_required"]},
                    "summary": {"type": "string"},
                    "findings": {"type": "array", "items": {"type": "object"}},
                    "evidence": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["verdict", "summary", "findings", "evidence"],
            },
            execute=complete_review,
            read_only=True,
            effect=ToolEffect.READ,
        )

    def complete_task(args: dict) -> dict:
        try:
            claim = CompletionClaim.from_args(args, step=process.steps_used)
        except ValueError as exc:
            return {"error": str(exc), "accepted": False}
        process.completion_claim = claim
        return {"accepted": True, "claim": claim.to_dict()}

    tools["complete_task"] = AgentTool(
        execution_contract=data_broker("host.runtime.complete_task"),
        name="complete_task",
        description=(
            "Submit the action result and verification evidence for deterministic "
            "host evaluation. This does not self-approve completion."
        ),
        parameters=COMPLETE_TASK_PARAMETERS,
        execute=complete_task,
        read_only=True,
        effect=ToolEffect.READ,
        idempotent=False,
    )
    return tools


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


def _to_provider_tool_calls(tool_calls: list[dict]) -> list[dict]:
    result = []
    for index, call in enumerate(tool_calls):
        result.append({
            "id": str(call.get("id") or f"call_{index}"),
            "type": "function",
            "function": {
                "name": str(call.get("name", "")),
                "arguments": json.dumps(
                    call.get("args", {}), ensure_ascii=False, separators=(",", ":"),
                ),
            },
        })
    return result


def _latest_error_code(process: AgentProcess, tool_name: str) -> str:
    for receipt in reversed(getattr(process, "tool_receipts", [])):
        if receipt.get("tool_name") == tool_name and not receipt.get("succeeded", False):
            return str(receipt.get("error_code", ""))
    return ""


def _host_tool_error_result(
    *,
    tool_name: str,
    execution_id: str,
    call_index: int,
    error_name: str,
    details: dict | None = None,
    next_actions: list[dict] | None = None,
):
    """Build a pre-execution failure through the canonical tool result path."""
    from backend.core.errors import error_payload
    from backend.core.loop.tool_pipeline import ToolPipeline

    payload = error_payload(
        error_name,
        details=details,
        next_actions=next_actions,
    )
    return ToolPipeline().error_result(
        tool_name, execution_id, call_index, payload["error"],
        diagnostics={
            "nature": "governance",
            "code": payload["error"],
            "source": "host",
            "execution_state": str(
                (details or {}).get("execution_state") or "not_started"
            ),
            "error_info": payload["error_info"],
        },
    )

def _select_authorized_tools(
    process: AgentProcess,
    dispatcher_tools: dict[str, AgentTool],
) -> tuple[dict[str, AgentTool], list[str]]:
    """Resolve one capability set for every model/execution surface.

    Every actor, including Ring 0, is restricted by the server-issued profile.
    Host protocol tools are added according to actor/task semantics.
    """
    if process.tool_registry is None:
        return {}, ["<missing ToolRegistry>"]

    declared = process.tool_registry.list_all()
    if (
        process.actor_kind == "supervisor"
        and process.capability_lease is None
        and _supervisor_is_coordination_only(process)
    ):
        # Enforce coordination-only mode from the *current task contract*.
        # Historical B contracts remain available for audit/owner routing, but
        # must never remove ordinary tools from a later unrelated user turn.
        # Explicit self-execution remains available via a scoped lease.
        delegated_mode = {
            "context_open", "context_search", "artifact_read", "read_child_artifact", "calculate",
            "decision_evidence", "declare_task_contract", "prepare_task_bundle", "delegate_task",
            "delegate_task_dag", "delegate_task_bundle", "list_agents", "wait_agents",
            "send_feedback", "cancel_agent", "request_review",
            "review_child_outcome", "request_user_decision", "request_permission",
            "acknowledge_governance_signal",
            "define_tool", "author_tool", "engineering_workflow",
            "promote_agent_changes", "complete_supervision",
        }
        declared = [name for name in declared if name in delegated_mode]
    unavailable = sorted(name for name in declared if name not in dispatcher_tools)
    authorized = {
        name: dispatcher_tools[name]
        for name in declared
        if name in dispatcher_tools
    }
    # The model has already supplied the semantic routing facts.  Once the
    # compiled contract selects bounded single-owner execution, omit the three
    # delegation entry points from the current provider surface.  A may revise
    # the contract if the work genuinely grows; the next surface rebuild then
    # exposes delegation again.  This avoids a reject/retry turn without using
    # keyword intent rules or granting execution authority implicitly.
    if process.actor_kind == "supervisor":
        from backend.core.loop.task_contract import get_task_contract
        contract = get_task_contract(process)
        host_requirements = dict(contract.get("host_requirements") or {})
        execution_mode = str(
            contract.get("execution_mode") or ""
        ).strip().lower()
        contract_declared = bool(
            contract.get("revision")
            or execution_mode in {"answer", "self_execute", "delegate", "either"}
        )
        active_supervision_contract = any(
            bool(dict(item or {}).get("required_for_parent_completion", True))
            and not str(dict(item or {}).get("superseded_by") or "")
            for item in process.delegated_contracts.values()
        )
        restored_delegate_contract = bool(
            execution_mode in {"delegate", "either"}
            and (
                contract.get("delegation_required")
                or contract.get("agent_required_delegation")
                or contract.get("user_requested_delegation")
            )
        ) or active_supervision_contract
        if (
            process.task_kind == "answer"
            and not contract_declared
            and not restored_delegate_contract
        ):
            # The adaptive public turn must have one unambiguous workflow
            # entry point.  A first declares the semantic facts; the Host then
            # compiles those facts into either self execution or delegation.
            # Advertising both authority-expansion and delegation before that
            # compilation made weaker providers guess between mutually
            # exclusive paths, often obtaining a useless governance lease or
            # attempting a B before a contract existed.  Explicit manual B
            # creation is already a Host fact, so that path remains available.
            authorized.pop("request_self_execute", None)
            manual_b_creation = bool(host_requirements.get("manual_B_creation"))
            if not manual_b_creation:
                for name in (
                    "delegate_task", "delegate_task_dag", "delegate_task_bundle",
                ):
                    authorized.pop(name, None)
                # This is an execution boundary, not merely prompt advice.
                # Before the semantic contract exists, a supervisor may answer
                # directly, gather read-only evidence, declare the contract, or
                # ask the user to resolve genuine ambiguity.  Protected write,
                # command, test and delegation tools stay hidden until the Host
                # compiles one route; public research does not need an artificial
                # workflow merely to discover whether current evidence exists.
                pre_contract_tools = set(CapabilityProfiles.pre_contract_tools())
                authorized = {
                    name: tool for name, tool in authorized.items()
                    if name in pre_contract_tools
                }
        if str(contract.get("execution_mode") or "").strip().lower() == "self_execute":
            for name in (
                "delegate_task", "delegate_task_dag", "delegate_task_bundle",
            ):
                authorized.pop(name, None)
    # Completion protocols are mutually exclusive.  Public-answer turns close
    # naturally; self-executed mutations submit a structured action claim;
    # delegated supervisors synthesize reviewed child outcomes.  Leaving all
    # three visible made models choose by name rather than current Host state.
    if process.task_kind != "supervisor":
        authorized.pop("complete_supervision", None)
    if process.task_kind != "action":
        authorized.pop("complete_task", None)
    if process.task_kind != "review":
        authorized.pop("complete_review", None)
    if (
        process.actor_kind == "supervisor"
        and process.capability_profile_id == "supervisor.control"
        and bool(
            dict(
                (process.context_snapshot or {}).get("task_contract") or {}
            ).get("revision")
        )
        and "request_self_execute" in dispatcher_tools
    ):
        authorized["request_self_execute"] = dispatcher_tools["request_self_execute"]
    if process.task_kind == "action" and "complete_task" in dispatcher_tools:
        # Host protocol tools are issued by task semantics rather than the
        # caller-selected capability profile.  Keep the provider surface and
        # RingGate's execution surface identical by recording that scoped grant.
        process.tool_registry.add_scoped("complete_task")
        authorized["complete_task"] = dispatcher_tools["complete_task"]
    if process.task_kind == "review" and "complete_review" in dispatcher_tools:
        process.tool_registry.add_scoped("complete_review")
        authorized["complete_review"] = dispatcher_tools["complete_review"]
    return authorized, unavailable


def _supervisor_is_coordination_only(process: AgentProcess) -> bool:
    """Return whether the current task epoch committed A to delegation."""
    if process.actor_kind != "supervisor":
        return False
    from backend.core.loop.task_contract import get_task_contract

    contract = get_task_contract(process)
    mode = str(contract.get("execution_mode") or "").strip().lower()
    if mode == "delegate":
        return True
    return bool(
        contract.get("delegation_required")
        or contract.get("agent_required_delegation")
    ) and mode in {"", "either"}

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


_STRUCTURAL_SUPERVISOR_WORKFLOW_TOOLS = frozenset({
    "delegate_task", "delegate_task_dag", "delegate_task_bundle",
    "send_feedback", "cancel_agent", "request_review",
    "review_child_outcome", "promote_agent_changes", "complete_supervision",
})

_NON_DELIVERY_CONTROL_TOOLS = _STRUCTURAL_SUPERVISOR_WORKFLOW_TOOLS | frozenset({
    "declare_task_contract", "request_user_decision", "request_permission",
})


def _apply_host_task_transitions(process: AgentProcess, results: list) -> bool:
    """Promote adaptive A turns from committed Host events, never prose.

    Public turns begin with answer completion semantics but a stable supervisor
    tool surface.  Only an actual child/supervision workflow promotes the turn
    to supervisor semantics.  Declaring a contract, asking a question or
    obtaining permission is control-plane bookkeeping, not evidence that the
    user's task became a delegated workflow.  A failed delegation still
    promotes so it cannot vanish into a plain-text answer.  A capability lease
    alone does not change task semantics; committed mutating receipts do.
    """
    if process.actor_kind != "supervisor" or process.task_kind != "answer":
        return False
    from backend.core.loop.operation_policy import is_effectful_mutation
    if any(
        result.tool_name not in _NON_DELIVERY_CONTROL_TOOLS
        and
        not result.is_error
        and is_effectful_mutation(
            str((getattr(result, "receipt", None) or {}).get("effect", "read"))
        )
        for result in results
    ):
        process.task_kind = "action"
        return True
    workflow_results = [
        result for result in results
        if result.tool_name in _STRUCTURAL_SUPERVISOR_WORKFLOW_TOOLS
    ]
    if not workflow_results:
        return False
    delegation_tools = {"delegate_task", "delegate_task_dag", "delegate_task_bundle"}
    from backend.core.loop.task_contract import get_task_contract
    contract = get_task_contract(process)
    delegation_is_binding = bool(
        contract.get("delegation_required")
        or contract.get("agent_required_delegation")
        or contract.get("user_requested_delegation")
        or dict(contract.get("host_requirements") or {}).get("manual_B_creation")
    )
    for result in workflow_results:
        if result.tool_name not in delegation_tools or not result.is_error:
            continue
        # A hallucinated or stale delegation call outside a compiled delegation
        # route is a recoverable capability error.  It must not manufacture a
        # new required-B completion gate and hold an otherwise valid self-run
        # task hostage.  Real delegation intent is already durable in the
        # semantic contract or an explicit Host requirement.
        if not delegation_is_binding:
            continue
        data = dict(getattr(result, "data", None) or {})
        if data.get("intent_recorded"):
            continue
        from backend.core.loop.task_contract import record_unbound_delegation_failure
        record_unbound_delegation_failure(
            process,
            tool_name=result.tool_name,
            error_info={
                "name": str(
                    (getattr(result, "diagnostics", None) or {}).get("code")
                    or "TOOL_ERROR"
                ),
                "message": str(
                    getattr(result, "error", "") or "delegation tool failed"
                ),
                "retryable": True,
            },
        )
    successful_workflow = any(not result.is_error for result in workflow_results)
    if successful_workflow or delegation_is_binding:
        process.task_kind = "supervisor"
        return True
    return False


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


def _compact_receipt(receipt: dict) -> dict:
    """Keep completion evidence useful without duplicating tool payload paths."""
    keys = (
        "receipt_id", "execution_id", "call_index", "tool_name", "effect",
        "effect_state", "succeeded", "committed", "error_code",
        "task_id", "test_id", "test_target", "test_passed", "test_seeds",
        "dynamic_tool_name", "definition_digest", "definition_version",
        "definition_steps", "child_receipt_ids",
    )
    return {key: receipt[key] for key in keys if key in receipt}


def _delegated_outcome_metadata(process: AgentProcess) -> list[dict]:
    manager = getattr(process, "_manager", None)
    outcomes: list[dict] = []
    if getattr(process, "coordination_snapshot", None):
        _child_ids, contracts, reviews = process.coordination_snapshot()
    else:
        contracts = dict(process.delegated_contracts or {})
        reviews = dict(process.child_reviews or {})
    for child_id, contract in contracts.items():
        if contract.get("inherited_from_process_id"):
            # Historical ownership remains queryable through the process DAG;
            # it is not evidence produced by this submitted task.
            continue
        child = manager.get(child_id) if manager else None
        result = child.result if child is not None and isinstance(child.result, dict) else {}
        outcomes.append({
            "process_id": child_id,
            "task_id": getattr(child, "active_task_id", "") if child else "",
            "status": result.get(
                "status", getattr(getattr(child, "status", None), "value", "missing")
            ),
            "task_kind": contract.get("task_kind", ""),
            "required_for_parent_completion": contract.get(
                "required_for_parent_completion", True
            ),
            "required_test_ids": list(contract.get("required_test_ids", [])),
            "required_tool_calls": [
                dict(item) for item in list(contract.get("required_tool_calls", []))
                if isinstance(item, dict)
            ],
            "superseded_by": contract.get("superseded_by", ""),
            "supervisor_review": dict(reviews.get(child_id, {})),
            "completion_claim": (
                child.completion_claim.to_dict()
                if child is not None and getattr(child.completion_claim, "to_dict", None)
                else None
            ),
            "tool_receipts": [
                _compact_receipt(item)
                for item in list(getattr(child, "tool_receipts", []) or [])
            ],
        })
    return outcomes


def _capture_completion_recovery(process: AgentProcess) -> None:
    """Record the durable rejected-then-corrected sequence exactly once."""
    if not process.completion_rejections:
        return
    context, _version = process.read_context_snapshot()
    project_name = str(context.get("project_name", "")).strip()
    if not project_name:
        return
    try:
        from backend.core.knowledge.harvest import capture_signal
        capture_signal(
            "completion_recovery",
            {
                "trigger": process.task_description[:240],
                "task_id": process.active_task_id or process.process_id,
                "rejections": list(process.completion_rejections),
                "resolved_at_step": process.steps_used,
            },
            project_name,
            source_event_id=(process.active_task_id or process.process_id),
        )
    except Exception:
        # Knowledge capture cannot turn an otherwise valid delivery into failure.
        return


def _make_result(process: AgentProcess, session: "AgentSession",
                 response: str, *, duration_ms: float = 0.0,
                 error_code: str = "", error_message: str = "",
                 outcome_status: OutcomeStatus | None = None,
                 doom_loop: bool = False,
                 window_action: str = "none",
                 window_usage_ratio: float = 0.0) -> dict:
    started = getattr(process, "_turn_started_monotonic", None)
    if started is not None:
        duration_ms = max(0.0, (time.monotonic() - started) * 1000)
    agent_duration_ms = duration_ms
    request_started_at = str(
        process.runtime_preferences.get("request_started_at_utc") or ""
    )
    if request_started_at:
        try:
            from datetime import datetime, timezone
            parsed = datetime.fromisoformat(request_started_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            durable_wall_ms = (
                datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
            ).total_seconds() * 1000
            # A backwards clock adjustment must never make a completed timer
            # shrink below measured agent execution.
            duration_ms = max(agent_duration_ms, durable_wall_ms, 0.0)
        except (TypeError, ValueError, OverflowError):
            pass
    if outcome_status is None:
        outcome_status = (
            OutcomeStatus.COMPLETED
            if process.status == ProcessStatus.COMPLETED
            else OutcomeStatus.CANCELLED
            if process.status in (ProcessStatus.CANCELLED, ProcessStatus.CANCELLING)
            else OutcomeStatus.TIMED_OUT
            if process.status == ProcessStatus.TIMED_OUT
            else OutcomeStatus.FAILED
        )
    error = None
    if error_code:
        from backend.core.errors import ERROR_CATALOG, error_payload
        details = error_payload(error_code, message=error_message).get("error_info", {}) if error_code in ERROR_CATALOG else {}
        error = TaskError(code=error_code, message=error_message or error_code, details=details)
    from backend.core.loop.completion_protocol import HostCompletionEvidence
    completion_evidence = (
        HostCompletionEvidence.collect(process).to_dict()
        if process.task_kind == "action" else None
    )
    provider_usage = list(session.provider_usage)[int(
        getattr(process, "_turn_provider_usage_start", 0) or 0
    ):]
    cache_telemetry = list(session.cache_telemetry)[int(
        getattr(process, "_turn_cache_telemetry_start", 0) or 0
    ):]
    task_receipts = list(process.tool_receipts)[int(
        getattr(process, "_turn_tool_receipts_start", 0) or 0
    ):]
    wire = TaskOutcome(
        task_id=process.active_task_id or process.process_id,
        process_id=process.process_id,
        status=outcome_status,
        process_status=process.status.value,
        response=response,
        error=error,
        steps_used=process.steps_used,
        steps_remaining=max(0, process.max_steps - process.steps_used),
        session_tokens=session.estimate_tokens(),
        duration_ms=duration_ms,
        llm_used=process.steps_used > 0,
        tool_calls_executed=len(task_receipts),
        metadata={
            "doom_loop": doom_loop,
            "window_action": window_action,
            "window_usage_ratio": window_usage_ratio,
            "session_id": session.session_id,
            "trace_id": str(
                getattr(process.task_budget, "task_id", "")
                or process.active_task_id or process.process_id
            ),
            "context_epoch": session.context_epoch,
            "provider_usage": provider_usage,
            "cache_telemetry": cache_telemetry,
            "cache_summary": session.cache_summary(cache_telemetry),
            "session_cache_summary": session.cache_summary(),
            "completion_evidence": completion_evidence,
            "task_kind": process.task_kind,
            "capability_profile_id": process.capability_profile_id,
            "capability_lease_id": (
                process.capability_lease.lease_id
                if process.capability_lease is not None else ""
            ),
            "completion_claim": (
                process.completion_claim.to_dict()
                if process.completion_claim is not None else None
            ),
            "completion_exception": dict(
                process.runtime_preferences.get("completion_exception", {}) or {}
            ),
            "pending_decision": (
                dict(process.pending_decision)
                if process.pending_decision is not None else None
            ),
            "tool_receipts": [_compact_receipt(item) for item in task_receipts],
            "task_tree_budget": (
                process.task_budget.snapshot() if process.task_budget is not None else None
            ),
            "delegated_outcomes": _delegated_outcome_metadata(process),
            "timing": {
                "request_started_at": request_started_at,
                "agent_elapsed_ms": round(agent_duration_ms, 3),
                "end_to_end_elapsed_ms": round(duration_ms, 3),
            },
        },
    ).to_dict()
    # Per-call usage remains durable in Trace events.  Keep only a bounded
    # recent window in the mutable session checkpoint after the task-local
    # outcome has taken its independent copies.
    del session.provider_usage[:-64]
    del session.cache_telemetry[:-64]
    if process.mailbox is not None and outcome_status not in {
        OutcomeStatus.COMPLETED, OutcomeStatus.AWAITING_USER,
    }:
        process.mailbox.close(error_message or outcome_status.value)
    process.result = wire
    return wire


def _error_result(process: AgentProcess, code: str, message: str) -> dict:
    wire = TaskOutcome.failed(
        task_id=process.active_task_id or process.process_id,
        process_id=process.process_id,
        process_status=process.status.value,
        code=code,
        message=message,
        steps_used=process.steps_used,
        llm_used=False,
        metadata={"trace_id": str(
            getattr(process.task_budget, "task_id", "")
            or process.active_task_id or process.process_id
        )},
    ).to_dict()
    started = getattr(process, "_turn_started_monotonic", None)
    if started is not None:
        wire["duration_ms"] = max(0.0, (time.monotonic() - started) * 1000)
    if process.mailbox is not None:
        process.mailbox.close(message)
    process.result = wire
    return wire


def _cancellation_result(process: AgentProcess, session: "AgentSession",
                         response: str = "", *, duration_ms: float = 0.0) -> dict:
    timed_out = process.cancellation_reason == "task_deadline"
    process.status = ProcessStatus.TIMED_OUT if timed_out else ProcessStatus.CANCELLED
    return _make_result(
        process, session, response, duration_ms=duration_ms,
        error_code=("TASK_TREE_DEADLINE_EXCEEDED" if timed_out else "TASK_CANCELLED"),
        error_message=(
            "Agent task tree exceeded its admitted deadline"
            if timed_out else "Agent task was cancelled"
        ),
        outcome_status=(OutcomeStatus.TIMED_OUT if timed_out else OutcomeStatus.CANCELLED),
    )


def _budget_exhausted_result(process: AgentProcess, session: "AgentSession",
                             exc, *, response: str = "",
                             duration_ms: float = 0.0) -> dict:
    timed_out = bool(getattr(exc, "timed_out", False))
    process.status = ProcessStatus.TIMED_OUT if timed_out else ProcessStatus.FAILED
    return _make_result(
        process, session, response, duration_ms=duration_ms,
        error_code=str(getattr(exc, "code", "TASK_TREE_BUDGET_EXHAUSTED")),
        error_message=str(exc),
        outcome_status=(OutcomeStatus.TIMED_OUT if timed_out else OutcomeStatus.FAILED),
    )


def _status_result(process: AgentProcess, session: "AgentSession") -> dict:
    if process.status == ProcessStatus.COMPLETED and isinstance(process.result, dict):
        return process.result
    if process.status in (
        ProcessStatus.CANCELLING, ProcessStatus.CANCELLED, ProcessStatus.KILLED,
    ):
        return _make_result(
            process, session, "",
            error_code="TASK_CANCELLED",
            error_message="Agent task was cancelled",
            outcome_status=OutcomeStatus.CANCELLED,
        )
    if process.status == ProcessStatus.TIMED_OUT:
        return _make_result(
            process, session, "",
            error_code="TASK_TIMED_OUT",
            error_message="Agent task timed out",
            outcome_status=OutcomeStatus.TIMED_OUT,
        )
    return _make_result(
        process, session, "",
        error_code="PROCESS_NOT_RUNNABLE",
        error_message=f"Agent process is {process.status.value}",
        outcome_status=OutcomeStatus.FAILED,
    )


# ── v0.36: Nudge Counter ──────────────────────────────────

def _inc_nudge_counter(process: AgentProcess, nudge_type: str) -> None:
    process._nudge_counters[nudge_type] = process._nudge_counters.get(nudge_type, 0) + 1


def _get_nudge_count(process: AgentProcess, nudge_type: str) -> int:
    return process._nudge_counters.get(nudge_type, 0)


def _build_system_message_for_llm(process: AgentProcess,
                                   base_system: str) -> str:
    """L2-ext: 动态拼接中途约束到 system message 末尾。"""
    if not getattr(process, 'task_constraints', None):
        return base_system

    constraint_block = "\n\n## Task-level Constraints (this task only)\n" + "\n".join(
        f"- {c}" for c in process.task_constraints
    )
    return base_system + constraint_block
