"""Model adapter to the registered, project-owned harness composition service."""
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.execution_contract import data_broker
from backend.core.prompt_templates import load_prompt


def install(ctx):
    from backend.core.mechanism_runtime.application import configuration_allowed
    operations = ["catalog", "status", "preview", "preview_revision"]
    if configuration_allowed(ctx.process):
        operations += ["publish", "publish_revision", "instantiate", "activate", "suspend", "detach", "reconcile", "require", "inbox_revise", "inbox_merge", "inbox_discard", "inbox_adopt"]
    def application():
        from backend.core.mechanism_runtime.application import Application
        value = getattr(ctx.process, "_harness_application", None)
        restoring = value is None
        if value is None:
            value = Application(ctx.process, ctx.llm_provider, ctx.on_stream_event)
            ctx.process._harness_application = value
            # A restored program is reconciled before new callbacks. Invocation
            # tombstones survive a lost reply; observation never replays them.
        else:
            value.bind_provider(ctx.llm_provider, ctx.on_stream_event)
        if ctx.process.actor_kind == "supervisor" and ctx.manager is not None and ctx.llm_provider is not None and ctx.dispatcher is not None:
            from backend.core.mechanism_runtime.reviews import review_service
            value.services["agent.review"] = review_service(ctx, value)
        value.coordinator.capabilities = tuple(value.services)
        for instance in value.repo.list_instances(value.task, limit=100):
            if restoring:
                value.coordinator.reconcile(value.task, instance['id'])
            run = instance.get('runs', {}).get(instance.get('active_run'), {})
            if restoring or instance['phase'] == 'blocked' and run.get('phase') == 'reconciliation_required':
                value.runner.reconcile(value.task, instance['id'])
                value.coordinator.settle(value.task, instance['id'])
        if restoring:
            value.dispatch("restored", f"restore:{ctx.process.session.session_id}:{ctx.process.durable_revision}",
                {"task_id": value.task, "checkpoint_revision": ctx.process.durable_revision})
        value.requirements.synchronize()
        return value

    def invoke(args):
        try:
            return application().invoke(args)
        except (ValueError, TypeError, KeyError, OSError, ImportError, RuntimeError) as error:
            from backend.core.storage import StorageCommitUnknown
            return {"error": "MECHANISM_COMMIT_UNKNOWN" if isinstance(error, StorageCommitUnknown) else "MECHANISM_REQUEST_REJECTED",
                "detail": str(error), "authority_granted": False,
                "effect_state": "unknown" if isinstance(error, StorageCommitUnknown) else
                    "inspect_required" if args.get("operation") not in {"catalog", "status", "preview", "preview_revision"} else "not_committed"}

    def preview(args):
        app = application()
        if args.get("operation") in {"publish", "publish_revision"}:
            return {"kind": "harness_composition", **app.semantic_preview(args)}
        if args.get("operation") in {"instantiate", "activate", "suspend", "detach"}:
            return app.lifecycle_preview(args)
        if args.get('operation') == 'reconcile':
            instance, _ = app.repo.instance(app.task, args['instance_id'])
            return {'kind': 'harness_readonly_reconciliation', 'instance_id': args['instance_id'],
                'current_revision': (instance or {}).get('revision'), 'effect_state': (instance or {}).get('error', {}).get('effect_state'),
                'never_replays_effects': True, 'authority_granted': False}
        if args.get("operation", "").startswith("inbox_"):
            app.sources(args.get("source_refs"))
            instance, _ = app.repo.instance(app.task, args["instance_id"])
            selected = {args["candidate_id"]} if args.get("candidate_id") else {item["id"] for item in args.get("candidates", [])}
            candidates = [{key: item.get(key) for key in ("id", "revision", "status", "text", "source_refs")}
                for item in (instance or {}).get("inbox", {}).values() if item["id"] in selected]
            return {"kind": "harness_candidate_disposition", "request": args, "current_candidates": candidates,
                    "requires_actual_user_adoption": args["operation"] == "inbox_adopt"}
        if args.get("operation") == "require":
            app.sources(args.get("source_refs"))
            return {"kind": "harness_required_constraints", "request": args, "current": app.requirements.read(app.task)[0],
                    "affects_owned_child_tasks": True, "authority_granted": False}
        raise ValueError("MECHANISM_OBSERVATION_HAS_NO_EFFECT_BOUNDARY")

    # Rebind loaded component adapters to the actual current provider before
    # a restored task can reach its first context/tool boundary.
    storage = getattr(ctx.process, "bound_storage", None)
    if storage is not None:
        from backend.core.mechanism_runtime.contracts import digest
        from backend.core.mechanism_runtime.repository import NAMESPACE
        parent = ctx.manager.get(ctx.process.parent_id) if ctx.manager and ctx.process.parent_id else None
        if (storage.list_state_refs(NAMESPACE, prefix="instance/" + digest(ctx.process.active_task_id or ctx.process.process_id) + "/", limit=1)
                or parent is not None and storage.get_state_ref(NAMESPACE, "requirements/" + digest(parent.active_task_id or parent.process_id))):
            application()

    ctx.tools["harness_mechanism"] = AgentTool(name="harness_mechanism",
        execution_contract=data_broker("host.runtime.harness_mechanism"),
        description=load_prompt("tools/harness_mechanism") + ("" if configuration_allowed(ctx.process) else
            "\nThis actor is read-only: only catalog, status, preview and preview_revision are available. It cannot publish, activate, suspend or otherwise change project or main-task mechanisms."),
        parameters={"type": "object", "additionalProperties": False, "properties": {
            "operation": {"enum": operations},
            "element_id": {"type": "string"}, "element_version": {"type": "integer", "minimum": 1},
            "program": {"type": "object"}, "preview_digest": {"type": "string"},
            "changes": {"type":"array","minItems":1,"maxItems":32,"items":{"type":"object",
                "properties":{"op":{"enum":["add","replace","remove"]},"path":{"type":"string","maxLength":512},"value":{}},
                "required":["op","path"],"additionalProperties":False},
                "description":"For preview_revision/publish_revision: bounded JSON Pointer edits to intent, elements or bindings of the exact expected_head. Do not rewrite the entire saved graph for a small parameter change."},
            "expected_head": {"type": ["string", "null"], "description": "Optional exact head assertion from preview. Omit to use the CAS head already bound by preview_digest. For a new definition the head is JSON null, never the string 'null'."}, "definition_id": {"type": "string"},
            "definition_revision": {"type": "integer", "minimum": 1, "description": "For instantiate OR activate: the exact saved definition revision to select. activate upgrades a pinned instance in place when this is supplied; omission resumes its currently selected revision."}, "instance_id": {"type": "string"},
            "version_policy": {"enum": ["pinned", "follow_compatible"]}, "expected_revision": {"type": "integer", "minimum": 1},
            "candidate_id": {"type": "string"}, "candidate_revision": {"type": "integer", "minimum": 1},
            "text": {"type": "string", "maxLength": 16000}, "quote": {"type": "string", "maxLength": 4000},
            "contract_revision": {"type": "integer", "minimum": 0},
            "expected_required_revision": {"type": "integer", "minimum": 0},
            "requirements": {"type": "array", "maxItems": 16, "items": {"type": "object", "properties": {
                "definition_id": {"type": "string"}, "definition_revision": {"type": "integer", "minimum": 1},
                "version_policy": {"enum": ["pinned", "follow_compatible"]}},
                "required": ["definition_id", "version_policy"], "additionalProperties": False}},
            "candidates": {"type": "array", "minItems": 2, "maxItems": 8, "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "revision": {"type": "integer", "minimum": 1}}, "required": ["id", "revision"], "additionalProperties": False}},
            "source_refs": {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "string"}}},
            "required": ["operation"]}, execute=invoke, alignment_contract=preview,
        read_only=True, effect=ToolEffect.READ, idempotent=False)
