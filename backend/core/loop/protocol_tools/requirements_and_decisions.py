"""requirements and decisions protocol tools."""
from __future__ import annotations

from backend.core.prompt_templates import load_prompt
from backend.core.loop.execution_contract import HOST_COMPUTE, data_broker
from backend.core.loop.agent_tool import AgentTool, ApprovalMode, ToolEffect
from backend.core.loop.code_dossier import build_code_dossier
from backend.core.loop.decision_support import collect_decision_evidence

def install(ctx):
    process = ctx.process
    available_tools = ctx.available_tools
    workspace_path = ctx.workspace_path
    on_stream_event = ctx.on_stream_event
    tools = ctx.tools
    manager = ctx.manager
    engineering = ctx.engineering
    code_dossier = ctx.code_dossier
    dossier_store = ctx.dossier_store
    from backend.core.loop.engineering_workflow import EngineeringWorkflow


    engineering = EngineeringWorkflow(process, on_stream_event)


    tools["engineering_workflow"] = AgentTool(
        execution_contract=data_broker("host.runtime.engineering_workflow"),
        name="engineering_workflow",
        description=(
            load_prompt('tools/engineering_workflow')
        ),
        parameters={"type": "object", "properties": {
            "operation": {"type": "string", "enum": ["configure", "status", "ask", "ask_frontier", "revise_decision", "resolve_answer", "record", "retrospective", "propose_amendment"]},
            "plan": {"type": "object"}, "node_id": {"type": "string"},
            "decision_id": {"type": "string"},
            "content": {"type": "object"}, "request": {"type": "object"}, "reason": {"type": "string"},
        }, "required": []},
        execute=engineering.invoke, read_only=True, effect=ToolEffect.READ,
        idempotent=False,
    )


    if workspace_path:
        from backend.core.loop.code_dossier import (
            build_code_dossier,
            build_shard_dossier,
            suggest_task_shards,
        )
        from backend.core.loop.context_store import ContextObjectStore

        dossier_store = ContextObjectStore(workspace_path)

        def code_dossier(args: dict) -> dict:
            dossier = build_code_dossier(
                workspace_path,
                [str(item) for item in (args.get("target_files", []) or [])],
                max_files=int(args.get("max_files", 240) or 240),
            )
            refs = dossier_store.put(
                f"code-dossiers/{dossier['digest']}",
                dossier,
                metadata={
                    "authority": "deterministic_host_facts",
                    "semantic_conclusion": False,
                },
            )
            return {
                **refs,
                "file_count": dossier["file_count"],
                "estimated_tokens": dossier["estimated_tokens"],
                "size_class": dossier["size_class"],
                "truncated": dossier["truncated"],
                "instruction": (
                    "Open the pinned ref when file-level facts are needed; do not "
                    "repeat whole-workspace enumeration."
                ),
            }

        tools["code_dossier"] = AgentTool(
            execution_contract=data_broker("host.runtime.code_dossier"),
            name="code_dossier",
            description=(
                load_prompt('tools/code_dossier')
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target_files": {"type": "array", "items": {"type": "string"}},
                    "max_files": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "required": ["target_files"],
            },
            execute=code_dossier,
            effect=ToolEffect.READ,
            idempotent=True,
        )


    from backend.core.loop.decision_support import (
        collect_decision_evidence,
        create_user_decision,
    )


    def lesson_checker(args):
        from backend.core.knowledge.checker import request_change
        from backend.core.knowledge.review import content_version
        from backend.core.storage import get_storage
        storage=getattr(process,"runtime_storage",None) or get_storage(workspace_path)
        found=storage.get_lesson_record(str(args.get("lesson_id") or ""))
        if found is None:return {"error":"LESSON_NOT_FOUND"}
        if args.get("operation","inspect")=="inspect":
            return {"lesson":found[1],"content_version":content_version(found[1]),"project_id":storage.paths.project_id,
                    "supported_check":"literal occurrence in complete edit_file/write_file candidate at exact project-relative paths"}
        if args.get("operation")!="request":return {"error":"INVALID_CHECKER_OPERATION"}
        return request_change(process,storage,workspace_path,args)

    tools["lesson_checker"] = AgentTool(execution_contract=data_broker("runtime.lesson_checker"), name="lesson_checker",description=load_prompt('tools/lesson_checker'),
        parameters={"type":"object","properties":{
            "operation":{"type":"string","enum":["inspect","request"]},"lesson_id":{"type":"string"},
            "expected_version":{"type":"string"},"action":{"type":"string","enum":["enable","disable"]},
            "violation_literal":{"type":"string","maxLength":256},"paths":{"type":"array","items":{"type":"string"},"maxItems":12}},
            "required":["lesson_id"],"additionalProperties":False},execute=lesson_checker,
        effect=ToolEffect.READ,read_only=True,idempotent=False)

    tools["decision_evidence"] = AgentTool(
        execution_contract=data_broker("host.runtime.decision_evidence"),
        name="decision_evidence",
        description=(
            load_prompt('tools/decision_evidence')
        ),
        parameters={
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "enum": ["all", "completion", "gates", "children", "tests", "governance", "budget", "capabilities", "engineering", "guidance"],
                },
                "identifier": {"type": "string", "maxLength": 120, "description": "For guidance only: a diagnostic catalog ID, symbolic name, or procedure name. Omit for the procedure index."},
            },
        },
        execute=lambda args: collect_decision_evidence(
            process, str(args.get("focus", "all")), identifier=str(args.get("identifier", ""))
        ),
        effect=ToolEffect.READ,
        idempotent=True,
    )


    def capability_status(_args: dict) -> dict:
        from backend.core.loop.capability_negotiation import build_capability_snapshot

        protocol = str(process.runtime_preferences.get("provider_protocol") or "unknown")
        snapshot = build_capability_snapshot(
            model_id=str(process.model_id or "unknown"),
            protocol=protocol,
            capabilities=process.runtime_preferences.get("provider_capabilities") or {},
            runtime_preferences=process.runtime_preferences,
            available_tools=set(process.tool_registry.list_all()),
        )
        if _args.get('capability', 'all') == 'all':
            from backend.core.loop.tool_surface import admission_snapshot, _select_authorized_tools
            catalog = {**available_tools, **process.dynamic_tools, **tools}
            active, _missing = _select_authorized_tools(process, catalog, issue_protocol_scopes=False)
            snapshot['tool_access'] = admission_snapshot(process, active, catalog)
        return snapshot


    tools["capability_status"] = AgentTool(
        execution_contract=data_broker("host.runtime.capability_status"),
        name="capability_status",
        description=(
            load_prompt('tools/capability_status')
        ),
        parameters={
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string", "enum": ["all", "web_access"],
                },
            },
        },
        execute=capability_status,
        effect=ToolEffect.READ,
        idempotent=True,
    )


    def configure_capability(args: dict) -> dict:
        """Apply one approved, deterministic Host capability configuration."""
        from urllib.parse import urlparse

        from backend.core.config import ConfigManager
        from backend.core.errors import error_payload
        from backend.core.loop.capability_negotiation import build_capability_snapshot

        capability = str(args.get("capability") or "").strip().lower()
        if capability != "web_search":
            return error_payload(
                "PROVIDER_CAPABILITY_UNAVAILABLE",
                message=f"Gitgo has no configurable adapter for capability {capability!r}.",
                details={"capability": capability, "supported": ["web_search"]},
                next_actions=[{"action": "choose_supported_capability"}],
            )
        mode = str(args.get("mode") or "").strip().lower()
        if mode not in {"auto", "provider", "searxng", "disabled"}:
            return error_payload(
                "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
                message="Choose auto, provider, searxng, or disabled for web search.",
                details={"mode": mode},
                next_actions=[{"action": "choose_search_mode"}],
            )
        cfg = ConfigManager.load()
        endpoint = str(args.get("endpoint") or cfg.web_search_endpoint or "").strip()
        engine = str(args.get("engine") or cfg.web_search_engine or "duckduckgo").lower()
        if endpoint:
            parsed = urlparse(endpoint)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return error_payload(
                    "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
                    message="The SearXNG endpoint must be an absolute HTTP(S) URL.",
                    details={"field": "endpoint"},
                    next_actions=[{"action": "provide_valid_endpoint"}],
                )
        if engine not in {"google", "bing", "baidu", "yandex", "duckduckgo"}:
            return error_payload(
                "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
                message="The selected SearXNG engine is not supported.",
                details={"engine": engine},
                next_actions=[{"action": "choose_search_engine"}],
            )
        caps = dict(process.runtime_preferences.get("provider_capabilities") or {})
        native = bool(caps.get("hosted_web_search", False)) or process.runtime_preferences.get("hosted_search_transport", "unavailable") != "unavailable"
        if mode == "provider" and not native:
            return error_payload(
                "PROVIDER_CAPABILITY_UNAVAILABLE",
                message=(
                    f"Model {process.model_id or 'unknown'} does not expose hosted web "
                    "search under the current API capability probe."
                ),
                details={
                    "capability": "hosted_web_search", "model_id": process.model_id,
                    "possible_provider_plan_difference": True,
                },
                next_actions=[
                    {"action": "switch_provider"},
                    {"action": "configure_searxng"},
                    {"action": "continue_offline"},
                ],
            )
        if mode == "searxng" and not endpoint:
            return error_payload(
                "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
                message="SearXNG mode requires an endpoint supplied by the user.",
                details={"missing": ["endpoint"]},
                next_actions=[{"action": "configure_searxng"}],
            )
        if mode == "auto" and not native and not endpoint:
            return error_payload(
                "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
                message=(
                    "Auto search needs either provider-hosted search or a configured "
                    "SearXNG endpoint."
                ),
                details={"model_id": process.model_id, "missing": ["fallback_endpoint"]},
                next_actions=[
                    {"action": "switch_provider"},
                    {"action": "configure_searxng"},
                    {"action": "continue_offline"},
                ],
            )
        cfg.web_search_mode = mode
        cfg.web_search_endpoint = endpoint
        cfg.web_search_engine = engine
        ConfigManager.save(cfg)
        process.runtime_preferences.update({
            "web_search_mode": mode,
            "web_search_endpoint": endpoint,
            "web_search_engine": engine,
        })
        snapshot = build_capability_snapshot(
            model_id=str(process.model_id or "unknown"),
            protocol=str(process.runtime_preferences.get("provider_protocol") or "unknown"),
            capabilities=caps,
            runtime_preferences=process.runtime_preferences,
            available_tools=set(process.tool_registry.list_all()),
        )
        return {
            "configured": True,
            "capability": capability,
            "mode": mode,
            "engine": engine,
            "endpoint_configured": bool(endpoint),
            "capability_snapshot": snapshot,
        }


    tools["configure_capability"] = AgentTool(
        execution_contract=data_broker("host.runtime.configure_capability"),
        name="configure_capability",
        description=(
            load_prompt('tools/configure_capability')
        ),
        parameters={
            "type": "object",
            "properties": {
                "capability": {"type": "string", "enum": ["web_search"]},
                "mode": {
                    "type": "string",
                    "enum": ["auto", "provider", "searxng", "disabled"],
                },
                "endpoint": {"type": "string"},
                "engine": {
                    "type": "string",
                    "enum": ["google", "bing", "baidu", "yandex", "duckduckgo"],
                },
            },
            "required": ["capability", "mode"],
        },
        execute=configure_capability,
        read_only=False,
        effect=ToolEffect.PROCESS,
        approval=ApprovalMode.ASK,
        approval_per_invocation=True,
        resources=["config:provider-capabilities"],
        idempotent=True,
    )


    def acknowledge_governance_signal(args: dict) -> dict:
        """Record the model's semantic resolution against Host-owned identity."""
        signal_id = str(args.get("signal_id", "")).strip()
        resolution = str(args.get("resolution", "")).strip()
        if not signal_id or not resolution:
            return {
                "accepted": False,
                "error": "signal_id and resolution are required",
            }
        context, _version = process.read_context_snapshot()
        matching = None
        for signal in context.get("signals", []) or []:
            candidate_id = (
                str(getattr(signal, "signal_id", ""))
                if not isinstance(signal, dict)
                else str(signal.get("signal_id", ""))
            )
            if candidate_id == signal_id:
                matching = signal
                break
        source = (
            str(getattr(matching, "source", ""))
            if matching is not None and not isinstance(matching, dict)
            else str((matching or {}).get("source", ""))
        )
        if matching is None or source != "rejection":
            return {
                "accepted": False,
                "error": "signal is not an active rejection signal",
            }

        evidence_ids = [
            str(item) for item in (args.get("evidence_receipt_ids", []) or [])
            if str(item)
        ]
        known_receipts = {
            str(receipt.get("receipt_id", ""))
            for receipt in process.tool_receipts
            if receipt.get("receipt_id")
            and receipt.get("succeeded") is True
            and receipt.get("committed") is True
        }
        unknown = [item for item in evidence_ids if item not in known_receipts]
        if unknown:
            return {
                "accepted": False,
                "error": "unknown or unsuccessful evidence receipts",
                "receipt_ids": unknown,
            }
        process.governance_resolutions[signal_id] = {
            "signal_id": signal_id,
            "resolution": resolution,
            "evidence_receipt_ids": list(dict.fromkeys(evidence_ids)),
            "resolved_at_step": process.steps_used,
        }
        return {
            "accepted": True,
            **process.governance_resolutions[signal_id],
        }


    tools["acknowledge_governance_signal"] = AgentTool(
        execution_contract=data_broker("host.runtime.acknowledge_governance_signal"),
        name="acknowledge_governance_signal",
        description=(
            load_prompt('tools/acknowledge_governance_signal')
        ),
        parameters={
            "type": "object",
            "properties": {
                "signal_id": {"type": "string"},
                "resolution": {"type": "string"},
                "evidence_receipt_ids": {
                    "type": "array", "items": {"type": "string"},
                },
            },
            "required": ["signal_id", "resolution", "evidence_receipt_ids"],
        },
        execute=acknowledge_governance_signal,
        effect=ToolEffect.READ,
        idempotent=True,
    )


    if process.actor_kind in {"supervisor", "worker", "reviewer"}:
        tools["request_user_decision"] = AgentTool(
            execution_contract=data_broker("host.runtime.request_user_decision"),
            name="request_user_decision",
            description=(
                load_prompt('tools/request_user_decision')
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "clarification", "preference", "direction", "verification",
                            "choice", "recovery", "checkpoint",
                        ],
                    },
                    "state_topic": {
                        "type": "string",
                        "description": (
                            "Stable semantic topic used to replace stale confirmed state; "
                            "required for preference/direction/verification/checkpoint."
                        ),
                    },
                    "supersedes_decision_id": {"type": "string"},
                    "question": {"type": "string"},
                    "why_user_must_decide": {"type": "string"},
                    "options": {
                        "type": "array", "minItems": 0, "maxItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                                "principle": {"type": "string"},
                                "immediate_effect": {"type": "string"},
                                "downstream_effect": {"type": "string"},
                                "risks": {"type": "string"},
                                "reversibility": {"type": "string"},
                                "recommended": {"type": "boolean"},
                            },
                            "required": ["label"],
                        },
                    },
                    "allow_free_form": {"type": "boolean"},
                },
                "required": ["question", "why_user_must_decide", "options"],
            },
            execute=lambda args: engineering.invoke({"operation": "ask_semantic", "request": args}),
            effect=ToolEffect.READ,
            idempotent=False,
        )
        from backend.core.loop.permission_broker import create_permission_request
        tools["request_permission"] = AgentTool(
            execution_contract=data_broker("host.runtime.request_permission"),
            name="request_permission",
            description=(
                load_prompt('tools/request_permission')
            ),
            parameters={
                "type": "object",
                "properties": {
                    "purpose": {"type": "string"},
                    "tool_name": {"type": "string"},
                    "arguments": {"type": "object"},
                    "resource": {"type": "string"},
                },
                "required": ["purpose", "tool_name", "arguments"],
            },
            # Resolve the tool surface at invocation time.  Authored tools are
            # mounted after this broker tool is constructed, so capturing a
            # merged dictionary here would make every later permission request
            # report the freshly mounted tool as unknown.
            execute=lambda args: create_permission_request(
                process,
                args,
                {**available_tools, **tools, **process.dynamic_tools},
                workspace_path,
            ),
            effect=ToolEffect.READ,
            idempotent=False,
        )


    manager = getattr(process, "_manager", None)


    if process.actor_kind in {"worker", "reviewer"}:
        def publish_interface_update(args: dict) -> dict:
            if manager is None:
                return {"accepted": False, "error": "AgentProcessManager is unavailable"}
            from backend.core.loop.coordination import observe_interface_changes
            try:
                result = observe_interface_changes(
                    manager,
                    process,
                    workspace_path or process.worktree_path,
                    summary=str(args.get("summary") or ""),
                    compatibility=str(args.get("compatibility") or "unknown"),
                    requested_refs=[
                        str(item) for item in list(args.get("interfaces") or [])
                    ],
                )
            except (OSError, ValueError) as exc:
                return {"accepted": False, "error": str(exc)}
            if on_stream_event is not None:
                for event in result.get("events", []):
                    on_stream_event({
                        "event": "coordination_event",
                        "process_id": process.process_id,
                        **event,
                        "_trace_detail": event,
                    })
            return {"accepted": True, **result}

        tools["publish_interface_update"] = AgentTool(
            execution_contract=data_broker("host.runtime.publish_interface_update"),
            name="publish_interface_update",
            description=(
                load_prompt('tools/publish_interface_update')
            ),
            parameters={
                "type": "object",
                "properties": {
                    "interfaces": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                    "compatibility": {
                        "type": "string",
                        "enum": ["compatible", "breaking", "unknown"],
                    },
                },
                "required": ["summary", "compatibility"],
            },
            execute=publish_interface_update,
            read_only=True,
            effect=ToolEffect.READ,
            idempotent=True,
        )

        def escalate_to_supervisor(args: dict) -> dict:
            if manager is None:
                return {"accepted": False, "error": "AgentProcessManager is unavailable"}
            from backend.core.loop.coordination import publish_coordination_event
            try:
                event = publish_coordination_event(
                    manager,
                    process,
                    kind=str(args.get("kind") or "coordination_request"),
                    summary=str(args.get("summary") or ""),
                    details={
                        "evidence": list(args.get("evidence") or []),
                        "proposed_action": str(args.get("proposed_action") or ""),
                        "requires_user_decision": bool(args.get("requires_user_decision", False)),
                        "user_question": str(args.get("user_question") or ""),
                    },
                    affected_interfaces=[
                        str(item) for item in list(args.get("affected_interfaces") or [])
                    ],
                    requires_supervisor_action=True,
                    block_affected=str(args.get("kind") or "") == "dependency_change",
                )
            except ValueError as exc:
                return {"accepted": False, "error": str(exc)}
            if on_stream_event is not None:
                on_stream_event({
                    "event": "coordination_event",
                    "process_id": process.process_id,
                    **event,
                    "_trace_detail": event,
                })
            return {"accepted": True, **event}

        tools["escalate_to_supervisor"] = AgentTool(
            execution_contract=data_broker("host.runtime.escalate_to_supervisor"),
            name="escalate_to_supervisor",
            description=(
                load_prompt('tools/escalate_to_supervisor')
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": [
                        "dependency_change", "interface_question", "requirement_ambiguity",
                        "blocked", "stale_user_intent", "coordination_request",
                    ]},
                    "summary": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "object"}},
                    "affected_interfaces": {"type": "array", "items": {"type": "string"}},
                    "proposed_action": {"type": "string"},
                    "requires_user_decision": {"type": "boolean"},
                    "user_question": {"type": "string"},
                },
                "required": ["kind", "summary", "evidence", "proposed_action"],
            },
            execute=escalate_to_supervisor,
            read_only=True,
            effect=ToolEffect.READ,
            idempotent=False,
        )


    ctx.engineering = engineering
    ctx.code_dossier = code_dossier
    ctx.dossier_store = dossier_store
