"""Server-owned capability profiles and explicit self-execution leases.

Callers select a profile id; they never manufacture an effective tool set.
An A-level supervisor may expand its capabilities only by making an explicit
``request_self_execute`` request.  The host validates that request and issues a
task-scoped lease.  The lease is evidence, not a prompt convention.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ActorKind(str, Enum):
    SUPERVISOR = "supervisor"
    WORKER = "worker"
    REVIEWER = "reviewer"


@dataclass(frozen=True)
class CapabilityProfile:
    profile_id: str
    actor_kind: ActorKind
    tool_names: tuple[str, ...]
    description: str
    allows_self_execution: bool = False


@dataclass(frozen=True)
class CapabilityLease:
    lease_id: str
    task_id: str
    requested_by: str
    profile_id: str
    reason: str
    intended_actions: tuple[str, ...]
    issued_at: str

    @classmethod
    def issue(
        cls,
        *,
        task_id: str,
        requested_by: str,
        profile_id: str,
        reason: str,
        intended_actions: list[str] | tuple[str, ...],
    ) -> "CapabilityLease":
        if not task_id:
            raise ValueError("Self-execution lease requires a task_id")
        if not requested_by:
            raise ValueError("Self-execution lease requires a requester")
        if not reason.strip():
            raise ValueError("Self-execution request requires a reason")
        actions = tuple(str(a).strip() for a in intended_actions if str(a).strip())
        if not actions:
            raise ValueError("Self-execution request requires intended_actions")
        return cls(
            lease_id=str(uuid.uuid4()),
            task_id=task_id,
            requested_by=requested_by,
            profile_id=profile_id,
            reason=reason.strip(),
            intended_actions=actions,
            issued_at=datetime.now().isoformat(),
        )


class CapabilityProfiles:
    """Canonical server-side profile registry."""

    _CONTEXT = (
        "context_open", "context_search", "dependency_query", "artifact_read",
        "tool_result_open",
    )
    _USER_BRIDGE = (
        "request_user_decision", "request_permission", "configure_capability",
        "engineering_workflow",
    )
    _WORKER_COORDINATION = (
        "publish_interface_update", "escalate_to_supervisor",
    )
    _TOOL_AUTHORING = ("define_tool", "author_tool")
    _OBSERVE = (
        "scan", "status", "recall_grep", "recall_semantic", "recall_rag",
        "assemble_context", "assemble_return_context", "decompose_task",
        "contract_detect_drift", "contract_get_impact",
        "contract_get_changed_symbols", "lesson_search", "lesson_list",
        "privacy_scan", "read_file", "list_files", "search_text", "document_open",
        "web_search", "web_fetch",
        "calculate", "decision_evidence", "capability_status",
        "acknowledge_governance_signal",
        "code_dossier",
    ) + _CONTEXT
    _OPERATE = _OBSERVE + (
        "formalize", "lesson_discard", "lesson_verify", "lesson_harvest",
        "memory_snapshot", "memory_restore", "lesson_promote", "run_test",
    )
    _DEVELOPMENT = _OBSERVE + (
        "edit_file", "write_file", "delete_file", "document_create", "apply_patch", "exec_command", "shell_script", "run_test",
        "dependency_feedback", "rebuild_dependency_graph",
    )
    _REVIEW = _OBSERVE + (
        "run_test", "dependency_feedback", "rebuild_dependency_graph",
    )
    _SUPERVISOR_COORDINATION = (
        "declare_task_contract", "delegate_task", "delegate_task_dag",
        "list_agents", "wait_agents", "send_feedback",
        "cancel_agent", "request_review", "review_child_outcome",
        "read_child_artifact",
        "prepare_task_bundle", "delegate_task_bundle",
        "list_coordination_events", "resolve_coordination_event",
        "promote_agent_changes", "complete_supervision",
    )

    _PROFILES = {
        "supervisor.chat": CapabilityProfile(
            profile_id="supervisor.chat",
            actor_kind=ActorKind.SUPERVISOR,
            tool_names=(),
            description=(
                "A-level ordinary conversation with no project tools or effectful "
                "capability surface."
            ),
        ),
        "supervisor.answer": CapabilityProfile(
            profile_id="supervisor.answer",
            actor_kind=ActorKind.SUPERVISOR,
            tool_names=_OBSERVE,
            description=(
                "A-level conversational supervisor: answer directly and use only "
                "read-only inspection tools when the question genuinely requires them."
            ),
        ),
        "supervisor.control": CapabilityProfile(
            profile_id="supervisor.control",
            actor_kind=ActorKind.SUPERVISOR,
            tool_names=_OBSERVE + _USER_BRIDGE + _TOOL_AUTHORING + _SUPERVISOR_COORDINATION,
            description=(
                "A-level project supervisor: inspect, plan, decompose and review; "
                "effectful self-execution requires an explicit task-scoped lease."
            ),
        ),
        "governance.observe": CapabilityProfile(
            profile_id="governance.observe",
            actor_kind=ActorKind.WORKER,
            tool_names=_OBSERVE + _USER_BRIDGE + _WORKER_COORDINATION,
            description="Read-only governance and project inspection worker.",
        ),
        "governance.operate": CapabilityProfile(
            profile_id="governance.operate",
            actor_kind=ActorKind.WORKER,
            tool_names=_OPERATE + _USER_BRIDGE + _WORKER_COORDINATION,
            description="Governance worker with explicit state-changing operations.",
            allows_self_execution=True,
        ),
        "development.workspace": CapabilityProfile(
            profile_id="development.workspace",
            actor_kind=ActorKind.WORKER,
            tool_names=_DEVELOPMENT + _USER_BRIDGE + _WORKER_COORDINATION + _TOOL_AUTHORING,
            description=(
                "Workspace-confined development worker with cancellable file, search, "
                "patch, command, test and dependency-feedback tools."
            ),
            allows_self_execution=True,
        ),
        "review.independent": CapabilityProfile(
            profile_id="review.independent",
            actor_kind=ActorKind.REVIEWER,
            tool_names=_REVIEW + _USER_BRIDGE + _WORKER_COORDINATION,
            description="Independent reviewer; cannot perform effectful changes.",
        ),
        "text.only": CapabilityProfile(
            profile_id="text.only",
            actor_kind=ActorKind.WORKER,
            tool_names=_USER_BRIDGE + _WORKER_COORDINATION,
            description="No tools; answer-only execution.",
        ),
    }

    @classmethod
    def ids(cls, *, actor_kind: ActorKind | str | None = None) -> tuple[str, ...]:
        """Return the stable, server-owned profile ids visible to contracts."""
        if actor_kind is None:
            return tuple(cls._PROFILES)
        expected = ActorKind(actor_kind)
        return tuple(
            profile_id for profile_id, profile in cls._PROFILES.items()
            if profile.actor_kind == expected
        )

    @classmethod
    def describe(cls, *, actor_kind: ActorKind | str | None = None) -> list[dict]:
        """Model/diagnostic projection without exposing mutable authority."""
        return [
            {
                "profile_id": profile_id,
                "actor_kind": profile.actor_kind.value,
                "description": profile.description,
                "allows_effectful_execution": profile.allows_self_execution,
            }
            for profile_id in cls.ids(actor_kind=actor_kind)
            for profile in (cls._PROFILES[profile_id],)
        ]

    @classmethod
    def get(cls, profile_id: str) -> CapabilityProfile:
        try:
            return cls._PROFILES[profile_id]
        except KeyError as exc:
            raise ValueError(f"Unknown capability profile: {profile_id}") from exc

    @classmethod
    def pre_contract_tools(cls) -> tuple[str, ...]:
        """Safe evidence/research surface plus explicit admission controls.

        Read-only capabilities do not grant workspace or external mutation
        authority and therefore need not disappear while A decides whether the
        request requires a managed workflow.  Keeping this list beside the
        profiles prevents the admission filter from becoming a second policy
        registry.
        """
        return tuple(dict.fromkeys(
            cls._OBSERVE
            + ("request_user_decision", "declare_task_contract", "configure_capability", "engineering_workflow")
        ))

    @classmethod
    def worker_error(cls, profile_id: str) -> dict | None:
        """Same discoverable error for direct, DAG and bundle admission."""
        from backend.core.errors import error_payload
        profile = cls._PROFILES.get(profile_id)
        if profile is not None and profile.actor_kind == ActorKind.WORKER:
            return None
        return error_payload(
            "CAPABILITY_PROFILE_UNKNOWN" if profile is None else "CAPABILITY_PROFILE_NOT_WORKER",
            details={"attempted_value": profile_id, "valid_worker_profiles": cls.describe(actor_kind="worker"),
                     "recommended_profile": "development.workspace"},
            next_actions=[{"action": "retry", "patch": {"capability_profile_id": "development.workspace"},
                           "reason": "Choose a registered worker profile matching the intended work."}],
        )

    @classmethod
    def resolve_tools(
        cls,
        profile_id: str,
        *,
        lease: CapabilityLease | None = None,
    ) -> list[str]:
        profile = cls.get(profile_id)
        tools = set(profile.tool_names)
        if lease is not None:
            leased_profile = cls.get(lease.profile_id)
            if not leased_profile.allows_self_execution:
                raise ValueError(
                    f"Profile {lease.profile_id} cannot be used for self execution"
                )
            leased_tools = set(leased_profile.tool_names)
            # A self-execution lease borrows the effectful workspace surface;
            # it does not turn the supervisor into a B.  Worker coordination
            # tools are actor-specific (their implementations are deliberately
            # mounted only for worker/reviewer processes), so advertising them
            # to A makes capability validation fail immediately after a valid
            # lease is granted.
            if profile.actor_kind == ActorKind.SUPERVISOR:
                leased_tools.difference_update(cls._WORKER_COORDINATION)
            tools.update(leased_tools)
        return sorted(tools)

    @classmethod
    def issue_self_execute_lease(
        cls,
        *,
        actor_kind: str,
        task_id: str,
        requested_by: str,
        profile_id: str,
        reason: str,
        intended_actions: list[str],
    ) -> CapabilityLease:
        if actor_kind != ActorKind.SUPERVISOR.value:
            raise PermissionError("Only an A-level supervisor can request self execution")
        profile = cls.get(profile_id)
        if not profile.allows_self_execution:
            raise PermissionError(
                f"Profile {profile_id} is not eligible for self execution"
            )
        return CapabilityLease.issue(
            task_id=task_id,
            requested_by=requested_by,
            profile_id=profile_id,
            reason=reason,
            intended_actions=intended_actions,
        )
