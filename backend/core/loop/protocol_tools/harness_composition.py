"""Model adapter to the registered, project-owned harness composition service."""
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.execution_contract import data_broker
from backend.core.prompt_templates import load_prompt


def install(ctx):
    from backend.core.mechanism_runtime.application import configuration_allowed
    from backend.core.mechanism_runtime.capability_protocol import current_schema, validate_request, preview_request, OBSERVATIONS
    from backend.core.mechanism_runtime.lifecycle_control import OPERATIONS as LIFECYCLE
    operations = ["catalog", "status", "preview", "preview_revision"]
    if configuration_allowed(ctx.process):
        operations += ["apply", "revise", "use", "commit", "publish", "publish_revision", "instantiate", "activate", "suspend", "detach", "reconcile", "require", "inbox_revise", "inbox_merge", "inbox_discard", "inbox_adopt"]
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
        # ToolPipeline adds private execution roots after validating public
        # arguments. They are not capability declaration or revision inputs.
        args = {key: value for key, value in args.items() if not key.startswith('_')}
        try:
            validate_request(args)
            app = application()
            if args.get('operation') == 'preview' and 'program' not in args:
                return preview_request(app.capability_runtime, args)
            if args.get('operation') == 'catalog':
                catalog = app.invoke(args)
                latest = {}
                for element in catalog['elements']:
                    if element['id'] not in latest or element['version'] > latest[element['id']]['version']:
                        latest[element['id']] = element
                # Lifecycle entries are supplied by declaration.on. Advertising
                # them as ordinary calls caused a real invalid authoring turn.
                callable_elements = [item for item in latest.values() if not item['entry'] or args.get('element_id')]
                return {'source_refs': catalog['source_refs'], 'version': catalog['version'],
                    'available_capabilities': catalog['available_capabilities'],
                    'lifecycle_boundaries': sorted({boundary for item in catalog['elements'] if item['entry'] for boundary in item['boundaries']}),
                    'elements': [{key: item[key] for key in ('id', 'version', 'description', 'configuration', 'inputs', 'outputs', 'boundaries')}
                        | {'callable': not item['entry']} for item in callable_elements], 'authority_granted': False}
            if args.get('operation') == 'status':
                from backend.core.mechanism_runtime.semantic_projection import describe_instances
                current = app.invoke(args)
                views = describe_instances(app.process, current['instances'], observations=True)
                for view, original in zip(views, current['instances']):
                    view['inbox'] = original.get('inbox', {})
                return {**current, 'instances': views,
                    'definitions': [{key: item[key] for key in ('id', 'revision', 'head_ref', 'selected_ref', 'declaration') if key in item}
                        | ({'legacy_program': item['compiled']} if 'declaration' not in item else {}) for item in current['definitions']]}
            if args.get('operation', '').startswith('inbox_'):
                return app.inbox.commit(args)
            if args.get('operation') in LIFECYCLE:
                return app.lifecycle_control.commit(args)
            return app.invoke(args)
        except (ValueError, TypeError, KeyError, OSError, ImportError, RuntimeError) as error:
            from backend.core.storage import StorageCommitUnknown
            return {"error": "MECHANISM_COMMIT_UNKNOWN" if isinstance(error, StorageCommitUnknown) else "MECHANISM_REQUEST_REJECTED",
                "detail": str(error), "authority_granted": False,
                "effect_state": "unknown" if isinstance(error, StorageCommitUnknown) else
                    "inspect_required" if args.get("operation") not in {"catalog", "status", "preview", "preview_revision"} else "not_committed"}

    def preview(args):
        args = {key: value for key, value in args.items() if not key.startswith('_')}
        validate_request(args)
        if args.get('operation') in OBSERVATIONS:
            return None
        app = application()
        if args.get("operation") in {"apply", "revise", "use", "commit"}:
            subject = app.capability_runtime.prepare(args)
            # Semantic review already receives exact compiled before/after
            # meaning and typed component contracts. The source declarations
            # are a readable preview representation, not a second obligation.
            return {key: value for key, value in subject.items()
                if key not in {'declaration', 'previous_declaration'}}
        if args.get("operation") in {"publish", "publish_revision"}:
            return {"kind": "harness_composition", **app.semantic_preview(args)}
        if args.get('operation') in LIFECYCLE:
            return app.lifecycle_control.prepare(args)
        if args.get("operation") == "instantiate":
            return app.lifecycle_preview(args)
        if args.get("operation", "").startswith("inbox_"):
            return app.inbox.prepare(args)
        raise ValueError("MECHANISM_OPERATION_UNSUPPORTED")

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
            "\nThis actor is read-only: catalog, status and preview are available. It cannot apply, revise, activate, suspend or otherwise change project or main-task mechanisms."),
        parameters={"type": "object", "additionalProperties": False, "properties": {
            "operation": {"enum": operations},
            "element_id": {"type": "string"}, "element_version": {"type": "integer", "minimum": 1},
            "declaration": {"type": "object", "description": "Callable mechanism declaration: id, intent, on lifecycle boundaries, and calls with id/use/parameters plus optional inputs, when, after."},
            "revision": {"type": "object", "description": "Named mechanism edits: intent/on replacements, calls additions or parameter/input merges keyed by each call id, and remove ids."},
            "program": {"type": "object"}, "preview_digest": {"type": "string"},
            "preview_ref": {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$",
                "description": "Exact draft reference returned by preview. commit accepts only operation and preview_ref; no rewritten arguments or authority."},
            "changes": {"type":"array","minItems":1,"maxItems":32,"items":{"type":"object",
                "properties":{"op":{"enum":["add","replace","remove"]},"path":{"type":"string","maxLength":512},"value":{}},
                "required":["op","path"],"additionalProperties":False},
                "description":"For preview_revision/publish_revision: bounded JSON Pointer edits to intent, elements or bindings of the exact expected_head. Do not rewrite the entire saved graph for a small parameter change."},
            "expected_head": {"type": ["string", "null"], "description": "Optional exact head assertion from preview. Omit to use the CAS head already bound by preview_digest. For a new definition the head is JSON null, never the string 'null'."}, "definition_id": {"type": "string"},
            "definition_revision": {"type": "integer", "minimum": 1, "description": "Exact saved revision for use, preview or status. For use, omission selects the latest saved revision even after previewing an older one; pinned freezes the selection, not a previously mentioned version. Legacy activate uses it to change a pinned instance; omission there resumes its selected revision."}, "instance_id": {"type": "string"},
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
                "id": {"type": "string"}, "revision": {"type": "integer", "minimum": 1}}, "required": ["id"], "additionalProperties": False}},
            "source_refs": {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "string"}}},
            "required": ["operation"]}, execute=invoke, alignment_contract=preview,
        read_only=True, effect=ToolEffect.READ, idempotent=False)
    tool = ctx.tools['harness_mechanism']
    from backend.core.loop.model_feedback import harness_feedback
    tool.model_result = harness_feedback
    tool.advertised_parameters = current_schema(tool.parameters, operations)
