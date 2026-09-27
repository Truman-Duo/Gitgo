"""Deterministic decision shortcuts and structured user escalation.

The Host computes facts that do not require semantic judgement.  Agents only
interpret the remaining ambiguity, and a supervisor can pause for a material
user choice through a validated, resumable decision request.
"""

from __future__ import annotations

import ast
import math
import operator
import uuid
from datetime import datetime
from typing import Any

from backend.core.loop.completion_protocol import HostCompletionEvidence


DECISION_KINDS = {
    "clarification", "preference", "direction", "verification",
    "choice", "permission", "recovery", "checkpoint",
}
STATEFUL_DECISION_KINDS = {
    "preference", "direction", "verification", "checkpoint",
}


_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _decision_position(process) -> dict:
    ledger = list(getattr(process.session, "host_ledger", []) or [])
    sequence = 1 + max((
        int(item.get("decision_sequence", 0) or 0)
        for item in ledger if item.get("event") == "user_decision_requested"
    ), default=0)
    return {
        "decision_sequence": sequence,
        "context_epoch": int(getattr(process.session, "context_epoch", 0) or 0),
        "message_sequence": len(process.session.messages),
    }


def safe_calculate(expression: str) -> int | float | bool:
    """Evaluate a bounded arithmetic/comparison expression without ``eval``."""
    expression = str(expression).strip()
    if not expression or len(expression) > 500:
        raise ValueError("expression must contain 1..500 characters")
    tree = ast.parse(expression, mode="eval")

    def visit(node: ast.AST, depth: int = 0) -> int | float | bool:
        if depth > 32:
            raise ValueError("expression is too deeply nested")
        if isinstance(node, ast.Expression):
            return visit(node.body, depth + 1)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return node.value
            if isinstance(node.value, (int, float)) and math.isfinite(float(node.value)):
                if abs(float(node.value)) > 1e100:
                    raise ValueError("numeric literal is too large")
                return node.value
            raise ValueError("only finite numeric constants are allowed")
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _bounded(_UNARY[type(node.op)](visit(node.operand, depth + 1)))
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left = visit(node.left, depth + 1)
            right = visit(node.right, depth + 1)
            if isinstance(node.op, ast.Pow) and abs(float(right)) > 100:
                raise ValueError("power exponent exceeds the safety bound")
            return _bounded(_BINARY[type(node.op)](left, right))
        if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators):
            left = visit(node.left, depth + 1)
            for operation, comparator in zip(node.ops, node.comparators):
                function = _COMPARE.get(type(operation))
                if function is None:
                    raise ValueError("unsupported comparison")
                right = visit(comparator, depth + 1)
                if not function(left, right):
                    return False
                left = right
            return True
        raise ValueError(f"unsupported expression element: {type(node).__name__}")

    return visit(tree)


def _bounded(value: Any) -> int | float | bool:
    if isinstance(value, bool):
        return value
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("calculation produced a non-finite result")
    if abs(float(value)) > 1e100:
        raise ValueError("calculation result exceeds the safety bound")
    return value


