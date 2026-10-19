"""Workbench application tool and a narrowly scoped legacy arithmetic adapter."""
from backend.core.errors import error_payload
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.prompt_templates import load_prompt
from backend.core.workbench.calculation import safe_calculate


_OPERATIONS = ["calculate", "create_sheet", "revise_sheet", "continue_sheet", "open_sheet", "list_sheets", "import_context", "aggregate", "check_sources", "submit_material", "add_data", "import_result"]


def _source_warnings(result):
    assessments = list((result.get("cell_applicability") or {}).values())
    if result.get("applicability"):
        assessments.append(result["applicability"])
    if any(a.get("state") != "current" for a in assessments):
        from backend.core.errors import notice_guidance
        code = "WORKBENCH_SOURCE_RECHECK"
        result["warnings"] = list(result.get("warnings") or []) + [{"code": code, **notice_guidance(code),
            "message": "Some inputs are historical, unbound or model-assembled. Calculations describe their declared snapshots only. Inspect applicability and coverage; obtain stronger/current evidence if the task requires it. Do not retry a committed append just because it carries this warning."}]
    return result


def invoke(args, store=None, sources=None, material_context=None):
    operation = args.get("operation")
    if operation == "calculate":
        expression = str(args.get("expression", ""))
        return {"operation": operation, "expression": expression,
                "result": safe_calculate(expression)}
    if operation not in _OPERATIONS:
        raise ValueError("Unsupported workbench operation: " + str(operation))
    if store is None:
        return error_payload("WORKBENCH_UNAVAILABLE")
    from backend.core.workbench.sheets import RevisionConflict
    from backend.core.workbench.sources import SourceUnavailable, InvalidData
    try:
        if operation == "create_sheet":
            result = store.create(args["title"], args.get("notes", []))
        elif operation == "revise_sheet":
            result = store.revise(args["sheet_id"], args["expected_revision"], args["notes"],
                                  title=args.get("title"))
        elif operation == "continue_sheet":
            result = store.create(args["title"], args.get("notes", []), continuation={
                "sheet_id": args["sheet_id"], "revision": args["revision"],
                "cell_ids": args["cell_ids"], "reason": args["reason"]})
        elif operation == "open_sheet":
            result = store.open(args["sheet_id"], args.get("revision"),
                                offset=args.get("offset", 0), limit=args.get("limit", 20))
            if sources is not None:
                result = sources.project_open(result)
        elif operation == "list_sheets":
            result = store.list(after=args.get("after", ""), limit=args.get("limit", 20))
        elif operation == "submit_material":
            if material_context is None:
                return error_payload("WORKBENCH_UNAVAILABLE", message="Only the Runtime-bound main user task can submit material.")
            from backend.core.workbench.materials import submit_material
            result = submit_material(store, sources, sheet_id=args["sheet_id"], revision=args["revision"],
                cell_ids=args["cell_ids"], purpose=args["purpose"], **material_context)
        elif sources is None:
            return error_payload("WORKBENCH_UNAVAILABLE", message="Source operations require a Runtime-bound context store.")
        elif operation == "add_data":
            result = sources.add_data(args["sheet_id"], args["expected_revision"], cell_id=args["cell_id"],
                                      json_text=args["json_text"], description=args["description"])
        elif operation == "import_result":
            result = sources.import_result(args["sheet_id"], args["expected_revision"], cell_id=args["cell_id"],
                                           locator=args["locator"])
        elif operation == "import_context":
            result = sources.import_context(args["sheet_id"], args["expected_revision"],
                cell_id=args["cell_id"], source_ref=args["source_ref"])
        elif operation == "aggregate":
            result = sources.aggregate(args["sheet_id"], args["expected_revision"],
                cell_id=args["cell_id"], source_cell_id=args["source_cell_id"],
                selector=args["selector"], reducer=args["reducer"],
                field=args.get("field", ""), null_policy=args.get("null_policy", "reject"))
        else:
            result = sources.check(args["sheet_id"], args.get("revision"))
        return _source_warnings({"operation": operation, **result})
    except RevisionConflict as exc:
        return error_payload("WORKBENCH_REVISION_CONFLICT", details={"current_revision": exc.current},
            next_actions=[{"action": "open_current_sheet", "sheet_id": args.get("sheet_id", "")}])
    except SourceUnavailable as exc:
        return error_payload("WORKBENCH_SOURCE_UNAVAILABLE", message=str(exc))
    except InvalidData as exc:
        return error_payload("WORKBENCH_INVALID_DATA", message=str(exc))
    except (KeyError, ValueError) as exc:
        return error_payload("WORKBENCH_INVALID_INPUT", message=str(exc))
    except ImportError:
        return error_payload("WORKBENCH_UNAVAILABLE",
            message="The installed runtime lacks the declared nbformat document dependencies; arithmetic remains available.")


