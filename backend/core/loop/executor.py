"""Agent run orchestration over explicit admission, provider and tool boundaries.

Controllers own protocol recovery, policy, execution, interaction and delivery.
The loop orders them without duplicating authority or persisting another ledger.
"""

from __future__ import annotations

from backend.core.loop.lifecycle import transition
from backend.core.loop.tool_batch_runtime import ToolBatchRuntime
from backend.core.loop.runtime_outcomes import (
    _make_result, _error_result,
)
import time
from typing import TYPE_CHECKING
from backend.core.loop.answer_continuation import clear_answer_fragments, pending_answer_text
# Retained as an import surface for existing LoopGuard instrumentation.
from backend.core.loop.loop_guard import LoopGuard
from backend.core.loop.models import AgentProcess, ProcessStatus
from backend.core.loop.outcome import OutcomeStatus

if TYPE_CHECKING:
    from backend.core.loop.llm import LLMProvider
    from backend.core.dispatch.dispatcher import ToolDispatcher

def agent_step(
    process: AgentProcess,
    llm_provider: "LLMProvider",
    instruction: str = "",
    dispatcher: "ToolDispatcher | None" = None,
    workspace_path: str = "",
    on_stream_event: callable = None,
) -> dict:
    """Run the admitted task until delivery, suspension or a bounded failure."""
    from .run_admission import admit_agent_run
    admitted = admit_agent_run(process, llm_provider, instruction, dispatcher, workspace_path, on_stream_event)
    if admitted.result is not None:
        return admitted.result
    services = admitted.services
    session, surface, ctx = process.session, services.surface, services.context
    loop_guard, route, governance = services.guard, services.route, services.governance
    turn_wall_started = services.started_wall
    _emit_observation = services.observations.publish
    on_stream_event = _emit_observation if on_stream_event is not None else None

    tool_batch = ToolBatchRuntime(process, session, lambda: surface.tools, loop_guard, ctx,
                                 workspace_path, turn_wall_started, _emit_observation)
    _execute_resumed_batch = tool_batch.execute_resumed_batch


    def _apply_mailbox_at_boundary() -> int:
        applied = _apply_pending_instructions(process, session)
        if applied:
            _emit_observation({
                "event": "mailbox_applied",
                "applied_count": applied,
            })
        return applied

    from .context_compaction import ContextCompaction
    _run_context_compaction = ContextCompaction(
        process, session, llm_provider, lambda: route.window, lambda: governance.harness,
        _emit_observation, on_stream_event).run

    from .semantic_review_coordinator import SemanticReviewCoordinator
    _check_alignment = SemanticReviewCoordinator(process, llm_provider, instruction, route,
        surface.refresh_prompt, ctx.artifacts['engineering_workflow'], _emit_observation, on_stream_event).check

    def _pause_blocked_delivery(guard_result, candidate):
        from .completion_control import blocked_delivery
        from .decision_support import format_decision_request
        decision = blocked_delivery(process, guard_result, candidate)
        if decision is None:
            return None
        transition(process, ProcessStatus.AWAITING_USER)
        _emit_observation({"event": "decision_required", "decision": decision})
        result = _make_result(process, session, format_decision_request(decision),
            outcome_status=OutcomeStatus.AWAITING_USER)
        process.result = result
        return result

    from .delivery_runtime import DeliveryRuntime
    capture_recovery = lambda current: _capture_completion_recovery(current, _emit_observation)
    delivery_runtime = DeliveryRuntime(process, session, loop_guard, governance.active_bus,
        emit=_emit_observation, apply_mailbox=_apply_mailbox_at_boundary,
        pause_blocked=_pause_blocked_delivery, capture_recovery=capture_recovery,
        increment_nudge=_inc_nudge_counter, nudge_count=_get_nudge_count)

    alignment_result = _check_alignment()
    if alignment_result is not None and "alignment_repair" not in alignment_result:
        return alignment_result

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
    from .provider_turn_runtime import ProviderTurnRuntime
    provider_turn = ProviderTurnRuntime(process, session, llm_provider,
        emit=_emit_observation, frontend_emit=on_stream_event,
        route_changed=route.selected,
        route=lambda: route.route, envelope=lambda: route.envelope,
        compact=_run_context_compaction)
    from .provider_progress import ProviderProgress
    from .tool_proposal_runtime import ToolProposalRuntime
    provider_progress = ProviderProgress(process, session, loop_guard, _emit_observation)
    tool_proposals = ToolProposalRuntime(process, session, lambda: surface.tools, _check_alignment)
    from .turn_preparation import TurnPreparation
    from .tool_outcome_runtime import ToolOutcomeRuntime
    preparation = TurnPreparation(process, session, llm_provider, lambda: route.window,
        governance, _run_context_compaction, _apply_mailbox_at_boundary, _emit_observation)
    tool_outcomes = ToolOutcomeRuntime(process, session, delivery_runtime, _emit_observation,
        _pause_blocked_delivery, capture_recovery)
    while process.steps_used < process.max_steps:
        prepared = preparation.prepare()
        if prepared.result is not None:
            return prepared.result
        window_check = prepared.window

        from .provider_request_surface import prepare_provider_request
        request = prepare_provider_request(process, llm_provider, surface, route, _emit_observation)
        provider_boundary = provider_turn.run(request.tools, request.web_mode, request.protocol)
        start = provider_turn.started
        if provider_boundary.result is not None:
            return provider_boundary.result
        if provider_boundary.retry:
            continue
        response_state = provider_turn.response
        tool_calls = response_state.tool_calls
        content, duration_ms = provider_turn.content, provider_turn.duration_ms

        progress_boundary = provider_progress.handle(response_state, duration_ms)
        if progress_boundary.result is not None:
            return progress_boundary.result
        if progress_boundary.retry:
            continue

        # Read current policy after provider work and before admission/delivery.
        governance.refresh()
        delivery_runtime.signal_bus = governance.active_bus
        if tool_calls:
            proposal_boundary = tool_proposals.review(tool_calls, content)
            if proposal_boundary.result is not None:
                return proposal_boundary.result
            if proposal_boundary.retry:
                continue
            batch_boundary = tool_batch.execute_live_batch(tool_calls)
            if batch_boundary.result is not None:
                return batch_boundary.result
            if batch_boundary.retry:
                continue
            results, task_kind_changed = batch_boundary.results, batch_boundary.task_kind_changed
            outcome = tool_outcomes.evaluate(results, content, (time.time() - start) * 1000,
                                             window_check, governance.signals)
            if outcome.result is not None:
                return outcome.result
            if not outcome.retry:
                tool_outcomes.notify_evidence_ready()

            unavailable_tools = surface.refresh_after(results, task_kind_changed, loop_guard)
            if unavailable_tools:
                transition(process, ProcessStatus.FAILED)
                return _error_result(process, "CAPABILITY_TOOL_UNAVAILABLE",
                    "Capability references unavailable tools: " + ", ".join(unavailable_tools))
            if outcome.retry:
                continue

            _track_step(process, content[:200])
            continue

        content = pending_answer_text(process) + content
        # Semantic delivery readiness is separate from receipt completeness.
        # A fluent answer or a prose question cannot silently end unresolved work.
        alignment_result = _check_alignment(content)
        if alignment_result is not None:
            if "alignment_repair" in alignment_result:
                clear_answer_fragments(process)
                _inc_nudge_counter(process, "alignment_repair")
                if _get_nudge_count(process, "alignment_repair") >= 3:
                    transition(process, ProcessStatus.FAILED)
                    return _make_result(process, session, content, error_code="ALIGNMENT_DELIVERY_REJECTED",
                                        error_message="User requirements were not adopted", outcome_status=OutcomeStatus.FAILED)
                session.append_user("[HOST REQUIREMENT CHECK] " + alignment_result["alignment_repair"],
                                    message_type="host_alignment_repair")
                continue
            return alignment_result

        delivery_boundary = delivery_runtime.evaluate_text(content, duration_ms, window_check, governance.signals)
        if delivery_boundary.result is not None:
            return delivery_boundary.result
        if delivery_boundary.retry:
            continue
        _track_step(process, content[:200])

        # v0.42: doom_loop 检测已接入 LoopGuard.check()（通过 check_doom_loop_safe）。
        # 普通文本路径中，LoopGuard 在 agent_step 的完成检查分支中调用，
        # 此处保留冗余检测作为最后防线（check_budget_continuity 也在这里）。
        # 如果 LoopGuard missed 了一个 doom_loop（例如 completion markers 出现在非完成上下文中），
        # 这个直接调用是最后的安全网。

        # 纯文本死循环检测
        if _repeated_plain_text(process, content):
            transition(process, ProcessStatus.FAILED)
            return _make_result(process, session, content,
                              duration_ms=duration_ms,
                              error_code="PLAIN_TEXT_LOOP_DETECTED",
                              error_message="Agent repeated the same plain-text response",
                              outcome_status=OutcomeStatus.FAILED)

        # Token budget 延续检测
        budget_check = route.window.check_budget_continuity(session)
        if budget_check["stagnant"]:
            session.append_user(
                "[系统提示] 最近几轮对话无实质进展（无工具调用或完成信号）。"
                "请调用工具或回复 TASK_COMPLETE 结束任务。"
            )
            continue

    # max_steps 耗尽
    transition(process, ProcessStatus.FAILED)
    return _make_result(process, session, "", duration_ms=0,
                       error_code="MAX_STEPS_EXHAUSTED",
                       error_message="Agent exhausted its step budget",
                       outcome_status=OutcomeStatus.FAILED)