def collect_decision_evidence(process, focus: str = "all") -> dict:
    """Return compact Host-owned facts so the LLM does not manually aggregate them."""
    allowed = {
        "all", "completion", "gates", "children", "tests",
        "governance", "budget", "capabilities",
    }
    if focus not in allowed:
        raise ValueError(f"focus must be one of: {', '.join(sorted(allowed))}")
    result: dict[str, Any] = {
        "focus": focus,
        "process_id": process.process_id,
        "task_kind": process.task_kind,
        "steps": {"used": process.steps_used, "maximum": process.max_steps},
    }
    if focus in {"all", "completion", "tests"}:
        evidence = HostCompletionEvidence.collect(process)
        result["completion"] = evidence.to_dict()
    if focus in {"all", "completion", "gates"}:
        from backend.core.loop.completion_protocol import HostCompletionEvaluator
        context, _version = process.read_context_snapshot()
        result["outstanding_gates"] = HostCompletionEvaluator.outstanding_gates(
            process, list(context.get("signals") or []),
        )
    if focus in {"all", "children"}:
        manager = getattr(process, "_manager", None)
        child_ids, contracts, reviews = process.coordination_snapshot()
        result["children"] = [
            {
                "process_id": child_id,
                "status": (
                    manager.get(child_id).status.value
                    if manager is not None and manager.get(child_id) is not None
                    else "missing"
                ),
                "contract": contracts.get(child_id, {}),
                "review": reviews.get(child_id, {}),
            }
            for child_id in child_ids
        ]
    if focus in {"all", "governance"}:
        context, version = process.read_context_snapshot()
        from backend.core.loop.governance_projection import GovernanceProjection
        result["governance"] = {
            "context_version": version,
            "snapshot_digest": context.get("governance_digest", ""),
            "context_reference_error": context.get("governance_context_error"),
            "evidence_candidates": list(context.get("evidence_candidates") or []),
            "evidence_graph": dict(context.get("evidence_graph") or {}),
            "active_signals": [
                {**item.to_dict(), "resolution": getattr(process, "governance_resolutions", {}).get(item.signal_id)}
                for item in GovernanceProjection.normalize(context.get("signals") or [])
            ],
        }
    if focus in {"all", "capabilities"}:
        from backend.core.loop.capabilities import CapabilityProfiles
        result["capabilities"] = {
            "current_profile_id": process.capability_profile_id,
            "worker_profiles": CapabilityProfiles.describe(actor_kind="worker"),
            "delegated_task_kinds": ["answer", "plan", "action"],
            "authority": "Host registry; selecting a profile does not grant A self-execution",
        }
    if focus in {"all", "budget"}:
        result["budget"] = (
            process.task_budget.snapshot() if process.task_budget is not None else None
        )
        result["budget_card"] = (
            process.task_budget.decision_card(process.process_id)
            if process.task_budget is not None else None
        )
    return result


def create_user_decision(process, args: dict) -> dict:
    """Validate and publish one durable Agent-to-user decision request."""
    kind = str(args.get("kind") or "choice").strip()
    if kind not in DECISION_KINDS:
        raise ValueError("kind must be one of: " + ", ".join(sorted(DECISION_KINDS)))
    question = str(args.get("question", "")).strip()
    why = str(args.get("why_user_must_decide", "")).strip()
    if not question or not why:
        raise ValueError("question and why_user_must_decide are required")
    raw_options = args.get("options", []) or []
    if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= 3:
        raise ValueError(
            "request_user_decision requires 2..3 business options; use the Host "
            "free-form Discuss/Amend affordance instead of adding a defer option"
        )
    state_topic = str(args.get("state_topic") or "").strip()
    if kind in STATEFUL_DECISION_KINDS and not state_topic:
        raise ValueError(
            f"state_topic is required for {kind}; provide a stable semantic topic "
            "so the newest confirmed answer supersedes stale state"
        )
    options = []
    labels: set[str] = set()
    recommended_count = 0
    required = (
        "label", "principle", "immediate_effect", "downstream_effect",
        "risks", "reversibility",
    )
    for raw in raw_options:
        if not isinstance(raw, dict):
            raise ValueError("every decision option must be an object")
        option = {name: str(raw.get(name, "")).strip() for name in required}
        missing = [name for name, value in option.items() if not value]
        if missing:
            raise ValueError("decision option missing: " + ", ".join(missing))
        if option["label"] in labels:
            raise ValueError("decision option labels must be unique")
        labels.add(option["label"])
        option["recommended"] = bool(raw.get("recommended", False))
        if raw.get("action"):
            option["action"] = str(raw.get("action"))
        recommended_count += int(option["recommended"])
        options.append(option)
    if recommended_count > 1:
        raise ValueError("at most one option may be recommended")
    request = {
        "decision_id": str(uuid.uuid4()),
        "kind": kind,
        "state_topic": state_topic,
        "supersedes_decision_id": str(args.get("supersedes_decision_id") or "").strip(),
        "question": question,
        "why_user_must_decide": why,
        "options": options,
        "allow_free_form": bool(args.get("allow_free_form", True)),
        "created_at": datetime.now().isoformat(),
        **_decision_position(process),
        "task_id": process.active_task_id or process.process_id,
        "process_id": process.process_id,
    }
    from backend.core.loop.question_broker import route_question
    request = route_question(process, request)
    if isinstance(args.get("permission_request"), dict):
        request["permission_request"] = dict(args["permission_request"])
    with process._coordination_lock:
        if process.pending_decision is not None:
            raise RuntimeError("a user decision is already pending")
        process.pending_decision = request
    if process.task_budget is not None:
        process.task_budget.pause_for_user()
    process.session.host_ledger.append({"event": "user_decision_requested", **request})
    return {"status": "awaiting_user", **request}


