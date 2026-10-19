"""Mailbox, progress and audit bookkeeping; no semantic decisions."""
from .models import AgentProcess
from .session import AgentSession

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


def _latest_error_code(process: AgentProcess, tool_name: str) -> str:
    for receipt in reversed(getattr(process, "tool_receipts", [])):
        if receipt.get("tool_name") == tool_name and not receipt.get("succeeded", False):
            return str(receipt.get("error_code", ""))
    return ""


def _track_step(process: AgentProcess, response_prefix: str) -> None:
    process._step_history.append({
        "tool_name": "llm_call",
        "args": response_prefix,
    })
    if len(process._step_history) > 5:
        process._step_history = process._step_history[-5:]


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


def _capture_completion_recovery(process: AgentProcess, on_event=None) -> None:
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
    except Exception as exc:
        # This observer cannot invalidate delivery, but its failure is retained.
        from .engineering_workflow import EngineeringWorkflow
        EngineeringWorkflow(process, on_event).notice('KNOWLEDGE_CAPTURE_FAILED',
            '交付完成，但恢复经验未能保存；失败已记录。', error_type=type(exc).__name__)


def _inc_nudge_counter(process: AgentProcess, nudge_type: str) -> None:
    process._nudge_counters[nudge_type] = process._nudge_counters.get(nudge_type, 0) + 1


def capture_lesson_outcome(process, result, workspace_path, *, rolled_back=False, on_event=None):
    """Observe exact proposal/execution facts; never claim semantic adoption.

    The original receipt remains authoritative. Failure of this observer must
    not repeat an already executed file operation.
    """
    receipt = result.receipt or {}
    if result.suspended:
        return
    context, _ = process.read_context_snapshot()
    project = str(context.get("project_name") or "")
    if not project:
        return
    try:
        from backend.core.knowledge.harvest import capture_signal, task_failure_relations
        from backend.core.knowledge.evidence_context import task_context_snapshot
        from backend.core.history import HistoryManager
        with HistoryManager.workspace_scope(workspace_path):
            if not receipt.get("lesson_checks"):
                task_id = process.active_task_id or process.process_id
                command_done = receipt.get("command_completed") is True
                test_done = receipt.get("test_completed") is True
                failed = (result.is_error or (command_done and receipt.get("command_exit_code") != 0)
                          or (test_done and receipt.get("test_passed") is False))
                observable = (command_done or test_done
                              or receipt.get("effect_state") == "committed")
                if not failed and not observable:
                    return
                task_context = task_context_snapshot(context)
                relations = [] if failed else task_failure_relations(project, task_id, task_context, receipt)
                if not failed and not relations:
                    return
                data = result.data if isinstance(result.data, dict) else {}
                capture_signal("execution_failure" if failed else "execution_followup", {
                    "task_context": task_context,
                    "process_id": process.process_id,
                    "failure_relations": relations,
                    "knowledge_excerpts": data.get("knowledge_excerpts", [])
                        if result.tool_name in {"edit_file", "write_file"} and receipt.get("effect_state") == "committed" else [],

                    "task_id": task_id, "tool_name": result.tool_name,
                    "observation": {key: str(value)[:1500 if key == "diff" else 1000]
                                    for key, value in {"error": result.error,
                                    **{key: data[key] for key in ("error", "stdout", "stderr", "diff")
                                       if key in data}}.items()},
                    "observation_truncated_fields": [key for key in ("error", "stdout", "stderr", "diff")
                        if key in data and len(str(data[key])) > (1500 if key == "diff" else 1000)],
                    "target_files": receipt.get("files", []), "receipt": dict(receipt),
                    "related_failure_signal_ids": [item["signal_id"] for item in relations if item["same_contract"] is not False],
                    "is_error": bool(result.is_error), "rolled_back": rolled_back,
                }, project, source_event_id=str(receipt.get("receipt_id") or result.id))
                return
            capture_signal("lesson_feedback", {
                "task_id": process.active_task_id or process.process_id,
                "receipt_id": receipt.get("receipt_id"), "tool_name": result.tool_name,
                "candidate_sha256": receipt.get("candidate_sha256"),
                "before_sha256": receipt.get("before_sha256"),
                "after_sha256": receipt.get("after_sha256"),
                "target_files": receipt.get("files", []),
                "lesson_checks": receipt["lesson_checks"],
                "effect_state": receipt.get("effect_state"),
                "succeeded": receipt.get("succeeded", False),
                "rolled_back": rolled_back,
                "user_override": receipt.get("lesson_user_override", False),
            }, project, source_event_id=str(receipt.get("receipt_id") or result.id))
    except Exception as exc:
        from .engineering_workflow import EngineeringWorkflow
        EngineeringWorkflow(process, on_event).notice(
            "KNOWLEDGE_CAPTURE_FAILED", "工具结果已保留，但 lesson 结果观察未能保存。",
            error_type=type(exc).__name__,
        )


def _get_nudge_count(process: AgentProcess, nudge_type: str) -> int:
    return process._nudge_counters.get(nudge_type, 0)
