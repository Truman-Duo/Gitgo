"""LLM-proposed, Host-validated task intent contracts.

The model owns semantic interpretation of user language.  The Host never uses
keywords or regular expressions to infer intent; it validates paths, enums and
evidence, then persists the proposal as task-scoped control-plane data.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


EXECUTION_MODES = (
    "answer", "delegate", "self_execute", "either", "plan", "review",
)
DELIVERABLE_KINDS = ("workspace_file", "response", "other")
ROUTING_TRANSITIONS = (
    "initial", "keep_supervisor", "delegate_initial", "handoff_to_worker",
    "continue_owner", "parallelize",
)

# Completion calls are Host protocol, never evidence that the user's requested
# work happened.  Models may propose required domain tool receipts, but binding
# a contract to its own closing handshake creates an impossible circular gate
# when the task later changes from answer/supervisor to action/review.
COMPLETION_PROTOCOL_TOOLS = frozenset({
    "complete_task", "complete_supervision", "complete_review",
})


def _relative_workspace_path(workspace: Path, raw: str) -> str:
    value = str(raw or "").strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    if not value:
        raise ValueError("workspace_file deliverable requires path")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        relative = candidate.resolve(strict=False).relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"deliverable path escapes workspace: {raw}") from exc
    return str(relative).replace("\\", "/")


def validate_contract_proposal(args: dict, workspace_path: str) -> dict[str, Any]:
    goal = str(args.get("goal") or "").strip()
    if not goal:
        raise ValueError("goal is required")
    mode = str(args.get("execution_mode") or "either").strip().lower()
    if mode not in EXECUTION_MODES:
        raise ValueError("execution_mode must be one of: " + ", ".join(EXECUTION_MODES))
    delegation_required = bool(args.get("delegation_required", False))
    if delegation_required and mode not in {"delegate", "either"}:
        raise ValueError(
            "delegation_required=true requires execution_mode delegate or either"
        )
    minimum = int(args.get("minimum_delegated_outcomes", 0) or 0)
    if delegation_required and minimum < 1:
        minimum = 1
    if minimum < 0 or minimum > 8:
        raise ValueError("minimum_delegated_outcomes must be between 0 and 8")

    workspace = Path(workspace_path).resolve()
    deliverables = []
    for raw in list(args.get("deliverables") or []):
        item = dict(raw or {})
        kind = str(item.get("kind") or "other").strip().lower()
        if kind not in DELIVERABLE_KINDS:
            raise ValueError("deliverable kind must be workspace_file, response, or other")
        path = str(item.get("path") or "").strip()
        if kind == "workspace_file":
            path = _relative_workspace_path(workspace, path)
        deliverables.append({
            "kind": kind,
            "path": path,
            "description": str(item.get("description") or "").strip(),
            "required": bool(item.get("required", True)),
            "allow_inline_substitution": bool(
                item.get("allow_inline_substitution", False)
            ),
        })

    complexity = str(args.get("estimated_complexity") or "bounded").strip().lower()
    if complexity not in {"bounded", "moderate", "high"}:
        raise ValueError("estimated_complexity must be bounded, moderate, or high")
    workstreams = int(args.get("independent_workstreams", 1) or 1)
    if not 1 <= workstreams <= 8:
        raise ValueError("independent_workstreams must be between 1 and 8")
    routing_transition = str(args.get("routing_transition") or "initial").strip().lower()
    if routing_transition not in ROUTING_TRANSITIONS:
        raise ValueError(
            "routing_transition must be one of: " + ", ".join(ROUTING_TRANSITIONS)
        )

    # Natural-language intent is compiled by the model, not by Host regexes.
    # Keep an explicit user request for a subprocess as a first-class semantic
    # fact so the deterministic budget policy cannot normalize it away merely
    # because the underlying delivery is small.  The excerpt is audit evidence,
    # not a second Host-side classifier.
    user_requested_delegation = bool(args.get("user_requested_delegation", False))
    user_request_evidence = str(args.get("user_request_evidence") or "").strip()
    if user_requested_delegation:
        if not user_request_evidence:
            raise ValueError(
                "user_requested_delegation=true requires user_request_evidence"
            )
        if mode not in {"delegate", "either"}:
            raise ValueError(
                "user_requested_delegation=true requires execution_mode delegate or either"
            )
        delegation_required = True
        minimum = max(1, minimum)

    required_tool_calls = []
    ignored_protocol_requirements = []
    for raw in list(args.get("required_tool_calls") or []):
        item = dict(raw or {})
        tool_name = str(item.get("tool_name") or "").strip()
        if not tool_name:
            raise ValueError("required_tool_calls.tool_name is required")
        if tool_name in COMPLETION_PROTOCOL_TOOLS:
            ignored_protocol_requirements.append(tool_name)
            continue
        minimum_calls = int(item.get("min_calls", 1) or 0)
        maximum_raw = item.get("max_calls")
        maximum_calls = int(maximum_raw) if maximum_raw is not None else None
        if not 0 <= minimum_calls <= 100:
            raise ValueError("required_tool_calls.min_calls must be between 0 and 100")
        if maximum_calls is not None and not minimum_calls <= maximum_calls <= 100:
            raise ValueError(
                "required_tool_calls.max_calls must be between min_calls and 100"
            )
        required_tool_calls.append({
            "tool_name": tool_name,
            "min_calls": minimum_calls,
            **({"max_calls": maximum_calls} if maximum_calls is not None else {}),
            "include_composite_steps": bool(item.get("include_composite_steps", False)),
        })

    return {
        "schema_version": 1,
        "authority": "llm_proposed_host_validated",
        "goal": goal,
        "execution_mode": mode,
        "delegation_required": delegation_required,
        "minimum_delegated_outcomes": minimum,
        "adopt_process_ids": list(dict.fromkeys(str(item) for item in (args.get("adopt_process_ids") or []))),
        "deliverables": deliverables,
        "acceptance_criteria": [
            str(item).strip() for item in list(args.get("acceptance_criteria") or [])
            if str(item).strip()
        ],
        "uncertainties": [
            str(item).strip() for item in list(args.get("uncertainties") or [])
            if str(item).strip()
        ],
        "requires_user_decision": bool(args.get("requires_user_decision", False)),
        "estimated_complexity": complexity,
        "independent_workstreams": workstreams,
        "delegation_rationale": str(args.get("delegation_rationale") or "").strip(),
        "user_requested_delegation": user_requested_delegation,
        "user_request_evidence": user_request_evidence,
        "routing_transition": routing_transition,
        "handoff_summary": str(args.get("handoff_summary") or "").strip(),
        "required_tool_calls": required_tool_calls,
        "ignored_protocol_requirements": list(dict.fromkeys(
            ignored_protocol_requirements
        )),
    }


def routing_advice(contract: dict) -> dict:
    """Return a deterministic recommendation from A's semantic contract.

    The Host does not classify user prose.  It only checks the structured facts
    A already supplied, which keeps simple work cheap while allowing a complex
    single-file task to justify delegation explicitly.
    """
    requirements = dict(contract.get("host_requirements") or {})
    transition = str(contract.get("routing_transition") or "initial")
    if requirements.get("manual_B_creation"):
        return {"recommended": "delegate", "binding": True, "reason": "manual_B_creation"}
    if contract.get("user_requested_delegation"):
        return {
            "recommended": "delegate",
            "binding": True,
            "reason": "semantic_user_request",
        }
    if transition == "keep_supervisor":
        return {"recommended": "self_execute", "binding": False, "reason": transition}
    if transition == "continue_owner":
        return {"recommended": "continue_worker", "binding": False, "reason": transition}
    if transition in {"handoff_to_worker", "parallelize"}:
        return {"recommended": "delegate", "binding": False, "reason": transition}
    workstreams = int(contract.get("independent_workstreams", 1) or 1)
    complexity = str(contract.get("estimated_complexity") or "bounded")
    rationale = str(contract.get("delegation_rationale") or "").strip()
    if workstreams > 1:
        return {"recommended": "delegate", "binding": False, "reason": "independent_workstreams"}
    if complexity in {"moderate", "high"} and rationale:
        return {"recommended": "delegate", "binding": False, "reason": "complexity_justified"}
    return {
        "recommended": "self_execute",
        "binding": False,
        "reason": "bounded_single_owner",
        "override_requires": ["estimated_complexity", "delegation_rationale"],
    }


def compile_routing_proposal(
    previous: dict,
    proposal: dict,
    *,
    workflow_started: bool,
) -> tuple[dict, dict]:
    """Compile semantic facts into a route before the contract is published.

    A supplies the semantic facts; the Host performs deterministic bookkeeping.
    A contradictory request to delegate bounded single-owner work is normalized
    to self-execution instead of forcing an error/retry round. This grants no
    authority: A must still explicitly request a task-scoped execution lease.
    """
    normalized = dict(proposal)
    requirements = dict(previous.get("host_requirements") or {})
    candidate = {**previous, **normalized, "host_requirements": requirements}
    transition = str(normalized.get("routing_transition") or "initial")
    workstreams = int(normalized.get("independent_workstreams", 1) or 1)
    adopted = [
        str(item) for item in (normalized.get("adopt_process_ids") or [])
        if str(item)
    ]

    if transition == "parallelize" and workstreams <= 1:
        raise ValueError("parallelize requires at least two independent_workstreams")
    if transition == "continue_owner" and not adopted:
        raise ValueError("continue_owner requires adopt_process_ids for the existing owner")
    if transition == "handoff_to_worker" and not workflow_started:
        raise ValueError(
            "handoff_to_worker requires Host-observed work in the current workflow; "
            "use self_execute for bounded initial work or provide a truthful "
            "moderate/high initial complexity rationale"
        )

    advice = routing_advice(candidate)
    requested_mode = str(normalized.get("execution_mode") or "either")
    asked_to_delegate = bool(
        normalized.get("delegation_required") or requested_mode == "delegate"
    )
    adjusted = False
    if (
        advice.get("recommended") == "self_execute"
        and asked_to_delegate
        and not requirements.get("manual_B_creation")
    ):
        normalized["execution_mode"] = "self_execute"
        normalized["delegation_required"] = False
        normalized["minimum_delegated_outcomes"] = 0
        if transition == "delegate_initial":
            normalized["routing_transition"] = "keep_supervisor"
        adjusted = True

    if requirements.get("manual_B_creation"):
        if normalized.get("execution_mode") not in ("delegate", "either"):
            raise ValueError(
                "The user explicitly requested a new B; execution_mode must be delegate or either"
            )
        normalized["delegation_required"] = True
        normalized["minimum_delegated_outcomes"] = max(
            1, int(normalized.get("minimum_delegated_outcomes", 0))
        )
    elif normalized.get("user_requested_delegation"):
        if normalized.get("execution_mode") not in ("delegate", "either"):
            raise ValueError(
                "A semantically explicit subprocess request must use delegate or either"
            )
        if not str(normalized.get("user_request_evidence") or "").strip():
            raise ValueError(
                "A semantically explicit subprocess request requires quoted user evidence"
            )
        normalized["delegation_required"] = True
        normalized["minimum_delegated_outcomes"] = max(
            1, int(normalized.get("minimum_delegated_outcomes", 0))
        )

    compiled_candidate = {**candidate, **normalized}
    compiled_advice = routing_advice(compiled_candidate)
    # Ownership and authority are separate axes.  A bounded owner may research,
    # calculate and answer with observation-only tools without an execution
    # lease.  A lease is required only when the declared delivery itself is
    # effectful; individual tools still enforce their own authority at call
    # time, so an incomplete contract cannot accidentally grant write access.
    required_deliverables = [
        dict(item) for item in (normalized.get("deliverables") or [])
        if bool(dict(item).get("required", True))
    ]
    declared_effectful_delivery = any(
        str(item.get("kind") or "other") != "response"
        for item in required_deliverables
    )
    self_execute_action = (
        "request_self_execute"
        if declared_effectful_delivery
        else "continue_answer"
    )
    compilation = {
        "requested": requested_mode,
        "recommended": compiled_advice.get("recommended"),
        "reason": advice.get("reason") if adjusted else compiled_advice.get("reason"),
        "adjusted": adjusted,
        "authority_granted": False,
        "next_action": (
            self_execute_action
            if compiled_advice.get("recommended") == "self_execute"
            else "continue_owner"
            if compiled_advice.get("recommended") == "continue_worker"
            else "delegate_task"
        ),
    }
    return normalized, compilation


def _update_contract(process, update) -> dict:
    """Atomically update the task contract inside the versioned context snapshot."""
    with process._context_lock:
        context = dict(process.context_snapshot or {})
        contract = dict(context.get("task_contract") or {})
        contract = update(contract)
        context["task_contract"] = contract
        process.context_snapshot = context
        process.context_version += 1
        return dict(contract)


def publish_contract(process, proposal: dict) -> dict:
    def apply(previous: dict) -> dict:
        preserved_attempts = list(previous.get("delegation_attempts") or [])
        revision = int(previous.get("revision", 0) or 0) + 1
        merged = {
            key: previous[key]
            for key in (
                "task_description", "task_kind", "capability_profile_id",
                "required_test_ids", "host_requirements",
            )
            if key in previous
        }
        # Host facts come from explicit UI actions, never natural-language inference.
        requirements = dict(previous.get("host_requirements") or {})
        workflow_started = bool(
            previous.get("workflow_started")
            or getattr(process, "successful_actions", 0)
        )
        normalized, compilation = compile_routing_proposal(
            previous, proposal, workflow_started=workflow_started,
        )
        merged.update(normalized)
        if requirements:
            merged["host_requirements"] = requirements
        merged["routing_compilation"] = compilation
        merged["revision"] = revision
        merged["delegation_attempts"] = preserved_attempts
        return merged

    return _update_contract(process, apply)


def get_task_contract(process) -> dict:
    if getattr(process, "read_context_snapshot", None):
        context, _version = process.read_context_snapshot()
    else:
        # Lightweight test/compatibility processes predate versioned snapshots.
        # Reading their already-owned dict is safe; production AgentProcess
        # always takes the locked path above.
        context = dict(getattr(process, "context_snapshot", {}) or {})
    return dict(context.get("task_contract") or {})


def delegation_intent_id(
    task_description: str,
    target_files: list[str],
    acceptance_criteria: list[str],
) -> str:
    canonical = json.dumps({
        "task_description": str(task_description).strip(),
        "target_files": sorted(str(item) for item in target_files),
        "acceptance_criteria": sorted(str(item) for item in acceptance_criteria),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "delegation_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def record_delegation_attempt(
    process,
    *,
    intent_id: str,
    required: bool,
    profile_id: str,
    task_kind: str,
    target_files: list[str],
) -> dict:
    def apply(contract: dict) -> dict:
        attempts = list(contract.get("delegation_attempts") or [])
        attempt = {
            "intent_id": intent_id,
            "attempt": 1 + sum(
                1 for item in attempts if item.get("intent_id") == intent_id
            ),
            "state": "admitting",
            "required_for_parent_completion": bool(required),
            "profile_id": profile_id,
            "task_kind": task_kind,
            "target_files": list(target_files),
        }
        attempts.append(attempt)
        contract["delegation_attempts"] = attempts[-32:]
        contract["workflow_started"] = True
        if required and not contract.get("delegation_required"):
            # A's structured tool call is a stronger signal than prose.  It does
            # not claim that the user mandated B, but it prevents a failed
            # required strategy from disappearing behind answer semantics.
            contract["agent_required_delegation"] = True
            contract["minimum_delegated_outcomes"] = max(
                1, int(contract.get("minimum_delegated_outcomes", 0) or 0),
            )
        return contract

    return _update_contract(process, apply)


def finish_delegation_attempt(
    process,
    *,
    intent_id: str,
    state: str,
    child_process_id: str = "",
    error_info: dict | None = None,
) -> dict:
    def apply(contract: dict) -> dict:
        attempts = list(contract.get("delegation_attempts") or [])
        for index in range(len(attempts) - 1, -1, -1):
            item = attempts[index]
            if item.get("intent_id") == intent_id and item.get("state") == "admitting":
                item = dict(item)
                item["state"] = state
                if child_process_id:
                    item["child_process_id"] = child_process_id
                if error_info:
                    item["error_info"] = dict(error_info)
                attempts[index] = item
                break
        contract["delegation_attempts"] = attempts
        return contract

    return _update_contract(process, apply)


def record_unbound_delegation_failure(
    process, *, tool_name: str, error_info: dict | None = None,
) -> dict:
    """Preserve a failed delegation call rejected before tool execution.

    JSON-schema validation happens before ``delegate_task`` can create its
    semantic intent record.  The Host still knows which structural tool A chose,
    so that workflow attempt must not disappear behind answer completion.
    """
    def apply(contract: dict) -> dict:
        attempts = list(contract.get("delegation_attempts") or [])
        attempts.append({
            "intent_id": f"unbound_{tool_name}_{len(attempts) + 1}",
            "attempt": 1,
            "state": "admission_failed",
            "required_for_parent_completion": True,
            "tool_name": tool_name,
            "error_info": dict(error_info or {}),
        })
        contract["delegation_attempts"] = attempts[-32:]
        contract["workflow_started"] = True
        contract["agent_required_delegation"] = True
        contract["minimum_delegated_outcomes"] = max(
            1, int(contract.get("minimum_delegated_outcomes", 0) or 0),
        )
        return contract

    return _update_contract(process, apply)