# Compatibility exports; implementations live with their respective responsibility.
from .loop_bookkeeping import (_apply_pending_instructions, _latest_error_code, _track_step,
    _repeated_plain_text, _capture_completion_recovery, _inc_nudge_counter, _get_nudge_count)
from .prompt_admission import (_refresh_compiled_prompt, _inject_tool_prompt, _build_system_message_for_llm)
from .legacy_policy import _policy_pre_check, _tool_already_called, _tools_already_called
from .turn_guidance import _BASE_CURRENT_TURN_ENVELOPE, _build_current_turn_envelope
from .tool_surface import _select_authorized_tools, _supervisor_is_coordination_only
from .prompt_admission import TOOL_PROMPT_MARKER
from .tool_result_support import (_NON_DELIVERY_CONTROL_TOOLS, _STRUCTURAL_SUPERVISOR_WORKFLOW_TOOLS,
    _apply_host_task_transitions, _extract_error_code, _extract_referenced_files, _host_tool_error_result)
from .provider_turn_protocol import (_stream_recovery_message, _bounded_exception_chain,
    _reasoning_continuation, _provider_continuation, _filter_incomplete_response_artifacts,
    _iter_provider_events, _provider_idle_timeout)
from .provider_turn_runtime import _to_provider_tool_calls, MAX_STREAM_RECOVERIES
from .runtime_outcomes import (_compact_receipt, _delegated_outcome_metadata,
    _cancellation_result, _budget_exhausted_result, _status_result)


def _build_internal_tools(process, available_tools=None, workspace_path='', llm_provider=None,
                          dispatcher=None, on_stream_event=None):
    from .protocol_tools.registry import build_internal_tools
    return build_internal_tools(process, available_tools, workspace_path, llm_provider, dispatcher, on_stream_event)
