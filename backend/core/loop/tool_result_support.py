"""Tool-result interpretation from committed receipts and exact failure facts."""
from __future__ import annotations
from .models import AgentProcess

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
