"""Construction boundary for a valid Agent runtime.

Every execution entry point must create AgentProcess and AgentSession together.
Prompt policy and default capabilities intentionally do not live here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from backend.core.loop.models import AgentProcess, ProcessStatus, RingLevel
from backend.core.loop.mailbox import AgentMailbox
from backend.core.loop.session import AgentSession
from backend.core.loop.tools import ToolRegistry


@dataclass(frozen=True)
class RuntimeSpec:
    role: str
    ring_level: RingLevel
    tool_registry: ToolRegistry
    max_steps: int
    parent_id: str | None = None
    context_snapshot: dict | None = None
    task_description: str = ""
    task_id: str = ""
    workspace_path: str = ""
    provider_id: str = ""
    model_id: str = ""
    actor_kind: str = "worker"
    capability_profile_id: str = "text.only"
    capability_lease: object | None = None
    task_kind: str = "answer"
    required_test_ids: tuple[str, ...] = ()
    task_budget: object | None = None
    runtime_preferences: dict | None = None


class AgentRuntimeFactory:
    """Create a process that satisfies the runtime initialization invariants."""

    @staticmethod
    def create(
        spec: RuntimeSpec,
        *,
        session: AgentSession | None = None,
        process_id: str | None = None,
    ) -> AgentProcess:
        if not spec.role.strip():
            raise ValueError("Agent runtime role is required")
        if spec.max_steps <= 0:
            raise ValueError("Agent runtime max_steps must be greater than zero")
        if spec.tool_registry is None:
            raise ValueError("Agent runtime requires an explicit ToolRegistry")

        runtime_session = session or AgentSession()
        process = AgentProcess(
            process_id=process_id or str(uuid.uuid4()),
            role=spec.role,
            ring_level=spec.ring_level,
            tool_registry=spec.tool_registry,
            max_steps=spec.max_steps,
            status=(
                ProcessStatus.WAITING
                if spec.parent_id is not None
                else ProcessStatus.RUNNING
            ),
            parent_id=spec.parent_id,
            context_snapshot=spec.context_snapshot,
            task_description=spec.task_description,
            active_task_id=spec.task_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            worktree_path=spec.workspace_path,
            provider_id=spec.provider_id,
            model_id=spec.model_id,
            actor_kind=spec.actor_kind,
            capability_profile_id=spec.capability_profile_id,
            capability_lease=spec.capability_lease,
            task_kind=spec.task_kind,
            required_test_ids=list(spec.required_test_ids),
            task_budget=spec.task_budget,
            runtime_preferences=dict(spec.runtime_preferences or {}),
            session=runtime_session,
            mailbox=AgentMailbox(),
        )
        runtime_session.host_ledger.append({
            "event": "runtime_admission",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "process_id": process.process_id,
            "task_id": spec.task_id,
            "actor_kind": spec.actor_kind,
            "capability_profile_id": spec.capability_profile_id,
        })
        AgentRuntimeFactory.assert_valid(process)
        return process

    @staticmethod
    def assert_valid(process: AgentProcess) -> None:
        if process.session is None:
            raise ValueError("Agent runtime invariant violated: session is missing")
        if process.tool_registry is None:
            raise ValueError("Agent runtime invariant violated: ToolRegistry is missing")
        if process.mailbox is None:
            raise ValueError("Agent runtime invariant violated: mailbox is missing")
        if not process.process_id:
            raise ValueError("Agent runtime invariant violated: process_id is missing")
