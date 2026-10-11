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


def _get_nudge_count(process: AgentProcess, nudge_type: str) -> int:
    return process._nudge_counters.get(nudge_type, 0)
