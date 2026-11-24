"""One deterministic authority policy for every tool admission boundary.

The model decides *what* operation advances the user's goal.  This module
decides whether that already-scoped operation may run, needs a user decision,
or is denied.  Both the preflight suspender and ToolPipeline call this module;
there must not be a second permission classifier hidden in either layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from backend.core.loop.agent_tool import ApprovalMode, ToolEffect


class PolicyDisposition(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class OperationPolicyDecision:
    disposition: PolicyDisposition
    reason: str
    risk: str
    grant_scope: str
    rule_revision: int = 0
    rule_task: str = ""
    manual_exempt: bool = False

    @property
    def requires_user(self) -> bool:
        return self.disposition == PolicyDisposition.ASK

    @property
    def task_approval(self):
        return f"{self.rule_task}/{self.rule_revision}" if self.risk == "task_rule" else ""


def decide_tool_operation(tool, arguments: dict | None = None, *, process=None) -> OperationPolicyDecision:
    """Classify one concrete call without guessing from keywords.

    Calling a model-visible tool is already the semantic compilation step.  A
    low-risk public read therefore does not need a second confirmation card.
    Sensitive tools retain their declared ASK contract and exact/task-scoped
    grants continue to be enforced by the permission broker.
    """
    approval = ApprovalMode(getattr(tool, "approval", ApprovalMode.ALLOW))
    effect = ToolEffect(getattr(tool, "effect", ToolEffect.READ))
    per_invocation = bool(getattr(tool, "approval_per_invocation", False))

    if approval == ApprovalMode.DENY:
        return OperationPolicyDecision(
            PolicyDisposition.DENY, "tool contract denies this operation",
            "forbidden", "none",
        )
    from .task_execution_policy import matches
    state, actions = matches(process, tool.name, arguments or {})
    rule = {"rule_revision": state["revision"], "rule_task": state["task_id"],
            "manual_exempt": "manual_exempt" in actions} if state else {}
    if "deny" in actions:
        return OperationPolicyDecision(PolicyDisposition.DENY,
            "This operation is blocked by the current task execution policy.", "forbidden", "none", **rule)
    if "ask" in actions:
        return OperationPolicyDecision(PolicyDisposition.ASK,
            "The current task execution policy requires approval for this call.", "task_rule", "exact_invocation", **rule)
    if effect == ToolEffect.EXTERNAL_READ and approval == ApprovalMode.ALLOW:
        return OperationPolicyDecision(
            PolicyDisposition.ALLOW,
            "anonymous public-information retrieval is a low-risk read",
            "low", "task", **rule,
        )
    if approval == ApprovalMode.ASK:
        return OperationPolicyDecision(
            PolicyDisposition.ASK,
            "tool contract requires explicit authority",
            "high" if per_invocation else "sensitive",
            "exact_invocation" if per_invocation else "task_or_invocation", **rule,
        )
    return OperationPolicyDecision(
        PolicyDisposition.ALLOW, "tool contract allows this operation",
        "low" if effect == ToolEffect.READ else "bounded", "task", **rule,
    )


def is_external_mutation(effect: ToolEffect | str) -> bool:
    """Whether correctness needs independent review beyond read evidence."""
    return ToolEffect(effect) == ToolEffect.EXTERNAL


def is_observation_effect(effect: ToolEffect | str) -> bool:
    """Whether an operation only observes state, locally or on the public web."""
    value = str(getattr(effect, "value", effect) or "read")
    # Pre-schema checkpoints used values such as ``write`` and ``workspace``.
    # Unknown values fail closed as mutations, never as observations.
    return value in {ToolEffect.READ.value, ToolEffect.EXTERNAL_READ.value}


def is_effectful_mutation(effect: ToolEffect | str) -> bool:
    """Whether an operation changes workspace, process, or external state."""
    return not is_observation_effect(effect)
