"""Shared Host control for blocked deliveries, independent of claim syntax."""
from __future__ import annotations

from backend.core.loop.lifecycle import transition

from .completion_gates import contract_digest


def blocked_delivery(process, guard_result, candidate):
    gates = list(guard_result.gates)
    if not gates:
        return None
    fingerprint = contract_digest(process) + ":" + "|".join(sorted(g["gate_id"] for g in gates))
    preferences = process.runtime_preferences
    previous = preferences.get("completion_gate_fingerprint")
    repeats = int(preferences.get("completion_gate_repeats", 0)) + 1 if previous == fingerprint else 1
    preferences.update(completion_gate_fingerprint=fingerprint, completion_gate_repeats=repeats,
        completion_candidate=candidate)
    if repeats < 2 or process.pending_decision is not None:
        return None
    from .decision_support import create_completion_exception_decision
    return create_completion_exception_decision(process, {"ready": False, "gates": gates})


def validate_decision(process, request, action):
    if request.get("contract_digest") != contract_digest(process):
        raise ValueError("COMPLETION_SCOPE_CHANGED: inspect the current requirements before deciding")
    options = {option.get("action") for option in request.get("options", [])}
    if action not in options:
        raise ValueError("COMPLETION_DECISION_ACTION_INVALID")
    if action == "accept_partial" and not all(g.get("degradable_by_explicit_user_decision") is True
            for g in request.get("completion_gates", [])):
        raise ValueError("COMPLETION_GATE_NOT_DEGRADABLE")


def execute_control(process):
    """Consume an authenticated control action without another provider call."""
    command = process.runtime_preferences.pop("completion_control_action", None)
    if not command:
        return None
    from .models import ProcessStatus
    from .outcome import OutcomeStatus
    from .runtime_outcomes import _make_result
    request = command["request"]
    action = command["action"]
    try:
        validate_decision(process, request, action)
        task_id = process.active_task_id or process.process_id
        asked = next((e for e in reversed(process.session.host_ledger)
            if e.get("event") == "user_decision_requested" and e.get("decision_id") == request["decision_id"]
            and e.get("task_id") == task_id and e.get("process_id") == process.process_id), None)
        answered = next((e for e in reversed(process.session.host_ledger)
            if e.get("event") == "user_decision_received" and e.get("decision_id") == request["decision_id"]
            and e.get("task_id") == task_id and e.get("process_id") == process.process_id), None)
        if (not asked or not answered or asked.get("contract_digest") != request.get("contract_digest")
                or asked.get("kind") != "completion_exception" or answered.get("selected_action") != action):
            raise ValueError("COMPLETION_DECISION_EVIDENCE_MISSING")
    except ValueError as exc:
        transition(process, ProcessStatus.FAILED)
        return _make_result(process, process.session, "任务要求已变化，先前决定不能用于当前交付。",
            error_code="COMPLETION_SCOPE_CHANGED", error_message=str(exc), outcome_status=OutcomeStatus.FAILED)
    candidate = process.runtime_preferences.get("completion_candidate") or (
        process.completion_claim.result if process.completion_claim else "")
    if action in {"stop", "amend_scope"}:
        manager = getattr(process, "_manager", None)
        if manager is not None:
            manager.kill(process.process_id, reason="user_stopped_incomplete_task")
        process.cancel_requested = True
        process.cancellation_event.set()
        transition(process, ProcessStatus.CANCELLED)
        message = ("已结束当前执行，保留原约束、未完成的验证和已有成果。请提交具体的新任务范围；本次决定未豁免安全要求。"
                   if action == "amend_scope" else "已停止任务；未完成的验证与已有成果均已保留。")
        return _make_result(process, process.session, message + "\n\n" + candidate,
            outcome_status=OutcomeStatus.CANCELLED)
    if action == "accept_partial":
        from .loop_guard import LoopGuard
        from .signals import GovernanceSignal
        context, _ = process.read_context_snapshot()
        signals = [GovernanceSignal.from_dict(s) if isinstance(s, dict) else s for s in context.get("signals") or []]
        result = LoopGuard().check(process, "TASK_COMPLETE\n" + candidate, process.session,
            signals=signals)
        if result.is_complete and result.degraded:
            transition(process, ProcessStatus.COMPLETED)
            return _make_result(process, process.session, candidate, outcome_status=OutcomeStatus.DEGRADED)
        transition(process, ProcessStatus.FAILED)
        return _make_result(process, process.session, "当前门禁已变化，部分交付决定未被沿用于新的缺失证据。",
            error_code="COMPLETION_EXCEPTION_STALE", error_message=result.nudge_text,
            outcome_status=OutcomeStatus.FAILED)
    return None
