"""Build task outcomes from actual runtime state and evidence."""
from __future__ import annotations
import time
from .answer_continuation import clear_answer_fragments
from .lifecycle import transition
from .models import AgentProcess, ProcessStatus
from .outcome import OutcomeStatus, TaskError, TaskOutcome


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
    if process.status in (ProcessStatus.CANCELLED, ProcessStatus.CANCELLING):
        outcome_status = OutcomeStatus.CANCELLED
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
    if outcome_status == OutcomeStatus.COMPLETED:
        clear_answer_fragments(process)
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
    transition(process, ProcessStatus.TIMED_OUT if timed_out else ProcessStatus.CANCELLED)
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
    transition(process, ProcessStatus.TIMED_OUT if timed_out else ProcessStatus.FAILED)
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

