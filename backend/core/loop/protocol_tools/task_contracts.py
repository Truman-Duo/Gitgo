"""task contracts protocol tools."""
from __future__ import annotations
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.completion_protocol import CompletionClaim

def install(ctx):
    from backend.core.loop.task_contract import preview_contract, validate_contract_proposal
    process = ctx.process
    available_tools = ctx.available_tools
    workspace_path = ctx.workspace_path
    tools = ctx.tools
    manager = ctx.manager
    engineering = ctx.engineering
    def declare_task_contract(args: dict) -> dict:
        from backend.core.loop.task_contract import (
            publish_contract,
            validate_contract_proposal,
        )
        try:
            proposal = validate_contract_proposal(
                args, workspace_path or process.worktree_path,
            )
            adopted = proposal.get("adopt_process_ids", [])
            if len(adopted) > 64:
                raise ValueError("at most 64 previous executions may be adopted")
            for child_id in adopted:
                if manager is None or not manager.owns_child(process, manager.get(child_id)):
                    raise ValueError("adopt_process_ids must identify owned executions from list_agents")
                if child_id not in process.delegated_contracts or process.delegated_contracts[child_id].get("superseded_by"):
                    raise ValueError("cannot adopt a missing or superseded task contract")
        except (TypeError, ValueError) as exc:
            return {
                "error": "INVALID_TASK_CONTRACT",
                "detail": str(exc),
                "accepted": False,
            }
        try:
            contract = publish_contract(process, proposal)
        except ValueError as exc:
            return {
                "error": "INVALID_TASK_ROUTING",
                "detail": str(exc),
                "accepted": False,
                "routing_advice": {
                    "recommended": "self_execute",
                    "next_action": "request_self_execute",
                },
            }
        from backend.core.loop.task_contract import routing_advice
        if proposal.get("engineering_workflow"):
            engineering.notice("WORKFLOW_CONFIGURED", "工程工作流已随任务契约启用；未完成证据会进入 Host 完成检查。")
        with process._coordination_lock:
            for child_id in adopted:
                process.delegated_contracts[child_id]["required_for_parent_completion"] = True
        return {
            "accepted": True,
            "contract": contract,
            "host_validated": True,
            "authority_granted": False,
            "routing_advice": routing_advice(contract),
            "routing_compilation": dict(contract.get("routing_compilation") or {}),
            "budget_card": (
                process.task_budget.decision_card(process.process_id)
                if process.task_budget is not None else None
            ),
        }


    tools["declare_task_contract"] = AgentTool(
        name="declare_task_contract",
        alignment_contract=lambda args: preview_contract(process,
            validate_contract_proposal(args, workspace_path or process.worktree_path)),
        description=(
            "Compile your semantic understanding of a non-trivial user request "
            "into a Host-validated task contract before effectful or delegated "
            "work. This is not keyword classification and grants no authority. "
            "Do not call it for ordinary conversation. Record whether delegation "
            "or concrete workspace artifacts are genuine requirements; list any "
            "unresolved product choice so request_user_decision can handle it. "
            "For bounded single-owner work, choose self_execute. Continue directly "
            "when the delivery is only a response or uses observation-only tools; "
            "request_self_execute only before an actual mutating delivery. Delegation "
            "is valid only for truthful complexity, "
            "multiple workstreams, explicit handoff/parallelization, or a user's "
            "manual B request. When the user explicitly asks for a subprocess/B, "
            "set user_requested_delegation=true and quote the relevant request in "
            "user_request_evidence; do not infer this flag merely because delegation "
            "would be convenient. If the wording is ambiguous, put that ambiguity in "
            "uncertainties and ask the user instead of guessing. "
            "The result includes a deterministic Budget Card. Treat a B as having "
            "setup and coordination cost: persisting or testing an answer A already "
            "derived is still bounded single-owner work, not a separate workstream. "
            "For continuing/reviewing prior work, select adopt_process_ids from "
            "list_agents to make those outcomes required for this turn. Unrelated "
            "new tasks must not inherit old delivery obligations automatically."
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "execution_mode": {
                    "type": "string",
                    "enum": [
                        "answer", "delegate", "self_execute", "either",
                        "plan", "review",
                    ],
                },
                "delegation_required": {"type": "boolean"},
                "adopt_process_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
                "minimum_delegated_outcomes": {
                    "type": "integer", "minimum": 0, "maximum": 8,
                },
                "deliverables": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["workspace_file", "response", "other"],
                            },
                            "path": {"type": "string"},
                            "description": {"type": "string"},
                            "required": {"type": "boolean"},
                            "allow_inline_substitution": {"type": "boolean"},
                        },
                        "required": ["kind", "required"],
                    },
                },
                "acceptance_criteria": {
                    "type": "array", "items": {"type": "string"},
                },
                "uncertainties": {
                    "type": "array", "items": {"type": "string"},
                },
                "requires_user_decision": {"type": "boolean"},
                "engineering_workflow": {"type": "object", "description": "Optional Host-enforced practice plan: profiles and evidence nodes with depends_on; use engineering_workflow status to discover supported practices."},
                "estimated_complexity": {
                    "type": "string", "enum": ["bounded", "moderate", "high"],
                },
                "independent_workstreams": {
                    "type": "integer", "minimum": 1, "maximum": 8,
                },
                "delegation_rationale": {"type": "string"},
                "user_requested_delegation": {"type": "boolean"},
                "user_request_evidence": {"type": "string"},
                "routing_transition": {
                    "type": "string",
                    "enum": [
                        "initial", "keep_supervisor", "delegate_initial",
                        "handoff_to_worker", "continue_owner", "parallelize",
                    ],
                },
                "handoff_summary": {"type": "string"},
                "required_tool_calls": {
                    "type": "array",
                    "description": (
                        "Optional domain-tool receipt requirements. Never list complete_task, "
                        "complete_supervision, or complete_review: completion protocols are "
                        "selected by current Host state and cannot be acceptance evidence."
                    ),
                    "maxItems": 32,
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool_name": {"type": "string"},
                            "min_calls": {"type": "integer", "minimum": 0, "maximum": 100},
                            "max_calls": {"type": "integer", "minimum": 0, "maximum": 100},
                            "include_composite_steps": {"type": "boolean"},
                        },
                        "required": ["tool_name"],
                    },
                },
            },
            "required": [
                "goal", "execution_mode", "delegation_required",
                "deliverables", "acceptance_criteria", "uncertainties",
                "requires_user_decision", "user_requested_delegation",
                "user_request_evidence",
            ],
        },
        execute=declare_task_contract,
        read_only=False,
        effect=ToolEffect.PROCESS,
        idempotent=False,
    )


    def complete_supervision(args: dict) -> dict:
        result = str(args.get("result", "")).strip()
        if not result:
            return {"accepted": False, "error": "result is required"}
        # Keep the public synthesis separate from process evidence even
        # though supervisor completion is checked from authoritative Host
        # child/review facts.  Persisting the structured claim makes the
        # boundary inspectable without printing it to the user.
        try:
            process.completion_claim = CompletionClaim.from_args(
                {
                    "result": result,
                    "verification": list(args.get("verification") or []),
                    "files": list(args.get("files") or []),
                    "diff_summary": str(args.get("diff_summary") or ""),
                },
                step=process.steps_used,
            )
        except ValueError as exc:
            return {"accepted": False, "error": str(exc)}
        requested = str(args.get("status") or "auto").strip().lower()
        terminal_status = "completed"
        if requested == "failed":
            terminal_status = "failed"
        elif requested == "auto":
            manager = getattr(process, "_manager", None)
            _child_ids, contracts, reviews = process.coordination_snapshot()
            for child_id, contract in contracts.items():
                if not contract.get("required_for_parent_completion", True):
                    continue
                if contract.get("superseded_by"):
                    continue
                child = manager.get(child_id) if manager else None
                review = reviews.get(child_id, {})
                if (
                    child is not None
                    and child.status.value not in {"running", "waiting"}
                    and (
                        child.status.value != "completed"
                        or review.get("verdict") == "changes_required"
                    )
                ):
                    terminal_status = "failed"
                    break
        response = {"accepted": True, "result": result}
        if terminal_status == "failed":
            response["terminal_status"] = "failed"
        return response


    tools["complete_supervision"] = AgentTool(
        name="complete_supervision",
        description=(
            "Submit the final A-level synthesis. The Host verifies every "
            "required B is terminal and explicitly reviewed, then closes the "
            "supervisor task without a TASK_COMPLETE password. If a required "
            "delivery failed, review it as changes_required and submit the "
            "failure report here; status=auto lets the Host derive failure."
        ),
        parameters={
            "type": "object",
            "properties": {
                "result": {"type": "string", "description": (
                    "User-facing outcome: what was delivered and any material limitation. "
                    "Do not enumerate tests, searches, retries, receipts, hashes, process IDs "
                    "or governance procedures. Put those facts in verification unless the user "
                    "explicitly requested a process audit."
                )},
                "verification": {
                    "type": "array", "items": {"type": "object"},
                    "description": (
                        "Internal verification and audit facts. This field is persisted but "
                        "is not rendered as the final public answer."
                    ),
                },
                "files": {"type": "array", "items": {"type": "string"}},
                "diff_summary": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["auto", "completed", "failed"],
                    "default": "auto",
                },
            },
            "required": ["result"],
        },
        execute=complete_supervision,
        read_only=True,
        effect=ToolEffect.READ,
        idempotent=False,
    )


    def request_self_execute(args: dict) -> dict:
        requested_profile = str(args.get("profile_id") or "").strip()
        profile_id = requested_profile
        try:
            requested_definition = (
                CapabilityProfiles.get(requested_profile)
                if requested_profile else None
            )
        except ValueError:
            requested_definition = None
        if (
            requested_definition is None
            or not requested_definition.allows_self_execution
        ):
            from backend.core.loop.task_contract import get_task_contract
            contract = get_task_contract(process)
            workspace_delivery = any(
                str(item.get("kind") or "") == "workspace_file"
                and bool(item.get("required", True))
                for item in list(contract.get("deliverables") or [])
                if isinstance(item, dict)
            )
            profile_id = (
                "development.workspace"
                if workspace_delivery else "governance.operate"
            )
        try:
            lease = CapabilityProfiles.issue_self_execute_lease(
                actor_kind=process.actor_kind,
                task_id=process.active_task_id or process.process_id,
                requested_by=process.process_id,
                profile_id=profile_id,
                reason=str(args.get("reason", "")),
                intended_actions=list(args.get("intended_actions", []) or []),
            )
        except (ValueError, PermissionError) as exc:
            return {"error": str(exc), "granted": False}
        process.capability_lease = lease
        # A lease grants a capability; it is not evidence that an action
        # happened.  The Host promotes the task only after a committed
        # mutating receipt is observed.  This keeps research/answer turns
        # out of action completion and reviewer loops while preserving
        # strict evidence gates for real writes and external mutations.
        names = CapabilityProfiles.resolve_tools(
            process.capability_profile_id, lease=lease,
        )
        from backend.core.loop.tools import ToolRegistry
        process.tool_registry = ToolRegistry(names)
        # Rebuilding a capability surface must not lose compatible task-scoped
        # definitions. Their leaves still need exact approvals at execution.
        from ..tool_surface import component_blocker
        catalog = {**available_tools, **process.dynamic_tools}
        for name, tool in process.dynamic_tools.items():
            if component_blocker(process, tool.composite_spec or {}, catalog) is None:
                process.tool_registry.add_scoped(name)
        response = {
            "lease_id": lease.lease_id,
            "profile_id": lease.profile_id,
            "task_id": lease.task_id,
            "granted": True,
        }
        if requested_profile and requested_profile != profile_id:
            response["profile_normalized_from"] = requested_profile
        return response


    tools["request_self_execute"] = AgentTool(
        name="request_self_execute",
        description=(
            "Explicitly request the task-scoped capability lease selected by an "
            "accepted self_execute contract. Call this when the Host routing result "
            "says request_self_execute: the current pre-lease surface intentionally "
            "does not yet contain file mutation, command or test tools. A successful "
            "lease rebuilds the surface with those tools. Use development.workspace "
            "for files, commands or tests; governance.operate for governance-only "
            "state changes. This explicit call is required and never implied by prose."
        ),
        parameters={
            "type": "object",
            "properties": {
                "profile_id": {
                    "type": "string",
                    "enum": ["development.workspace", "governance.operate"],
                    "description": (
                        "Optional least-privilege lease. Use development.workspace "
                        "for files, commands or tests; governance.operate for a "
                        "governance-only mutation. The Host derives a safe default "
                        "from the structured contract when omitted."
                    ),
                },
                "reason": {"type": "string"},
                "intended_actions": {
                    "type": "array", "items": {"type": "string"},
                },
            },
            "required": ["reason", "intended_actions"],
        },
        execute=request_self_execute,
        read_only=True,
        effect=ToolEffect.READ,
        idempotent=False,
    )
