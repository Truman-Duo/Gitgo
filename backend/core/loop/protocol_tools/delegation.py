"""delegation protocol tools."""
from __future__ import annotations

from backend.core.prompt_templates import load_prompt
from pathlib import Path
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.capabilities import CapabilityProfiles

def install(ctx):
    process = ctx.process
    workspace_path = ctx.workspace_path
    tools = ctx.tools
    _spawn_child = ctx.spawn_child
    delegate_task = ctx.delegate_task
    def delegate_task(args: dict) -> dict:
        from backend.core.errors import error_payload
        from backend.core.loop.task_contract import (
            delegation_intent_id,
            finish_delegation_attempt,
            record_delegation_attempt,
        )
        task_description = str(args.get("task_description", "")).strip()
        criteria = [
            str(item) for item in (args.get("acceptance_criteria", []) or [])
            if str(item).strip()
        ]
        target_files = [
            str(item) for item in (args.get("target_files", []) or [])
            if str(item).strip()
        ]
        profile_id = str(
            args.get("capability_profile_id") or "development.workspace"
        )
        raw_task_kind = str(args.get("task_kind", "")).strip()
        required_for_parent = bool(
            args.get("required_for_parent_completion", True)
        )
        intent_id = delegation_intent_id(
            task_description, target_files, criteria,
        )
        record_delegation_attempt(
            process,
            intent_id=intent_id,
            required=required_for_parent,
            profile_id=profile_id,
            task_kind=raw_task_kind,
            target_files=target_files,
        )

        def reject(name: str, message: str, *, details: dict | None = None,
                   next_actions: list[dict] | None = None) -> dict:
            payload = error_payload(
                name, message=message, details=details,
                next_actions=next_actions, state_changed=True,
            )
            finish_delegation_attempt(
                process, intent_id=intent_id, state="admission_failed",
                error_info=payload["error_info"],
            )
            return {
                "delegated": False,
                "intent_recorded": True,
                "intent_id": intent_id,
                **payload,
            }

        try:
            from backend.core.loop.task_contract import get_task_contract, routing_advice
            current_contract = get_task_contract(process)
            host_requirements = current_contract.get("host_requirements") or {}
            if (
                process.task_kind == "answer"
                and not current_contract.get("revision")
                and not bool(host_requirements.get("manual_B_creation"))
            ):
                return reject(
                    "DELEGATION_ADMISSION_FAILED",
                    "Automatic delegation requires a Host-validated task contract first; declare the semantic contract, then follow its routing advice.",
                    details={
                        "reason": "task_contract_not_declared",
                        "execution_state": "not_started",
                    },
                    next_actions=[{
                        "action": "declare_task_contract",
                        "reason": (
                            "Describe the deliverables, acceptance criteria, complexity, "
                            "independent workstreams and delegation rationale before routing."
                        ),
                    }],
                )
            route = routing_advice(current_contract)
            if (
                current_contract.get("revision")
                and
                route.get("recommended") == "self_execute"
                and not bool(host_requirements.get("manual_B_creation"))
            ):
                return reject(
                    "DELEGATION_ADMISSION_FAILED",
                    "The current structured contract describes bounded single-owner work; use request_self_execute, or revise the contract with a concrete complexity rationale.",
                    details={"routing_advice": route},
                    next_actions=[
                        {"action": "request_self_execute", "profile_id": "development.workspace"},
                        {"action": "revise_task_contract", "required": ["estimated_complexity", "delegation_rationale"]},
                    ],
                )
            profile_error = CapabilityProfiles.worker_error(profile_id)
            if profile_error:
                info = profile_error["error_info"]
                return reject(info["name"], info["message"], details=info["details"],
                              next_actions=info["next_actions"])
            profile = CapabilityProfiles.get(profile_id)
            task_kind = raw_task_kind or (
                "action" if profile.allows_self_execution else "answer"
            )
            if task_kind not in {"answer", "plan", "action"}:
                return reject(
                    "DELEGATION_ADMISSION_FAILED",
                    "delegate_task only accepts answer, plan, or action task kinds.",
                    details={
                        "attempted_value": task_kind,
                        "valid_task_kinds": ["answer", "plan", "action"],
                    },
                    next_actions=[{
                        "action": "retry",
                        "patch": {"task_kind": (
                            "action" if profile.allows_self_execution else "answer"
                        )},
                    }],
                )
            if task_kind == "action" and not profile.allows_self_execution:
                return reject(
                    "DELEGATION_ADMISSION_FAILED",
                    f"Capability profile {profile_id} cannot satisfy an action task.",
                    details={"profile_id": profile_id, "task_kind": task_kind},
                    next_actions=[{
                        "action": "retry",
                        "patch": {
                            "capability_profile_id": "development.workspace",
                            "task_kind": "action",
                        },
                    }],
                )
            if not task_description:
                return reject(
                    "DELEGATION_ADMISSION_FAILED",
                    "task_description is required.",
                    details={"field": "task_description"},
                )
            if criteria:
                task_description += "\n\nAcceptance criteria:\n- " + "\n- ".join(criteria)
            explicit_required_tests = [
                str(item) for item in (args.get("required_test_ids", []) or [])
                if str(item).strip()
            ]
            from backend.core.loop.test_manifest import extract_declared_test_ids
            declared_required_tests = extract_declared_test_ids(task_description)
            inherited_required_tests = (
                list(process.required_test_ids)
                if task_kind == "action" else []
            )
            required_tests = list(dict.fromkeys(
                explicit_required_tests
                + declared_required_tests
                + inherited_required_tests
            ))
            required_tool_calls = [
                dict(item or {})
                for item in (args.get("required_tool_calls", []) or [])
            ]
            allowed_tools = [
                str(item) for item in (args.get("allowed_tools", []) or [])
                if str(item).strip()
            ]
            from backend.core.loop.interface_contract import (
                capture_declared_contract,
                normalise_interface_ref,
            )
            input_interfaces = list(dict.fromkeys(
                normalise_interface_ref(item)
                for item in (args.get("input_interfaces") or [])
            ))
            output_interfaces = list(dict.fromkeys(
                normalise_interface_ref(item)
                for item in (args.get("output_interfaces") or [])
            ))
            interface_contract = dict(args.get("_interface_contract") or {})
            if not interface_contract and (input_interfaces or output_interfaces):
                interface_contract = capture_declared_contract(
                    Path(workspace_path or process.worktree_path).resolve(),
                    [{
                        "node_id": "direct",
                        "input_interfaces": input_interfaces,
                        "output_interfaces": output_interfaces,
                    }],
                )
            child = _spawn_child(
                task_description=task_description,
                profile_id=profile_id,
                task_kind=task_kind,
                max_steps=int(args.get("max_steps", 50) or 50),
                target_files=target_files,
                acceptance_criteria=criteria,
                allowed_tools=allowed_tools,
                tool_scope_mode=str(args.get("tool_scope_mode", "profile")),
                required_test_ids=required_tests,
                required_tool_calls=required_tool_calls,
                depends_on=[
                    str(item) for item in (args.get("depends_on", []) or [])
                ],
                input_interfaces=input_interfaces,
                output_interfaces=output_interfaces,
                interface_contract=interface_contract,
                required_for_parent_completion=required_for_parent,
                supersedes_process_id=str(args.get("supersedes_process_id", "")),
                continue_process_id=str(args.get("continue_process_id", "")),
                display_name=str(args.get("display_name", "")),
                budget_request=dict(args.get("budget_request") or {}),
            )
            finish_delegation_attempt(
                process, intent_id=intent_id, state="running",
                child_process_id=child.process_id,
            )
            return {
                "delegated": True,
                "intent_id": intent_id,
                "process_id": child.process_id,
                "session_id": child.session.session_id,
                "display_name": child.session.display_name,
                "status": child.status.value,
                "profile_id": profile_id,
                "budget_lease": dict(child.budget_lease or {}),
            }
        except (ValueError, PermissionError, RuntimeError) as exc:
            return reject(
                "DELEGATION_ADMISSION_FAILED",
                str(exc),
                details={
                    "profile_id": profile_id,
                    "task_kind": raw_task_kind,
                    "valid_worker_profiles": CapabilityProfiles.describe(
                        actor_kind="worker"
                    ),
                },
            )


    tools["delegate_task"] = AgentTool(
        name="delegate_task",
        description=(
            load_prompt('tools/delegate_task')
            + (load_prompt('tools/delegate_task_owner')
               if process.runtime_preferences.get("agent_routing", "owner") == "owner"
               else load_prompt('tools/delegate_task_fresh'))
            + "display_name is an optional short human-readable worker name."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_description": {"type": "string"},
                "capability_profile_id": {
                    "type": "string",
                    "enum": list(CapabilityProfiles.ids(actor_kind="worker")),
                    "default": "development.workspace",
                },
                "task_kind": {
                    "type": "string",
                    "enum": ["answer", "plan", "action"],
                },
                "target_files": {"type": "array", "items": {"type": "string"}},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "required_test_ids": {"type": "array", "items": {"type": "string"}},
                "required_tool_calls": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool_name": {"type": "string"},
                            "min_calls": {"type": "integer"},
                            "max_calls": {"type": "integer"},
                            "include_composite_steps": {"type": "boolean"},
                        },
                        "required": ["tool_name"],
                    },
                },
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "input_interfaces": {"type": "array", "items": {"type": "string"}},
                "output_interfaces": {"type": "array", "items": {"type": "string"}},
                "allowed_tools": {"type": "array", "items": {"type": "string"}},
                "tool_scope_mode": {
                    "type": "string", "enum": ["profile", "test_only"],
                },
                "required_for_parent_completion": {"type": "boolean"},
                "supersedes_process_id": {"type": "string"},
                "continue_process_id": {"type": "string"},
                "display_name": {"type": "string", "maxLength": 48},
                "max_steps": {"type": "integer"},
                "budget_request": {
                    "type": "object",
                    "description": (
                        "Optional child escrow request. Omit for the deterministic default; "
                        "request more only when the task contract explains why."
                    ),
                    "properties": {
                        "provider_calls": {"type": "integer", "minimum": 1},
                        "output_tokens": {"type": "integer", "minimum": 256},
                        "steps": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["task_description", "target_files", "acceptance_criteria"],
        },
        execute=delegate_task,
        read_only=True,
        effect=ToolEffect.READ,
    )


    ctx.delegate_task = delegate_task
