"""Admission for user-facing A-level chat turns.

Natural-language wording is deliberately *not* an authority boundary.  Every
ordinary public turn starts with the same supervisor control surface and cheap
answer completion semantics.  If A uses a coordination or self-execution tool,
the Host promotes the task from that structured event and enables the stronger
completion contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.core.loop.completion_protocol import TaskKind


@dataclass(frozen=True)
class ChatAdmission:
    task_kind: str
    capability_profile_id: str
    reason: str


def classify_chat_admission(
    message: str,
    *,
    explicit_task_kind: str | None = None,
) -> ChatAdmission:
    """Return a server-owned task/profile pair for one public chat turn."""

    explicit = str(explicit_task_kind or "").strip().lower()
    if explicit and explicit != "auto":
        profile = (
            "supervisor.answer"
            if explicit == TaskKind.ANSWER.value
            else "supervisor.control"
        )
        return ChatAdmission(explicit, profile, "caller_explicit_task_kind")

    # Do not inspect ``message`` here.  Quoted text, negation, follow-up turns,
    # multilingual requests and new tool types make keyword classification both
    # lossy and security-sensitive.  The stable surface lets the model choose a
    # tool; RingGate/leases constrain the actual operation, and successful Host
    # tool receipts drive task-kind promotion.
    del message
    return ChatAdmission(
        TaskKind.ANSWER.value,
        "supervisor.control",
        "adaptive_supervisor_default",
    )
