"""Canonical task outcomes shared by the Agent runtime boundaries.

Executor, daemon, clients, and MCP adapters must agree on terminal semantics.
Transport failures remain exceptions; task failures are returned as data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class OutcomeStatus(str, Enum):
    """Terminal state of one submitted Agent task/turn."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    DEGRADED = "degraded"
    AWAITING_USER = "awaiting_user"


@dataclass(frozen=True)
class TaskError:
    """Stable machine code plus human-readable diagnostic text."""

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class TaskOutcome:
    """Versioned wire value for the result of one Agent task.

    ``process_status`` describes the long-lived Agent process. ``status``
    describes this submitted task. Keeping them separate is required for
    multi-turn sessions where a task can fail without destroying identity.
    """

    task_id: str
    process_id: str
    status: OutcomeStatus
    process_status: str
    response: str = ""
    error: TaskError | None = None
    steps_used: int = 0
    steps_remaining: int = 0
    session_tokens: int = 0
    duration_ms: float = 0.0
    llm_used: bool = False
    tool_calls_executed: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    WIRE_VERSION = 1

    @property
    def succeeded(self) -> bool:
        return self.status == OutcomeStatus.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.WIRE_VERSION,
            "task_id": self.task_id,
            "process_id": self.process_id,
            "status": self.status.value,
            "process_status": self.process_status,
            "response": self.response,
            "error": self.error.to_dict() if self.error else None,
            "steps_used": self.steps_used,
            "steps_remaining": self.steps_remaining,
            "session_tokens": self.session_tokens,
            "duration_ms": self.duration_ms,
            "llm_used": self.llm_used,
            "tool_calls_executed": self.tool_calls_executed,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def failed(
        cls,
        *,
        task_id: str,
        process_id: str,
        process_status: str,
        code: str,
        message: str,
        retryable: bool = False,
        steps_used: int = 0,
        llm_used: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> "TaskOutcome":
        return cls(
            task_id=task_id,
            process_id=process_id,
            status=OutcomeStatus.FAILED,
            process_status=process_status,
            error=TaskError(code, message, retryable=retryable),
            steps_used=steps_used,
            llm_used=llm_used,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TaskOutcome":
        """Validate and reconstruct an outcome received across a boundary."""
        try:
            status = OutcomeStatus(str(raw["status"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("Invalid TaskOutcome status") from exc

        error_raw = raw.get("error")
        error = None
        if error_raw is not None:
            if not isinstance(error_raw, Mapping):
                raise ValueError("TaskOutcome.error must be an object or null")
            code = str(error_raw.get("code", ""))
            message = str(error_raw.get("message", ""))
            if not code:
                raise ValueError("TaskOutcome.error.code is required")
            error = TaskError(
                code=code,
                message=message,
                retryable=bool(error_raw.get("retryable", False)),
                details=dict(error_raw.get("details") or {}),
            )

        if status == OutcomeStatus.COMPLETED and error is not None:
            raise ValueError("Completed TaskOutcome cannot contain an error")
        if status in (OutcomeStatus.FAILED, OutcomeStatus.TIMED_OUT) and error is None:
            raise ValueError(f"{status.value} TaskOutcome requires an error")

        return cls(
            task_id=str(raw.get("task_id", "")),
            process_id=str(raw.get("process_id", "")),
            status=status,
            process_status=str(raw.get("process_status", "")),
            response=str(raw.get("response", "")),
            error=error,
            steps_used=int(raw.get("steps_used", 0)),
            steps_remaining=int(raw.get("steps_remaining", 0)),
            session_tokens=int(raw.get("session_tokens", 0)),
            duration_ms=float(raw.get("duration_ms", 0.0)),
            llm_used=bool(raw.get("llm_used", False)),
            tool_calls_executed=int(raw.get("tool_calls_executed", 0)),
            metadata=dict(raw.get("metadata") or {}),
        )
