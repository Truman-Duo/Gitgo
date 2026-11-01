"""task contracts protocol tools."""
from __future__ import annotations

from backend.core.prompt_templates import load_prompt
from backend.core.loop.execution_contract import HOST_COMPUTE, data_broker
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.completion_protocol import CompletionClaim

def install(ctx):
    from backend.core.loop.mechanism_catalog import ELEMENTS, maintenance_schema
    from backend.core.loop.task_contract import preview_contract, validate_contract_proposal, get_task_contract, TaskContractRevisionConflict
    process = ctx.process
    available_tools = ctx.available_tools
    workspace_path = ctx.workspace_path
    tools = ctx.tools
    manager = ctx.manager
    engineering = ctx.engineering
    def proposal_for(args):
        proposal = validate_contract_proposal(args, workspace_path or process.worktree_path)
        if process.actor_kind != "supervisor":
            from backend.core.loop.task_maintenance import state_for
            if not state_for(process).get("allow_contract_reconciliation"):
                raise ValueError("worker scope revision requires an explicit adopted user change")
            previous = get_task_contract(process)
            if not set(previous.get("acceptance_criteria") or []).issubset(proposal.get("acceptance_criteria") or []):
                raise ValueError("worker revisions cannot remove supervisor-assigned acceptance obligations")
            for required in previous.get("deliverables") or []:
                if required.get("required", True) and not any(item.get("required", True)
                    and (item.get("kind"), item.get("path")) == (required.get("kind"), required.get("path"))
                    for item in proposal.get("deliverables") or []):
                    raise ValueError("worker revisions cannot remove supervisor-assigned deliverables")
            if proposal.get("execution_mode") in {"delegate", "either"} or proposal.get("adopt_process_ids"):
                raise ValueError("worker scope revision cannot acquire supervisor delegation authority")
        if get_task_contract(process).get("revision", 0) and "expected_revision" not in proposal:
            raise TaskContractRevisionConflict("TASK_CONTRACT_REVISION_REQUIRED: submit expected_revision from the current contract")
        adopted = proposal.get("adopt_process_ids", [])
        if len(adopted) > 64:
            raise ValueError("at most 64 previous executions may be adopted")
        for child_id in adopted:
            if manager is None or not manager.owns_child(process, manager.get(child_id)):
                raise ValueError("adopt_process_ids must identify owned child executions from list_agents, never the current process. "
                    "For the supervisor's own continuing work use keep_supervisor without adoption.")
            if child_id not in process.delegated_contracts or process.delegated_contracts[child_id].get("superseded_by"):
                raise ValueError("cannot adopt a missing or superseded task contract")
        return proposal

    def declare_task_contract(args: dict) -> dict:
        from backend.core.loop.task_contract import (
            publish_contract,
            validate_contract_proposal,
        )
        try:
            proposal = proposal_for(args)
        except TaskContractRevisionConflict as exc:
            return {"error": "TASK_CONTRACT_REVISION_CONFLICT", "detail": str(exc),
                    "accepted": False, "effect_state": "not_committed", "contract": get_task_contract(process)}
        except (TypeError, ValueError) as exc:
            return {
                "error": "INVALID_TASK_CONTRACT",
                "detail": str(exc),
                "accepted": False,
                "effect_state": "not_committed",
            }
        try:
            contract = publish_contract(process, proposal, store=getattr(manager, "session_store", None))
        except TaskContractRevisionConflict as exc:
            return {"error": "TASK_CONTRACT_REVISION_CONFLICT", "detail": str(exc),
                    "accepted": False, "effect_state": "not_committed", "contract": get_task_contract(process)}
        except ValueError as exc:
            return {
                "error": "INVALID_TASK_ROUTING",
                "detail": str(exc),
                "accepted": False,
                "effect_state": "not_committed",
                "routing_advice": {
                    "recommended": "self_execute",
                    "next_action": "request_self_execute",
                },
            }
        except (OSError, RuntimeError) as exc:
            from backend.core.storage import StorageCommitUnknown
            uncertain = isinstance(exc, StorageCommitUnknown)
            return {"error": "TASK_CONTRACT_COMMIT_UNKNOWN" if uncertain else "TASK_CONTRACT_CHECKPOINT_FAILED",
                    "detail": str(exc), "accepted": False,
                    "effect_state": "ambiguous" if uncertain else "not_committed"}
        from backend.core.loop.task_contract import routing_advice
        if proposal.get("engineering_workflow"):
            engineering.notice("WORKFLOW_CONFIGURED", "工程工作流已随任务契约启用；未完成证据会进入 Host 完成检查。")
        from backend.core.loop.task_maintenance import state_for
        adopted_changes = [value["id"] for value in state_for(process).get("changes", {}).values()
                           if value.get("contract_revision") == contract["revision"]]
        if adopted_changes:
            engineering.notice("TASK_CHANGE_ADOPTED", "新增要求已纳入当前任务的契约、计划与验收条件。", change_ids=adopted_changes,
                               contract_revision=contract["revision"])
        maintenance = state_for(process)
        return {
            "accepted": True,
            "contract": contract,
            "host_validated": True,
            "authority_granted": False,
            "activation": contract["activation"],
            "routing_advice": routing_advice(contract),
            "routing_compilation": dict(contract.get("routing_compilation") or {}),
            "maintenance_status": {
                "phase": maintenance.get("phase"),
                "revision": maintenance.get("revision"),
                "review_requirements": dict(maintenance.get("review_requirements") or {}),
                "pending_trigger_ids": [key for key, item in maintenance.get("maintenance_items", {}).items()
                                        if item.get("status") == "pending"],
                "next_action": "reconcile_with_declare_task_contract" if maintenance.get("phase") == "needs_review" else "",
            } if maintenance else None,
            "budget_card": (
                process.task_budget.decision_card(process.process_id)
                if process.task_budget is not None else None
            ),
        }


    tools["declare_task_contract"] = AgentTool(
        execution_contract=data_broker("host.runtime.declare_task_contract"),
        name="declare_task_contract",
        alignment_contract=lambda args: preview_contract(process, proposal_for(args)),
        description=(
            load_prompt('tools/declare_task_contract')
        ),
        parameters={
            "type": "object",
            "properties": {
                "mechanism": {"type": "object", "additionalProperties": False,
                    "description": "Registered task mechanism draft. Each element requires use=exact catalog id; id is an optional instance name. Copy exact source_refs from task_mechanism status, never file paths.",
                    "properties": {
                        "id": {"type": "string"}, "intent": {"type": "string"}, "catalog_version": {"type": "string"},
                        "source_refs": {"type": "array", "items": {"type": "string", "pattern": "^(user|decision|change):"}, "minItems": 1, "maxItems": 32},
                        "elements": {"type": "array", "maxItems": len(ELEMENTS), "items": {"type": "object", "additionalProperties": False,
                            "properties": {"id": {"type": "string"}, "use": {"type": "string", "enum": list(ELEMENTS)},
                                "version": {"type": "integer", "enum": [1]}, "parameters": {"type": "object"}}, "required": ["use"]}},
                        "bindings": {"type": "array", "items": {"type": "object", "properties": {"from": {"type": "string"}, "to": {"type": "string"}}, "required": ["from", "to"], "additionalProperties": False}}},
                    "required": ["intent", "source_refs", "elements"]},
                "maintenance": maintenance_schema(),
                "preview_digest": {"type": "string", "description": "Optional task_mechanism preview digest; rejects a changed preview before activation."},
                "expected_revision": {"type": "integer", "minimum": 0,
                    "description": "Required for a revision after initial declaration. Use the current contract revision; stale previews must be recomputed."},
                "goal": {"type": "string"},
                "execution_mode": {
                    "type": "string",
                    "enum": [
                        "answer", "delegate", "self_execute", "either",
                        "plan", "review",
                    ],
                },
                "delegation_required": {"type": "boolean"},
                "adopt_process_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 64,
                    "description": "Only existing owned child executions from list_agents whose results this task must adopt. Never the current process id; omit for continuing your own work."},
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
                    "description": "keep_supervisor continues or revises the supervisor's own work. continue_owner continues an existing owned worker and requires its adopt_process_ids. Neither grants authority.",
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
        execution_contract=data_broker("host.runtime.complete_supervision"),
        name="complete_supervision",
        description=(
            load_prompt('tools/complete_supervision')
        ),
        parameters={
            "type": "object",
            "properties": {
                "result": {"type": "string", "description": load_prompt('tools/completion_result')},
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
        execution_contract=data_broker("host.runtime.request_self_execute"),
        name="request_self_execute",
        description=(
            load_prompt('tools/request_self_execute')
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
