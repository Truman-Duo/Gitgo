"""FIFO control mailbox owned by one Agent process actor."""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime


@dataclass
class MailboxMessage:
    message_id: str
    kind: str
    content: str
    status: str = "accepted"  # accepted | applied | rejected
    accepted_at: str = ""
    applied_task_id: str = ""
    applied_at_step: int | None = None
    rejection_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "kind": self.kind,
            "status": self.status,
            "accepted_at": self.accepted_at,
            "applied_task_id": self.applied_task_id,
            "applied_at_step": self.applied_at_step,
            "rejection_reason": self.rejection_reason,
        }

    def to_durable_dict(self) -> dict:
        return {**self.to_dict(), "content": self.content}


class MailboxClosedError(RuntimeError):
    pass


class AgentMailbox:
    """Thread-safe FIFO mailbox with an atomic completion barrier.

    ``close_if_empty`` prevents a completion/instruction race: either the new
    instruction is visible to the executor, or the sender is told the mailbox
    is already closed. It can never be acknowledged and silently abandoned.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: deque[MailboxMessage] = deque()
        self._messages: dict[str, MailboxMessage] = {}
        self._closed = False
        self._close_reason = ""

    @classmethod
    def from_durable_snapshot(cls, snapshot: dict | None) -> "AgentMailbox":
        mailbox = cls()
        raw = dict(snapshot or {})
        pending_ids = {str(item) for item in (raw.get("pending_ids") or [])}
        with mailbox._lock:
            for item in raw.get("messages", []) or []:
                if not isinstance(item, dict) or not item.get("message_id"):
                    continue
                message = MailboxMessage(
                    message_id=str(item["message_id"]),
                    kind=str(item.get("kind") or "user_instruction"),
                    content=str(item.get("content") or ""),
                    status=str(item.get("status") or "accepted"),
                    accepted_at=str(item.get("accepted_at") or ""),
                    applied_task_id=str(item.get("applied_task_id") or ""),
                    applied_at_step=item.get("applied_at_step"),
                    rejection_reason=str(item.get("rejection_reason") or ""),
                )
                mailbox._messages[message.message_id] = message
                if message.message_id in pending_ids and message.status == "accepted":
                    mailbox._pending.append(message)
            mailbox._closed = bool(raw.get("closed", False))
            mailbox._close_reason = str(raw.get("close_reason") or "")
        return mailbox

    def enqueue_instruction(self, content: str) -> MailboxMessage:
        return self._enqueue(content, kind="user_instruction")

    def enqueue_governance_update(self, content: str) -> MailboxMessage:
        return self._enqueue(content, kind="governance_update")

    def enqueue_coordination_update(self, content: str) -> MailboxMessage:
        """Queue a Host-routed relationship/DAG update at a safe turn boundary."""
        return self._enqueue(content, kind="coordination_update")

    def _enqueue(self, content: str, *, kind: str) -> MailboxMessage:
        text = content.strip()
        if not text:
            raise ValueError("Instruction cannot be empty")
        with self._lock:
            if self._closed:
                raise MailboxClosedError(
                    self._close_reason or "Agent mailbox is closed"
                )
            message = MailboxMessage(
                message_id=str(uuid.uuid4()),
                kind=kind,
                content=text,
                accepted_at=datetime.now().isoformat(),
            )
            self._pending.append(message)
            self._messages[message.message_id] = message
            return message

    def drain(self, *, task_id: str, step: int) -> list[MailboxMessage]:
        with self._lock:
            messages = list(self._pending)
            self._pending.clear()
            for message in messages:
                message.status = "applied"
                message.applied_task_id = task_id
                message.applied_at_step = step
            return messages

    def close_if_empty(self, reason: str) -> bool:
        """Close atomically only when no accepted instruction is pending."""
        with self._lock:
            if self._pending:
                return False
            self._closed = True
            self._close_reason = reason
            return True

    def close(self, reason: str) -> None:
        with self._lock:
            self._closed = True
            self._close_reason = reason
            while self._pending:
                message = self._pending.popleft()
                message.status = "rejected"
                message.rejection_reason = reason

    def reopen_for_recovery(self) -> None:
        """Open the control boundary only after an explicit recovery action."""
        with self._lock:
            self._closed = False
            self._close_reason = ""

    def get(self, message_id: str) -> MailboxMessage | None:
        with self._lock:
            return self._messages.get(message_id)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "closed": self._closed,
                "close_reason": self._close_reason,
                "pending": len(self._pending),
                "messages": [m.to_dict() for m in self._messages.values()],
            }

    def durable_snapshot(self) -> dict:
        """Internal checkpoint including content required for safe replay."""
        with self._lock:
            return {
                "closed": self._closed,
                "close_reason": self._close_reason,
                "pending_ids": [message.message_id for message in self._pending],
                "messages": [m.to_durable_dict() for m in self._messages.values()],
            }
