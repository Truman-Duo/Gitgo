"""LLM-proposed, Host-validated task intent contracts.

The model owns semantic interpretation of user language.  The Host never uses
keywords or regular expressions to infer intent; it validates paths, enums and
evidence, then persists the proposal as task-scoped control-plane data.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any
import uuid


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


def semantic_contract(contract):
    """Stable request/dependency fields; lease/timing/revision are Host mechanics."""
    return {key: contract[key] for key in (
        'goal', 'execution_mode', 'deliverables', 'acceptance_criteria', 'uncertainties',
        'requires_user_decision', 'estimated_complexity', 'independent_workstreams',
        'delegation_required', 'user_requested_delegation', 'engineering_workflow',
        'required_tool_calls', 'required_workbench_claims', 'mechanism', 'maintenance_intent') if key in contract}


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
    if "expected_revision" in args and (type(args["expected_revision"]) is not int or args["expected_revision"] < 0):
        raise ValueError("expected_revision must be a non-negative integer")
    mechanism = None
    if args.get("mechanism") is not None:
        from .mechanism_catalog import compile_definition
        mechanism = compile_definition(args["mechanism"])
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
    if delegation_required and 'minimum_delegated_outcomes' not in args:
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
        if 'minimum_delegated_outcomes' not in args:
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

    workbench_claims = args.get("required_workbench_claims", [])
    if not isinstance(workbench_claims, list) or len(workbench_claims) > 12:
        raise ValueError("required_workbench_claims must be a list of at most 12 scopes")
    if workbench_claims:
        from backend.core.workbench.delivery import claim_scope
        workbench_claims = list(dict.fromkeys(claim_scope(scope) for scope in workbench_claims))

    workflow = None
    if args.get("engineering_workflow") is not None:
        from backend.core.loop.engineering_workflow import compile_plan
        workflow = compile_plan(args["engineering_workflow"], workspace)

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
        **({"required_workbench_claims": workbench_claims} if workbench_claims else {}),
        "ignored_protocol_requirements": list(dict.fromkeys(
            ignored_protocol_requirements
        )),
        **({"engineering_workflow": workflow} if workflow is not None else {}),
        **({"expected_revision": args["expected_revision"]} if "expected_revision" in args else {}),
        **({"mechanism": mechanism} if mechanism is not None else {}),
        **({key: deepcopy(args[key]) for key in ("maintenance", "preview_digest") if key in args}),
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
        normalized['minimum_delegated_outcomes'] = max(1, int(normalized.get('minimum_delegated_outcomes', 0) or 0))
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
        # Creation and parent completion are separate obligations. Semantic
        # requests may explicitly make the child's result optional; omission
        # remains strict. A preserved manual_B_creation requirement above is
        # authoritative and cannot be weakened by a model-authored proposal.
        normalized.setdefault("minimum_delegated_outcomes", 1)

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
        if contract.get("engineering_workflow"):
            from .engineering_workflow import EngineeringWorkflow, KEY
            context[KEY] = EngineeringWorkflow(process).configured_state(contract[KEY])
        context["task_contract"] = contract
        process.context_snapshot = context
        process.context_version += 1
        return dict(contract)


def _compile_contract(process, previous: dict, proposal: dict) -> dict:
    preserved_attempts = list(previous.get("delegation_attempts") or [])
    revision = int(previous.get("revision", 0) or 0) + 1
    merged = {
        key: previous[key]
        for key in (
            "task_description", "task_kind", "capability_profile_id",
            "required_test_ids", "host_requirements",
            "maintenance_intent",
            "target_files", "allowed_tools", "required_for_parent_completion", "input_contract", "output_contract",
        )
        if key in previous
    }
    # Host facts come from explicit UI actions, never natural-language inference.
    requirements = dict(previous.get("host_requirements") or {})
    workflow_started = bool(previous.get("workflow_started") or getattr(process, "successful_actions", 0))
    normalized, compilation = compile_routing_proposal(previous,
        {key: value for key, value in proposal.items() if key not in {"expected_revision", "maintenance", "preview_digest"}}, workflow_started=workflow_started)
    merged.update(normalized)
    if requirements:
        merged["host_requirements"] = requirements
    merged["routing_compilation"] = compilation
    merged["revision"] = revision
    merged["delegation_attempts"] = preserved_attempts
    # Omission never retires an active plan. Replacement still goes through
    # EngineeringWorkflow's existing explicit scope-amendment decision.
    from .engineering_workflow import EngineeringWorkflow, KEY
    state = EngineeringWorkflow(process)._load()
    if KEY not in merged and state:
        merged[KEY] = deepcopy(state["plan"])
    if "mechanism" not in merged and previous.get("mechanism") and not (getattr(process, "context_snapshot", {}) or {}).get("task_maintenance", {}).get("detached"):
        merged["mechanism"] = deepcopy(previous["mechanism"])
    return merged


class TaskContractRevisionConflict(ValueError):
    pass


def _prepare_contract(process, proposal):
    previous = get_task_contract(process)
    revision = int(previous.get("revision", 0) or 0)
    expected = proposal.get("expected_revision", revision)
    if type(expected) is not int or expected != revision:
        raise TaskContractRevisionConflict("TASK_CONTRACT_REVISION_CONFLICT: read the current contract before revising it")
    contract = _compile_contract(process, previous, proposal)
    if getattr(process, 'session', None) is not None:
        from .mechanisms import reconcile_definition
        reconcile_definition(process, contract)
    from .engineering_workflow import EngineeringWorkflow, KEY
    workflow = EngineeringWorkflow(process).configured_state(contract[KEY]) if contract.get(KEY) else None
    if proposal.get("maintenance"):
        contract["maintenance_intent"] = {key: deepcopy(value) for key, value in proposal["maintenance"].items() if key != "expected_revision"}
    maintenance = None
    if getattr(process, "session", None) is not None:
        from .task_maintenance import prepare_contract_state
        maintenance = prepare_contract_state(process, contract, proposal, workflow)
    if proposal.get("preview_digest"):
        from .mechanism_catalog import digest
        if proposal["preview_digest"] != digest(contract):
            raise TaskContractRevisionConflict("TASK_CONTRACT_PREVIEW_CHANGED: recompile the behavior preview")
    return contract, workflow, maintenance


def preview_contract(process, proposal: dict) -> dict:
    """Use the exact publication compiler without changing state or authority."""
    if getattr(process, "session", None) is not None:
        from .mechanism_lifecycle import fence
        with fence(process), process.session._mechanism_lock, process._context_lock:
            return _prepare_contract(process, proposal)[0]
    with process._context_lock:
        return _prepare_contract(process, proposal)[0]


def publish_contract(process, proposal: dict, *, store=None) -> dict:
    """Activate one task revision with its workflow and adopted obligations.

    Production adapters pass their existing SessionStore. Pure compiler callers
    may omit it; those activations are explicitly marked memory-only.
    """
    if getattr(process, "session", None) is None:
        # Legacy compiler clients have a context but no runnable AgentSession.
        # They cannot publish durable activation evidence or enter admission.
        if store is not None:
            raise ValueError("task contract activation requires an AgentSession")
        return _update_contract(process, lambda previous: _prepare_contract(process, proposal)[0])
    from .mechanism_lifecycle import fence, commit_activation
    from .engineering_workflow import KEY
    with fence(process) as root, process.session._mechanism_lock, process._context_lock, process._coordination_lock:
        if root.session.mechanism_activation_blocked or process.session.mechanism_activation_blocked:
            raise ValueError("MECHANISM_ACTIVATION_BLOCKED")
        contract, workflow, maintenance = _prepare_contract(process, proposal)
        revision = contract["revision"]
        activation = {"transaction_id": str(uuid.uuid4()), "base_revision": revision - 1,
            "revision": revision, "process_id": process.process_id,
            "task_id": process.active_task_id or process.process_id,
            "contract_digest": hashlib.sha256(json.dumps(semantic_contract(contract), sort_keys=True).encode()).hexdigest(),
            "workflow_digest": (workflow or {}).get("plan", {}).get("digest"),
            "consumers": ["task_routing", "engineering_workflow", "semantic_alignment", "completion", "tool_admission"],
            "phase": "effective", "persistence": "checkpoint" if store is not None else "memory_only"}
        contract["activation"] = activation
        before_context, before_version = process.context_snapshot, process.context_version
        before_children = deepcopy(process.delegated_contracts)
        before_mechanisms = deepcopy(root.session.mechanism_instances)
        staged_mechanisms = before_mechanisms
        context = {**(before_context or {}), "task_contract": contract}
        if workflow is not None:
            context[KEY] = workflow
        if maintenance is not None:
            from .task_maintenance import KEY as MAINTENANCE_KEY
            context[MAINTENANCE_KEY] = maintenance
            from .mechanism_catalog import enabled
            advancement = enabled(maintenance.get("definition"), "task.advancement")
            previous_definition = (before_context or {}).get(MAINTENANCE_KEY, {}).get("definition")
            if advancement is not None and maintenance.get("definition") != previous_definition:
                if root is not process:
                    raise ValueError("shared advancement must be revised by its owning task")
                from . import mechanisms
                from .mechanism_lifecycle import snapshot, compile_revision
                current = snapshot(process, mechanisms.DEFINITION)
                if current["parameters"] != advancement:
                    control_sequence = current.get("control_sequence", -1)
                    receipts = maintenance.get("source_receipts", {})
                    if control_sequence >= 0 and not any(receipts.get(ref, {}).get("authority_sequence", -1) > control_sequence
                            for ref in maintenance["definition"]["source_refs"]):
                        raise ValueError("MECHANISM_USER_CONTROL_NEWER: retain the user's newer mode selection; a prior request cannot override it")
                    staged_mechanisms = {**before_mechanisms,
                        mechanisms.ADVANCEMENT: compile_revision(current, mechanisms.DEFINITION, advancement, current["revision"])}
        for child_id in proposal.get("adopt_process_ids") or []:
            child = process.delegated_contracts.get(child_id)
            if child is None or child.get("superseded_by"):
                raise ValueError("cannot adopt a missing or superseded task contract")
        root.session.mechanism_instances = staged_mechanisms
        process.context_snapshot, process.context_version = context, before_version + 1
        for child_id in proposal.get("adopt_process_ids") or []:
            process.delegated_contracts[child_id]["required_for_parent_completion"] = True

        def rollback():
            process.context_snapshot, process.context_version = before_context, before_version
            process.delegated_contracts = before_children
            root.session.mechanism_instances = before_mechanisms

        event = {"event": "task_contract_revision_activated", **deepcopy(activation),
                 "adopted_changes": [key for key, value in (maintenance or {}).get("changes", {}).items()
                     if value.get("contract_revision") == revision],
                 "mechanism_manifest": deepcopy((maintenance or {}).get("manifest"))}
        if store is not None:
            commit_activation(process, event, store, rollback)
        else:
            process.session.host_ledger.append(event)
        return deepcopy(contract)


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
