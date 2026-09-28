"""Deterministic verification tier selection for one Agent task.

The model describes work in a structured task contract. The Host combines that
proposal with facts it owns (actual effects, recovery state and changed scope)
to choose how much verification is necessary. This module never classifies
user prose and never decides whether A should delegate implementation.

Level 0 is the ordinary bounded-work path: receipts plus requested checks are
enough. Level 1 asks for stronger deterministic checks, but still does not
create an Agent. Level 2 is the only tier that requires an independent
Reviewer B.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerificationPlan:
    level: int
    reasons: tuple[str, ...]

    @property
    def requires_independent_review(self) -> bool:
        return self.level >= 2

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "requires_independent_review": self.requires_independent_review,
            "reasons": list(self.reasons),
        }


def _effect_value(receipt: dict) -> str:
    value = receipt.get("effect", "read")
    return str(getattr(value, "value", value) or "read")


def verification_plan(process) -> VerificationPlan:
    """Return the current Host verification plan from structured facts.

    ``review_required`` remains a hard explicit/recovery override for old
    checkpoints. New self-execution leases use the adaptive policy below, so
    writing and running one bounded file does not manufacture a Reviewer B.
    """
    from backend.core.loop.task_contract import get_task_contract

    contract = get_task_contract(process)
    reasons: list[str] = []
    level = 0

    if bool(getattr(process, "review_required", False)):
        level = 2
        reasons.append("explicit_or_recovered_review_requirement")

    complexity = str(contract.get("estimated_complexity") or "bounded")
    if complexity == "moderate" and level < 1:
        level = 1
        reasons.append("moderate_complexity")
    elif complexity == "high":
        level = 2
        reasons.append("high_complexity")

    workstreams = int(contract.get("independent_workstreams", 1) or 1)
    if workstreams > 1:
        level = 2
        reasons.append("multiple_independent_workstreams")

    if contract.get("required_test_ids") or contract.get("required_tool_calls"):
        if level < 1:
            level = 1
            reasons.append("structured_verification_contract")

    workspace_deliverables = [
        item for item in list(contract.get("deliverables") or [])
        if isinstance(item, dict)
        and item.get("required", True)
        and item.get("kind") == "workspace_file"
    ]
    if len(workspace_deliverables) > 1 and level < 1:
        level = 1
        reasons.append("multi_file_delivery")
    if len(workspace_deliverables) > 8:
        level = 2
        reasons.append("broad_delivery_scope")

    receipts = list(getattr(process, "tool_receipts", []) or [])
    committed_effects = {
        _effect_value(item)
        for item in receipts
        if item.get("succeeded") is True and item.get("committed") is True
    }
    # Permission establishes authority; an independent review establishes the
    # correctness of mutations to external state. Anonymous public reads are
    # evidence acquisition and must never manufacture a Reviewer process.
    if "external" in committed_effects:
        level = 2
        reasons.append("external_side_effect")

    recovery = dict(getattr(process, "recovery", None) or {})
    if recovery and str(recovery.get("state") or recovery.get("status") or "") not in {
        "", "resolved", "completed",
    }:
        level = 2
        reasons.append("unresolved_recovery_state")

    if not reasons:
        reasons.append("bounded_single_owner_receipt_verification")
    return VerificationPlan(level=level, reasons=tuple(dict.fromkeys(reasons)))