def confirmed_user_state(ledger: list[dict]) -> list[dict]:
    """Project the newest user-confirmed value for each semantic topic."""
    requests = {
        str(item.get("decision_id")): item
        for item in ledger
        if item.get("event") == "user_decision_requested" and item.get("decision_id")
    }
    latest: dict[str, dict] = {}
    for event in ledger:
        if event.get("event") != "user_decision_received":
            continue
        request = requests.get(str(event.get("decision_id") or ""), {})
        kind = str(request.get("kind") or "choice")
        if kind in {"permission", "recovery"}:
            continue
        topic = str(request.get("state_topic") or "").strip()
        if not topic:
            continue
        latest[topic] = {
            "topic": topic,
            "kind": kind,
            "value": str(event.get("selected_label") or event.get("answer") or ""),
            "conditions": str(event.get("answer") or ""),
            "decision_id": str(event.get("decision_id") or ""),
            "confirmed_at": str(event.get("created_at") or ""),
            "supersedes_decision_id": str(request.get("supersedes_decision_id") or ""),
        }
    return sorted(latest.values(), key=lambda item: item["topic"])


def build_context_force_compaction_decision(process_id: str, task_id: str) -> dict:
    """One decision schema for live-loop and parked-session compaction."""
    request = {
        "decision_id": str(uuid.uuid4()),
        "kind": "context_force_compaction",
        "question": "Ordinary compaction failed three times. Force a lossy compaction?",
        "why_user_must_decide": (
            "The Host can preserve the full prior epoch in the local audit archive, "
            "but must delete part of the provider-visible history to continue."
        ),
        "options": [
            {
                "label": "Force compact",
                "principle": "Keep immutable policy/task prefixes and a bounded host checkpoint.",
                "immediate_effect": "Delete older provider-visible history and retry in a new epoch.",
                "downstream_effect": "The Agent continues with less conversational detail; the audit archive remains intact.",
                "risks": "Fine-grained details not captured by the checkpoint may need to be reopened from durable records.",
                "reversibility": "The provider context cannot be expanded in place, but the archived epoch remains inspectable.",
                "recommended": True,
                "action": "force_compact",
            },
            {
                "label": "Stop safely",
                "principle": "Do not discard model-visible history without approval.",
                "immediate_effect": "Stop the current task before another provider request.",
                "downstream_effect": "The task can be resumed after correcting the configured limit or reviewing the archive.",
                "risks": "Current work remains incomplete.",
                "reversibility": "A later task can continue from durable state.",
                "recommended": False,
                "action": "stop",
            },
        ],
        "allow_free_form": False,
        "created_at": datetime.now().isoformat(),
        "task_id": task_id or process_id,
        "process_id": process_id,
        "error_catalog_id": "GITGO-E5203",
    }
    return request


def create_context_force_compaction_decision(process) -> dict:
    """Publish the deterministic decision reached after three failed compactions."""
    request = build_context_force_compaction_decision(
        process.process_id, process.active_task_id or process.process_id,
    )
    request.update(_decision_position(process))
    with process._coordination_lock:
        if process.pending_decision is None:
            process.pending_decision = request
        else:
            request = process.pending_decision
    if process.task_budget is not None:
        process.task_budget.pause_for_user()
    process.session.host_ledger.append({"event": "user_decision_requested", **request})
    return {"status": "awaiting_user", **request}


