"""dynamic and completion protocol tools."""
from __future__ import annotations
from backend.core.loop.execution_contract import HOST_COMPUTE, data_broker
from backend.core.loop.agent_tool import AgentTool, ApprovalMode, CancellationMode, ToolEffect
from backend.core.loop.completion_protocol import COMPLETE_TASK_PARAMETERS, CompletionClaim

def install(ctx):
    process = ctx.process
    available_tools = ctx.available_tools
    workspace_path = ctx.workspace_path
    tools = ctx.tools
    if process.tool_registry is not None and process.tool_registry.has("define_tool"):
        def define_tool(args: dict) -> dict:
            from backend.core.tools.dynamic_tools import validate_definition

            operation = str(args.get("operation", "define") or "define").strip().lower()
            if operation == "list":
                return {
                    "operation": "list",
                    "tools": [
                        {
                            "name": item.name,
                            "effect": item.effect.value,
                            "version": (item.composite_spec or {}).get("version", 1),
                            "digest": (item.composite_spec or {}).get("digest", ""),
                            "steps": [
                                step.get("id")
                                for step in (item.composite_spec or {}).get("steps", [])
                            ],
                        }
                        for item in process.dynamic_tools.values()
                    ],
                }
            if operation == "revoke":
                name = str(args.get("name", "")).strip()
                if name not in process.dynamic_tools:
                    return {"revoked": False, "error": "dynamic tool not found", "name": name}
                previous = process.dynamic_tools.pop(name)
                process.tool_registry.remove_scoped(name)
                return {
                    "revoked": True,
                    "name": name,
                    "version": (previous.composite_spec or {}).get("version", 1),
                    "digest": (previous.composite_spec or {}).get("digest", ""),
                }
            if operation not in {"define", "replace"}:
                return {"defined": False, "error": "operation must be define, replace, list, or revoke"}

            authorized_base = {
                name: tool for name, tool in available_tools.items()
                if process.tool_registry is not None and process.tool_registry.has(name)
            }
            authorized_base.update(process.dynamic_tools)
            name = str(args.get("name", "")).strip()
            existing = process.dynamic_tools.get(name)
            if operation == "replace":
                if existing is None:
                    return {"defined": False, "error": "only a dynamic tool can be replaced"}
                authorized_base.pop(name, None)
            elif name in process.dynamic_tools:
                return {"defined": False, "error": "dynamic tool already exists; use operation=replace"}
            compiled_input = dict(args)
            compiled_input["_version"] = (
                int((existing.composite_spec or {}).get("version", 1)) + 1
                if existing is not None else 1
            )
            try:
                spec = validate_definition(compiled_input, authorized_base)
            except (ValueError, PermissionError) as exc:
                return {"error": str(exc), "defined": False}

            dynamic = AgentTool(
                execution_contract=data_broker("host.composite"),
                name=spec["name"],
                description=spec["description"],
                parameters=spec["parameters"],
                execute=lambda _args: {"error": "DYNAMIC_TOOL_REQUIRES_PIPELINE"},
                read_only=spec["effect"] == "read",
                resources=spec["resources"],
                timeout=spec["timeout"],
                effect=spec["effect"],
                cancellation=CancellationMode.COOPERATIVE,
                composite_spec=spec,
            )
            process.dynamic_tools[dynamic.name] = dynamic
            process.tool_registry.add_scoped(dynamic.name)
            return {
                "defined": True,
                "name": dynamic.name,
                "effect": spec["effect"],
                "resources": spec["resources"],
                "version": spec["version"],
                "digest": spec["digest"],
                "execution_mode": spec["execution_mode"],
                "steps": [item["id"] for item in spec["steps"]],
            }

        tools["define_tool"] = AgentTool(
            execution_contract=data_broker("host.runtime.define_tool"),
            name="define_tool",
            description=(
                "Define one task-scoped composite tool from tools already authorized in "
                "this capability. It cannot add authority or execute arbitrary Python."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["define", "replace", "list", "revoke"],
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "parameters": {"type": "object"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "tool": {"type": "string"},
                                "arguments": {"type": "object"},
                            },
                            "required": ["tool", "arguments"],
                        },
                    },
                },
                "required": [],
            },
            execute=define_tool,
            read_only=False,
            effect=ToolEffect.PROCESS,
        )


    if process.tool_registry is not None and process.tool_registry.has("author_tool"):
        def author_tool(args: dict) -> dict:
            from backend.core.errors import error_payload
            from backend.core.tools.dynamic_tools import (
                validate_authored_definition, validate_privileged_authored_definition, validate_host_authored_definition,
            )
            from backend.core.storage import get_storage
            operation = str(args.get("operation", "register") or "register").lower()
            storage = process.bound_storage
            if storage is None:
                storage = get_storage(workspace_path)

            def authored_error(name: str, *, message: str = "", details: dict | None = None,
                               next_actions: list[dict] | None = None, **state) -> dict:
                return {
                    **error_payload(
                        name, message=message, details=details,
                        next_actions=next_actions,
                    ),
                    **state,
                }

            def stored_error(exc: BaseException, **state) -> dict:
                raw = str(exc).strip("'\"")
                symbolic = raw.split(":", 1)[0]
                name = symbolic if symbolic in {
                    "CUSTOM_TOOL_NOT_FOUND", "CUSTOM_TOOL_ARCHIVED",
                    "CUSTOM_TOOL_PRIVACY_BLOCKED",
                    "CUSTOM_TOOL_SOURCE_INTEGRITY_FAILED",
                } else "CUSTOM_TOOL_INVALID"
                actions = {
                    "CUSTOM_TOOL_NOT_FOUND": [{"action": "list_saved_tools"}],
                    "CUSTOM_TOOL_ARCHIVED": [{"action": "restore_then_mount"}],
                    "CUSTOM_TOOL_PRIVACY_BLOCKED": [{"action": "remove_sensitive_content"}],
                    "CUSTOM_TOOL_SOURCE_INTEGRITY_FAILED": [{"action": "inspect_storage"}],
                }.get(name, [{"action": "revise_definition_then_retry"}])
                return authored_error(name, message=raw, next_actions=actions, **state)

            def mount(saved: dict) -> dict:
                spec = dict(saved.get("spec") or {})
                spec["source_ref"] = str(saved.get("source_ref") or "")
                name = str(spec.get("name") or saved.get("name") or "")
                privileged = spec.get("execution_mode") == "authored_privileged_python"
                from ..tool_surface import component_blocker
                blocker = component_blocker(process, spec, {**available_tools, **process.dynamic_tools})
                if blocker:
                    process.dynamic_tools.pop(name, None)
                    process.tool_registry.remove_scoped(name)
                    return {**blocker, 'mounted': False, 'name': name, 'saved_asset_retained': True}
                from ..dynamic_mount import mounted_tool
                dynamic = mounted_tool(spec)
                process.dynamic_tools[name] = dynamic
                process.tool_registry.add_scoped(name)
                return {
                    "mounted": True, "name": name,
                    "version": int(saved.get("version") or spec.get("version") or 1),
                    "digest": str(saved.get("digest") or spec.get("digest") or ""),
                    "scope": "current_task",
                    "authority": "user_approved_privileged" if privileged else "host_leaf_pipeline" if spec["execution_mode"] == "authored_host_python" else "pure_transform_only",
                }

            if operation == "list":
                catalog = storage.list_custom_tools(
                    include_archived=bool(args.get("include_archived", False)),
                )
                mounted = {
                    name for name, tool in process.dynamic_tools.items()
                    if (tool.composite_spec or {}).get("execution_mode") in {
                        "authored_pure_python", "authored_privileged_python", "authored_host_python",
                    }
                }
                for item in catalog.get("tools", []):
                    item["mounted"] = item.get("name") in mounted
                return catalog
            if operation in {"revoke", "detach"}:
                name = str(args.get("name") or "")
                tool = process.dynamic_tools.get(name)
                if tool is None or (tool.composite_spec or {}).get("execution_mode") not in {
                    "authored_pure_python", "authored_privileged_python", "authored_host_python",
                }:
                    return authored_error(
                        "CUSTOM_TOOL_NOT_FOUND",
                        message="This custom tool is not mounted in the current task.",
                        next_actions=[{"action": "list_mounted_tools"},
                                      {"action": "use_saved_tool"}],
                        revoked=False,
                    )
                process.dynamic_tools.pop(name, None)
                process.tool_registry.remove_scoped(name)
                return {"revoked": True, "detached": True, "name": name,
                        "saved_asset_retained": True}
            if operation == "use":
                try:
                    saved = storage.load_custom_tool(
                        str(args.get("name") or ""),
                        version=(int(args["version"]) if args.get("version") else None),
                    )
                    return mount(saved)
                except (KeyError, ValueError, RuntimeError, OSError) as exc:
                    return stored_error(exc, mounted=False)
            if operation in {"delete", "archive", "restore"}:
                name = str(args.get("name") or "")
                try:
                    changed = storage.set_custom_tool_archived(
                        name, archived=operation != "restore",
                    )
                except (KeyError, ValueError, RuntimeError) as exc:
                    return stored_error(exc, changed=False)
                if operation != "restore":
                    process.dynamic_tools.pop(name, None)
                    process.tool_registry.remove_scoped(name)
                return {"changed": True, **changed,
                        "versions_retained": True}
            if operation not in {"register", "replace"}:
                return authored_error(
                    "CUSTOM_TOOL_INVALID",
                    message=("operation must be register, replace, use, list, detach, "
                             "delete, archive, or restore"),
                    next_actions=[{"action": "retry", "valid_operations": [
                        "register", "replace", "use", "list", "detach",
                        "delete", "archive", "restore",
                    ]}],
                )
            name = str(args.get("name") or "")
            catalog = storage.list_custom_tools(include_archived=True)
            persistent = next(
                (item for item in catalog.get("tools", []) if item.get("name") == name),
                None,
            )
            if operation == "register" and persistent is not None:
                return authored_error(
                    "CUSTOM_TOOL_EXISTS", next_actions=[{"action": "replace"}],
                    registered=False,
                )
            if operation == "replace" and persistent is None:
                return authored_error(
                    "CUSTOM_TOOL_NOT_FOUND", next_actions=[{"action": "register"}],
                    registered=False,
                )
            compiled = dict(args)
            compiled["_version"] = int((persistent or {}).get("version", 0) or 0) + 1
            try:
                privileged = str(args.get("authority_mode") or "pure") == "privileged"
                spec = (
                    validate_privileged_authored_definition(compiled, workspace_path)
                    if privileged else validate_host_authored_definition(compiled, workspace_path, available_tools)
                    if str(args.get("authority_mode") or "pure") == "host" else validate_authored_definition(compiled, workspace_path)
                )
            except (ValueError, PermissionError, OSError) as exc:
                return authored_error(
                    "CUSTOM_TOOL_INVALID", message=str(exc),
                    next_actions=[{"action": "revise_definition_then_retry"}],
                    registered=False,
                )
            if privileged:
                from backend.core.loop.permission_broker import consume_exact_grant
                grant = consume_exact_grant(process, "author_tool", args)
                if grant is None:
                    return authored_error(
                        "CUSTOM_TOOL_PRIVILEGED_APPROVAL_REQUIRED",
                        message=(
                            "Privileged source can access the process, filesystem, imports or network. "
                            "Ask the user to approve this exact author_tool registration before running tests."
                        ),
                        details={
                            "name": name, "source_sha256": spec["source_sha256"],
                            "effect": spec["effect"], "resources": spec["resources"],
                        },
                        next_actions=[{
                            "action": "request_permission", "tool_name": "author_tool",
                            "arguments": {
                                key: value for key, value in args.items()
                                if not str(key).startswith("_")
                            },
                            "resource": "capability://author_tool",
                            "purpose": str(spec.get("purpose") or "register and test this privileged custom tool"),
                        }],
                        registered=False,
                    )
            # Registration tests run in the cancellable subprocess boundary;
            # model-authored code never executes in the daemon process.
            from backend.core.loop.process_tool_runner import ProcessToolRunner
            for index, test in enumerate(spec["tests"], 1):
                checked = ProcessToolRunner(timeout=spec["timeout"]).run(
                    "authored_privileged_python" if privileged else "authored_host_python"
                    if spec["execution_mode"] == "authored_host_python" else "authored_python",
                    {**dict(test["input"]), "_workspace": workspace_path,
                     "_dynamic_spec": spec, "_host_results": test.get("host_results") or []},
                    cancellation_event=process.cancellation_event,
                )
                if not checked.success:
                    return authored_error(
                        "CUSTOM_TOOL_INVALID",
                        message="The authored tool registration test crashed or timed out.",
                        details={"test": index, "diagnostic": checked.error},
                        next_actions=[{"action": "revise_source_then_retry"}],
                        test=index, registered=False,
                    )
                actual = checked.data or {}
                if spec["execution_mode"] == "authored_host_python":
                    if actual.get("state") != "completed":
                        return authored_error("CUSTOM_TOOL_INVALID", message="Host-call registration tests need complete mock host_results; no live effects run during registration.", registered=False)
                    actual = actual["output"]
                if actual != test["expected"]:
                    return authored_error(
                        "CUSTOM_TOOL_INVALID",
                        message="The authored tool registration test returned an unexpected result.",
                        details={"test": index, "expected": test["expected"],
                                 "actual": actual},
                        next_actions=[{"action": "revise_source_or_expected_result"}],
                        test=index, expected=test["expected"], actual=actual,
                        registered=False,
                    )
            source = str(spec.pop("_source"))
            try:
                saved = storage.save_custom_tool(
                    spec, source, replace=operation == "replace",
                )
                loaded = storage.load_custom_tool(name)
            except Exception as exc:
                return stored_error(exc, registered=False, saved=False)
            mounted = mount(loaded)
            if mounted.get('error') == 'CUSTOM_TOOL_CAPABILITY_REQUIRED':
                # Registration and mock self-tests succeeded. The persistent
                # asset exists; a missing capability is a mount blocker, not
                # an error that falsely claims registration had no effects.
                mounted['blocked_on'] = mounted.pop('error')
                mounted.pop('error_info', None)
                mounted['warnings'] = [{'code': 'CUSTOM_TOOL_CAPABILITY_REQUIRED', 'message': mounted['detail']}]
            return {"registered": True, **saved, **mounted,
                    "tests_passed": len(spec["tests"])}

        tools["author_tool"] = AgentTool(
            execution_contract=data_broker("host.runtime.author_tool"),
            name="author_tool",
            description=(
                "Manage project-saved Python shortcuts. Pure mode is statically confined. "
                "Host mode keeps pure code but supports call_tool(name, arguments) for declared host_tools; "
                "every leaf uses the canonical Host pipeline and exact permission, and resumes without replaying effects. "
                "Registration tests supply mock host_results (tool_name,arguments_digest,result); no live effects run. "
                "Privileged mode may use imports, files, processes or network, but binds the "
                "source digest and registration contract to explicit user approval, and every "
                "later invocation requires a new exact approval. register/replace creates an "
                "immutable tested version; use mounts it for this task; detach keeps the asset; "
                "delete archives it; restore reactivates it."
            ),
            parameters={"type": "object", "properties": {
                "operation": {"type": "string", "enum": [
                    "register", "replace", "use", "list", "detach", "revoke",
                    "delete", "archive", "restore",
                ]},
                "name": {"type": "string"}, "description": {"type": "string"},
                "parameters": {"type": "object"}, "source_path": {"type": "string"},
                "source_sha256": {"type": "string"},
                "host_tools": {"type": "array", "items": {"type": "string"}},
                "authority_mode": {"type": "string", "enum": ["pure", "host", "privileged"]},
                "purpose": {"type": "string"},
                "effect": {"type": "string", "enum": ["workspace_write", "process", "external"]},
                "resources": {"type": "array", "items": {"type": "string"}},
                "tests": {"type": "array", "items": {"type": "object"}},
                "timeout": {"type": "number"}, "version": {"type": "integer"},
                "include_archived": {"type": "boolean"},
            }, "required": []},
            execute=author_tool, read_only=False, effect=ToolEffect.PROCESS,
            approval_per_invocation=True,
        )


    if process.actor_kind == "reviewer":
        def complete_review(args: dict) -> dict:
            verdict = str(args.get("verdict", "")).strip()
            summary = str(args.get("summary", "")).strip()
            if verdict not in {"approved", "changes_required"}:
                return {"error": "verdict must be approved or changes_required"}
            if not summary:
                return {"error": "review summary is required"}
            process.review_claim = {
                "verdict": verdict,
                "summary": summary,
                "findings": list(args.get("findings", []) or []),
                "evidence": list(args.get("evidence", []) or []),
                "target_process_id": process.review_target_id,
            }
            return {"accepted": True, **process.review_claim}

        tools["complete_review"] = AgentTool(
            execution_contract=data_broker("host.runtime.complete_review"),
            name="complete_review",
            description="Submit the independent review verdict and evidence to the host gate.",
            parameters={
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": ["approved", "changes_required"]},
                    "summary": {"type": "string"},
                    "findings": {"type": "array", "items": {"type": "object"}},
                    "evidence": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["verdict", "summary", "findings", "evidence"],
            },
            execute=complete_review,
            read_only=True,
            effect=ToolEffect.READ,
        )


    def complete_task(args: dict) -> dict:
        try:
            claim = CompletionClaim.from_args(args, step=process.steps_used)
        except ValueError as exc:
            return {"error": str(exc), "accepted": False}
        process.completion_claim = claim
        return {"accepted": True, "claim": claim.to_dict()}


    tools["complete_task"] = AgentTool(
        execution_contract=data_broker("host.runtime.complete_task"),
        name="complete_task",
        description=(
            "Submit the action result and verification evidence for deterministic "
            "host evaluation. This does not self-approve completion."
        ),
        parameters=COMPLETE_TASK_PARAMETERS,
        execute=complete_task,
        read_only=True,
        effect=ToolEffect.READ,
        idempotent=False,
    )