def bound_sheets(process):
    from backend.core.workbench.sheets import SheetStore
    storage = getattr(process, "bound_storage", None)
    session_id = getattr(getattr(process, "session", None), "session_id", "")
    process_id = getattr(process, "process_id", "")
    if storage is None or not session_id or not process_id:
        return None
    return SheetStore(storage, session_id=session_id, process_id=process_id,
                      owner_scope="process" if getattr(process, "parent_id", None) else "main_session")


def install(ctx):
    def execute(args):
        if args.get("operation") == "calculate":
            return invoke(args)
        process = getattr(ctx, "process", None)
        sheets = bound_sheets(process)
        sources = None
        if sheets is not None and getattr(ctx, "workspace_path", ""):
            from backend.core.loop.context_store import ContextObjectStore
            from backend.core.workbench.sources import SourceWorkbench
            def current_context():
                read = getattr(process, "read_context_snapshot", None)
                return read()[0] if read else dict(getattr(process, "context_snapshot", None) or {})
            sources = SourceWorkbench(sheets, ContextObjectStore(ctx.workspace_path), current_context)
        context = current_context() if sources is not None else {}
        project = str(context.get("project_name") or "")
        material_context = {"project": project, "task_id": getattr(process, "active_task_id", ""),
                            "context": context} if project and not getattr(process, "parent_id", None) else None
        return invoke(args, sheets, sources, material_context)

    ctx.tools["workbench"] = AgentTool(
        name="workbench", description=load_prompt("tools/workbench"),
        parameters={"type": "object", "properties": {
            "operation": {"type": "string", "enum": _OPERATIONS},
            "expression": {"type": "string", "minLength": 1, "maxLength": 500},
            "title": {"type": "string", "minLength": 1, "maxLength": 240},
            "sheet_id": {"type": "string"}, "revision": {"type": "string"},
            "expected_revision": {"type": "string"},
            "notes": {"type": "array", "maxItems": 100, "items": {
                "type": "object", "properties": {"id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,64}$"},
                    "text": {"type": "string", "minLength": 1, "maxLength": 24000}},
                "required": ["id", "text"], "additionalProperties": False}},
            "cell_ids": {"type": "array", "maxItems": 100, "items": {"type": "string"}, "uniqueItems": True},
            "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "after": {"type": "string"},
            "cell_id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,64}$"},
            "purpose": {"type": "string", "minLength": 1, "maxLength": 2000},
            "json_text": {"type": "string", "maxLength": 120000},
            "description": {"type": "string", "minLength": 1, "maxLength": 2000},
            "locator": {"type": "string"},
            "source_cell_id": {"type": "string"}, "source_ref": {"type": "string"},
            "selector": {"type": "string", "maxLength": 500, "description": "JSON Pointer to the input collection: empty string selects root, /items selects the items member. Plain member names or expressions are invalid."},
            "field": {"type": "string", "maxLength": 500, "description": "Optional per-row JSON Pointer: /value selects each row value member; empty string selects the row itself. Use /value, never plain value."},
            "reducer": {"type": "string", "enum": ["count", "key_count", "sum", "min", "max", "distinct_count"]},
            "null_policy": {"type": "string", "enum": ["reject", "skip"]}},
            "required": ["operation"], "additionalProperties": False},
        # READ is the existing internal protocol-state classification, as for
        # engineering_workflow. No user workspace files or execution receipts
        # are modified. Sheet creation is not idempotent and must not be replayed.
        execute=execute, read_only=True, effect=ToolEffect.READ, idempotent=False)

    # Explicit old allowlists retain their original arithmetic authority only.
    def legacy_calculate(args):
        result = invoke({"operation": "calculate", "expression": args.get("expression", "")})
        return {"expression": result["expression"], "result": result["result"]}

    ctx.tools["calculate"] = AgentTool(
        name="calculate", description=load_prompt("tools/calculate"),
        parameters={"type": "object", "properties": {"expression": {"type": "string"}},
                    "required": ["expression"]},
        execute=legacy_calculate, effect=ToolEffect.READ, idempotent=True)