def create_completion_exception_decision(process, gate_status: dict) -> dict:
    """Escalate a stable, repeatedly-unsatisfied completion checklist.

    Facts are never rewritten as successful. The user may accept only gates
    explicitly marked degradable, producing a distinct degraded outcome.
    """
    gates = [dict(item) for item in list(gate_status.get("gates") or [])]
    all_degradable = bool(gates) and all(
        item.get("degradable_by_explicit_user_decision") is True for item in gates
    )
    middle = {
        "label": "Accept partial result" if all_degradable else "Change scope",
        "principle": (
            "Preserve the failed facts and accept an explicitly degraded delivery."
            if all_degradable else
            "Change the requested scope instead of pretending a required fact exists."
        ),
        "immediate_effect": (
            "Finish this task as degraded with every unresolved gate recorded."
            if all_degradable else
            "Return the task to the main process with your amended scope."
        ),
        "downstream_effect": (
            "History and reports distinguish it from a verified completion."
            if all_degradable else
            "The task contract must be revised before another completion attempt."
        ),
        "risks": "Some requested verification or deliverables remain incomplete.",
        "reversibility": "A later task can satisfy the preserved gates.",
        "recommended": False,
        "action": "accept_partial" if all_degradable else "amend_scope",
    }
    request = {
        "decision_id": str(uuid.uuid4()),
        "kind": "completion_exception",
        "question": "The task still cannot pass the same completion checks. How should Gitgo proceed?",
        "why_user_must_decide": (
            "The Host supplied the exact checklist and one bounded recovery attempt "
            "did not change it. Continuing automatically would waste model work."
        ),
        "options": [
            {
                "label": "Continue and fix",
                "principle": "Keep the current scope and satisfy the Host facts.",
                "immediate_effect": "Resume the same process with the checklist attached.",
                "downstream_effect": "Completion remains fully verified if all gates pass.",
                "risks": "This may consume more time and provider budget.",
                "reversibility": "You can stop or accept a partial result later.",
                "recommended": True,
                "action": "continue",
            },
            middle,
            {
                "label": "Stop safely",
                "principle": "Do not spend more work on an unsatisfied contract.",
                "immediate_effect": "Resume only to produce a truthful incomplete-task report.",
                "downstream_effect": "Durable evidence remains available for a later continuation.",
                "risks": "The requested task is not delivered in this run.",
                "reversibility": "A future task can continue from the saved state.",
                "recommended": False,
                "action": "stop",
            },
        ],
        "allow_free_form": True,
        "created_at": datetime.now().isoformat(),
        "task_id": process.active_task_id or process.process_id,
        "process_id": process.process_id,
        "error_catalog_id": "GITGO-E6105",
        "completion_gate_ids": [str(item.get("gate_id") or "") for item in gates],
        "completion_gates": gates,
    }
    request.update(_decision_position(process))
    with process._coordination_lock:
        if process.pending_decision is None:
            process.pending_decision = request
        else:
            request = process.pending_decision
    if process.task_budget is not None:
        process.task_budget.pause_for_user()
    process.session.host_ledger.append({"event": "user_decision_requested", **request})
    return {"status": "awaiting_user", **request}


def format_decision_request(request: dict) -> str:
    lines = [
        str(request.get("question", "")),
        "",
        "为什么需要你决定：" + str(request.get("why_user_must_decide", "")),
        "",
    ]
    for index, option in enumerate(request.get("options", []) or [], start=1):
        marker = "（建议）" if option.get("recommended") else ""
        lines.extend([
            f"{index}. {option.get('label', '')}{marker}",
            f"   原理：{option.get('principle', '')}",
            f"   立即效果：{option.get('immediate_effect', '')}",
            f"   后续影响：{option.get('downstream_effect', '')}",
            f"   风险：{option.get('risks', '')}",
            f"   可逆性：{option.get('reversibility', '')}",
        ])
    if request.get("allow_free_form", True):
        lines.extend(["", "你也可以直接说明自己的选择或补充条件。"])
    return "\n".join(lines).strip()
