"""task contracts protocol tools."""
from __future__ import annotations
from copy import deepcopy
from backend.core.loop.model_feedback import contract_feedback

from backend.core.prompt_templates import load_prompt
from backend.core.loop.execution_contract import HOST_COMPUTE, data_broker
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.completion_protocol import CompletionClaim

def install(ctx):
    from backend.core.loop.engineering_workflow import plan_schema
    from backend.core.loop.mechanism_catalog import ELEMENTS, maintenance_schema
    from backend.core.loop.task_contract import preview_contract, validate_contract_proposal, get_task_contract, TaskContractRevisionConflict
    process = ctx.process
    available_tools = ctx.available_tools
    workspace_path = ctx.workspace_path
    tools = ctx.tools
    manager = ctx.manager
    engineering = ctx.engineering
    def bind_arguments(args):
        """Lower a semantic delta to the existing version-bound proposal.

        Pipeline prepares this once before admission; publication receives that
        exact full proposal, never a fresh merge after a concurrent change.
        The same binder is used for the earlier read-only semantic preview.
        """
        if not isinstance(args, dict):
            raise ValueError('task contract arguments must be an object')
        args = deepcopy(args)
        semantic_update = {key: args.pop(key) for key in ('plan', 'plan_review', 'related_review') if key in args}
        if semantic_update or args.get('maintenance') is not None:
            from backend.core.loop.task_maintenance import state_for
            update = args.get('maintenance')
            if update is None:
                update = {}
            if not isinstance(update, dict):
                raise ValueError('maintenance must be an object')
            for key, value in semantic_update.items():
                if key in update and update[key] != value:
                    raise ValueError('Conflicting task update: ' + key)
                update[key] = value
            update.setdefault('expected_revision', int(state_for(process).get('revision') or 0))
            args['maintenance'] = update
        operation = args.get('operation', 'declare')
        if operation not in {'declare', 'revise'}:
            raise ValueError('task contract operation must be declare or revise')
        if operation == 'declare':
            if 'append' in args:
                raise ValueError('append requires operation=revise')
            if not get_task_contract(process).get('revision'):
                # Initial absence proposes no additional uncertainty; it does
                # not resolve existing questions or certify semantic readiness.
                # Existing contracts use explicit revise/retain semantics.
                args.setdefault('uncertainties', [])
                args.setdefault('requires_user_decision', bool(args['uncertainties']))
                args.setdefault('delegation_required', args.get('execution_mode') == 'delegate')
                args.setdefault('user_requested_delegation', False)
                args.setdefault('user_request_evidence', '')
            return bind_workflow({key: deepcopy(value) for key, value in args.items() if key != 'operation'})
        schema = tools['declare_task_contract'].parameters
        properties = schema['properties']
        unknown = set(args) - set(properties) - {'operation', 'append'}
        if unknown:
            raise ValueError('Unsupported revision fields: ' + ', '.join(sorted(unknown)))
        current = get_task_contract(process)
        revision = int(current.get('revision') or 0)
        if revision == 0:
            raise ValueError('TASK_CONTRACT_INITIAL_DECLARATION_REQUIRED: declare the initial task before revising it')
        expected = args.get('expected_revision', revision)
        if type(expected) is not int or expected != revision:
            raise TaskContractRevisionConflict('TASK_CONTRACT_REVISION_CONFLICT: reconcile the current contract before revising it')
        edits = {key: deepcopy(value) for key, value in args.items()
                 if key not in {'operation', 'append', 'expected_revision'}}
        additions = args.get('append', {})
        if not isinstance(additions, dict) or set(additions) - APPEND_FIELDS:
            raise ValueError('append supports only: ' + ', '.join(sorted(APPEND_FIELDS)))
        if not edits and not any(additions.values()):
            raise ValueError('TASK_CONTRACT_CHANGE_REQUIRED: supply changed fields or appended requirements')
        # Active workflow/mechanism omission is handled by the original compiler;
        # do not replay an earlier maintenance transaction or private Host facts.
        inherited = {key: deepcopy(current[key]) for key in properties if key in current
            and key not in {'engineering_workflow', 'mechanism', 'maintenance', 'preview_digest', 'expected_revision'}}
        for key, values in additions.items():
            if key in edits:
                raise ValueError('Cannot both replace and append the same field: ' + key)
            if not isinstance(values, list):
                raise ValueError('append.' + key + ' must be an array')
            retained = inherited.setdefault(key, [])
            for value in values:
                if value not in retained:
                    retained.append(deepcopy(value))
        return bind_workflow({**inherited, **edits, 'expected_revision': revision})

    def bind_workflow(args):
        workflow = args.get('engineering_workflow')
        if isinstance(workflow, dict) and 'nodes' not in workflow and 'target_files' not in workflow:
            # Exact already-proposed deliverable paths, not prose inference.
            files = [d['path'] for d in args.get('deliverables', []) if isinstance(d, dict)
                and d.get('kind') == 'workspace_file' and d.get('path')]
            if files and 'tdd' in (workflow.get('profiles') or []):
                workflow['target_files'] = list(dict.fromkeys(files))
        for item in args.get('deliverables', []):
            if isinstance(item, dict):
                item.setdefault('required', True)
        return args

    APPEND_FIELDS = {'acceptance_criteria', 'deliverables', 'uncertainties', 'required_tool_calls', 'adopt_process_ids'}
    def proposal_for(args):
        args = bind_arguments(args)
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

    def preview_proposal(args):
        # Public proposal scheduling must validate the same bound arguments as
        # Pipeline. Direct legacy application callers retain compiler defaults.
        from backend.core.loop.tool_pipeline import _validate_args
        bound = bind_arguments(args)
        _validate_args(bound, tools['declare_task_contract'].parameters)
        return preview_contract(process, proposal_for(bound))

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
        model_result=contract_feedback,
        name="declare_task_contract",
        prepare_args=bind_arguments,
        alignment_contract=preview_proposal,
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
                    "description": "Required for a legacy full replacement; optional exact base for operation=revise, otherwise Runtime binds the current revision. Stale proposals require reconciliation."},
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
                "engineering_workflow": plan_schema(),
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

    # One implementation and execution schema; discovery also describes the
    # concise revision form. Old saved full declarations remain compatible.
    contract_tool = tools['declare_task_contract']
    advertised = deepcopy(contract_tool.parameters)
    # The task-state consumer graph is a compatibility representation owned by
    # the runtime. New callable lifecycle definitions use harness_mechanism;
    # do not ask the model to duplicate one program into two different IRs.
    for legacy in ('mechanism', 'preview_digest'):
        advertised['properties'].pop(legacy)
    for field in ('plan', 'plan_review', 'related_review'):
        advertised['properties'][field] = deepcopy(maintenance_schema()['properties'][field])
    advertised['properties']['maintenance']['required'] = []
    advertised['properties']['maintenance']['description'] = 'Reconcile actual adopted changes and dependency failures. Runtime binds the current maintenance revision unless an exact expected_revision is supplied.'
    required_initial = advertised.pop('required')
    optional_initial = {'delegation_required', 'uncertainties', 'requires_user_decision',
        'user_requested_delegation', 'user_request_evidence'}
    required_initial = [key for key in required_initial if key not in optional_initial]
    advertised['properties']['deliverables']['items']['required'] = ['kind']
    advertised['properties']['uncertainties']['description'] = 'Explicit unresolved choices. On initial declare omission proposes none; it never clears existing questions. revise preserves omission. If nonempty and requires_user_decision is omitted initially, Runtime retains a decision requirement.'
    advertised['properties'].update({
        'operation': {'type': 'string', 'enum': ['declare', 'revise'],
            'description': 'Default declare supplies the initial full contract. revise supplies only changed fields; omitted obligations are retained and Runtime binds the current revision.'},
        'append': {'type': 'object', 'additionalProperties': False,
            'description': 'With revise, append exact additional obligations without copying existing arrays. Do not also replace the same field.',
            'properties': {key: deepcopy(contract_tool.parameters['properties'][key]) for key in sorted(APPEND_FIELDS)}}})
    advertised['anyOf'] = [{'required': required_initial},
        {'required': ['operation'], 'properties': {'operation': {'const': 'revise'}}}]
    contract_tool.advertised_parameters = advertised


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
                process=process,
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
