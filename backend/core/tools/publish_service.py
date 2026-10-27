"""Host-bound registrations shared by the Dashboard and Agent Publish paths."""
from pathlib import Path

from backend.core.loop.agent_tool import AgentTool, CancellationMode, ApprovalMode, ToolEffect
from backend.core.loop.execution_contract import data_broker
from backend.core.loop.operation_policy import decide_tool_operation, PolicyDisposition
from backend.core.publish.service import PublishService
from backend.core.publish.state import PublishError

_STRING = {"type": "string"}
_REVISION = {"type": "integer", "minimum": 1}
_PLATFORM_TARGET = {"type": "object", "additionalProperties": False, "properties": {
    "kind": {"type": "string", "enum": ["github", "gitlab"]}, "instance": _STRING, "repository": _STRING, "account": _STRING},
    "required": ["kind", "instance", "repository", "account"]}
_PLATFORM_REFERENCE = {"type": "object", "additionalProperties": False, "properties": {
    "kind": {"type": "string", "enum": ["comment", "anchor"]}, "id": _STRING}, "required": ["kind", "id"]}
_HISTORY_HEAD = {"type": "string", "enum": ["candidate", "formal", *["contribution:" + str(i) for i in range(8)]]}
SCHEMAS = {
    "prepare": ({"source_role": {"type": "string", "enum": ["workspace", "trial"]},
        "source_ref": _STRING, "target_branch": _STRING, "owner": _STRING,
        "title": _STRING, "base_oid": _STRING, "rework": _STRING, "item_id": _STRING,
        "destination": {"type": "string", "enum": ["work", "formal"]},
        "method": {"type": "string", "enum": ["squash", "ff", "merge", "rebase", "cherry_pick", "partial"]}, "test_target": _STRING,
        "contributions": {"type": "array", "maxItems": 7, "items": {"type": "object", "additionalProperties": False,
            "properties": {"source_role": {"type": "string", "enum": ["workspace", "trial"]}, "source_ref": _STRING, "base_oid": _STRING},
            "required": ["source_role", "source_ref"]}},
        "selected_commits": {"type": "array", "maxItems": 32, "items": _STRING},
        "paths": {"type": "array", "maxItems": 512, "items": _STRING},
        "checks": {"type": "array", "maxItems": 8, "items": {"type": "object", "additionalProperties": False,
            "properties": {"id": _STRING, "target": _STRING,
                "seeds": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "integer", "minimum": 0, "maximum": 4294967295}},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 120}}, "required": ["target"]}},
        "expected_revision": _REVISION}, ["source_role", "source_ref", "target_branch", "owner"]),
    "plan": ({"item_id": _STRING, "expected_revision": _REVISION, "target_branch": _STRING,
        "owner": _STRING, "rework": _STRING}, ["item_id", "expected_revision", "target_branch", "owner", "rework"]),
    "adopt": ({"item_id": _STRING, "expected_revision": _REVISION}, ["item_id", "expected_revision"]),
    "reconcile": ({"item_id": _STRING, "expected_revision": _REVISION}, ["item_id", "expected_revision"]),
    "verify": ({"item_id": _STRING, "expected_revision": _REVISION}, ["item_id", "expected_revision"]),
    "execute": ({"item_id": _STRING, "expected_revision": _REVISION}, ["item_id", "expected_revision"]),
    "pause": ({"item_id": _STRING, "expected_revision": _REVISION}, ["item_id", "expected_revision"]),
    "resume": ({"item_id": _STRING, "expected_revision": _REVISION}, ["item_id", "expected_revision"]),
    "decide": ({"item_id": _STRING, "expected_revision": _REVISION, "note": _STRING, "owner": _STRING,
        "decision": {"type": "string", "enum": ["accept", "rework", "defer"]}},
        ["item_id", "expected_revision", "note", "owner", "decision"]),
    "run": ({"item_id": _STRING, "expected_revision": _REVISION,
        "steps": {"type": "array", "minItems": 1, "maxItems": 2,
                  "items": {"type": "string", "enum": ["verify", "execute", "adopt"]}}},
        ["item_id", "expected_revision", "steps"]),
    "share": ({"item_id": _STRING, "expected_revision": _REVISION, "remote": _STRING,
        "action": {"type": "string", "enum": ["prepare", "refresh", "reconcile"]}}, ["item_id", "expected_revision", "action"]),
    "share_publish": ({"item_id": _STRING, "expected_revision": _REVISION}, ["item_id", "expected_revision"]),
    "collaborate": ({"item_id": _STRING, "expected_revision": _REVISION,
        "action": {"type": "string", "enum": ["refresh", "search", "followup", "draft", "reconcile"]}, "target": _PLATFORM_TARGET,
        "resource": {"type": "object", "additionalProperties": False,
            "properties": {"kind": {"type": "string", "enum": ["issue", "pull", "issues", "pulls"]}, "number": {"type": "integer", "minimum": 1}}, "required": ["kind"]},
        "snapshot_id": _STRING, "draft_id": _STRING, "page": {"type": "integer", "minimum": 1, "maximum": 20},
        "query": _STRING, "followup": {"type": "object", "additionalProperties": False, "properties": {
            "reference": _PLATFORM_REFERENCE, "status": {"type": "string", "enum": ["pending_reply", "resolved"]},
            "owner": _STRING, "note": _STRING}, "required": ["reference", "status", "owner", "note"]},
        "draft": {"type": "object", "additionalProperties": False, "properties": {
            "kind": {"type": "string", "enum": ["comment", "review", "issue", "pull", "edit", "state", "comment_edit", "reply"]},
            "reference": _PLATFORM_REFERENCE, "state": {"type": "string", "enum": ["open", "closed"]},
            **{key: _STRING for key in ["body", "title", "head", "base", "head_oid", "base_oid", "decision"]},
            "anchor": {"type": "object", "additionalProperties": False, "properties": {
                "path": _STRING, "line": {"type": "integer", "minimum": 1}, "side": {"type": "string", "enum": ["LEFT", "RIGHT"]}, "commit_id": _STRING},
                "required": ["path", "line", "side", "commit_id"]}}, "required": ["kind", "body"]}},
        ["item_id", "expected_revision", "action"]),
    "collaborate_send": ({"item_id": _STRING, "expected_revision": _REVISION, "draft_id": _STRING}, ["item_id", "expected_revision", "draft_id"]),
    "link_task": ({"item_id": _STRING, "expected_revision": _REVISION,
        **{key: _STRING for key in ["process_id", "parent_process_id", "decision_id", "snapshot_id", "anchor_id", "note"]}}, ["item_id", "expected_revision", "process_id"]),
    "package": ({"item_id": _STRING, "expected_revision": _REVISION, "version": _STRING,
        "recipe": {"type": "object", "additionalProperties": False, "properties": {"entry": _STRING,
            "outputs": {"type": "array", "minItems": 1, "maxItems": 8, "items": _STRING},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 120}}, "required": ["entry", "outputs"]}}, ["item_id", "expected_revision", "version"]),
    "export": ({"item_id": _STRING, "expected_revision": _REVISION, "package_id": _STRING, "destination": _STRING}, ["item_id", "expected_revision", "package_id", "destination"]),
    "tag": ({"item_id": _STRING, "expected_revision": _REVISION, "package_id": _STRING, "remote": _STRING}, ["item_id", "expected_revision", "package_id"]),
    "release": ({"item_id": _STRING, "expected_revision": _REVISION, "package_id": _STRING, "target": _PLATFORM_TARGET}, ["item_id", "expected_revision", "package_id", "target"]),
    "import_package": ({"item_id": _STRING, "expected_revision": _REVISION, "path": _STRING, "sha256": _STRING}, ["item_id", "expected_revision", "path", "sha256"]),
    "distribution_reconcile": ({"item_id": _STRING, "expected_revision": _REVISION}, ["item_id", "expected_revision"]),
    "download": ({"item_id": _STRING, "expected_revision": _REVISION, "package_id": _STRING,
        "target": _PLATFORM_TARGET, "destination": _STRING}, ["item_id", "expected_revision", "package_id", "target", "destination"]),
    "branch": ({"item_id": _STRING, "expected_revision": _REVISION,
        "action": {"type": "string", "enum": ["retain_source", "retain_work", "prepare_archive", "prepare_delete", "prepare_publish", "prepare_remote_delete", "reconcile"]},
        **{key: _STRING for key in ["branch", "owner", "reason", "remote", "remote_branch", "merge_item_id"]}}, ["item_id", "expected_revision", "action"]),
    "branch_apply": ({"item_id": _STRING, "expected_revision": _REVISION}, ["item_id", "expected_revision"]),
}
SCHEMAS["migrate"] = ({"legacy_id": _STRING, **{key: value for key, value in SCHEMAS["prepare"][0].items()
    if key not in {"item_id", "expected_revision", "title", "base_oid"}}},
    ["legacy_id", "source_role", "source_ref", "target_branch", "owner"])


def build_publish_tools(project):
    result = {}
    def current_project():
        from backend.core.application.services import ApplicationServices
        current = ApplicationServices._publish_project(project.name)
        if Path(current.workspace_path).resolve() != Path(project.workspace_path).resolve():
            raise PublishError("PUBLISH_CONTEXT_UNAVAILABLE")
        return current
    for operation, (properties, required) in SCHEMAS.items():
        def prepare(args, operation=operation):
            current = current_project()
            prepared = {**args, "_workspace": str(project.workspace_path)}
            if operation in {"package", "tag", "release", "import_package"}:
                prepared["_publish_target_path"] = str(current.backup_path)
            if operation == "distribution_reconcile":
                from backend.core.publish.state import read_ledger, find_item
                ledger, _ = read_ledger(project.workspace_path)
                pending = find_item(ledger, args.get("item_id")).get("distribution_operation", {})
                # Recovery observes the original effect location. Configuration
                # may have changed since its intent; authorizing today's Formal
                # path does not authorize yesterday's repository/export folder.
                key, value = {
                    "tag": ("_publish_target_path", pending.get("repository")),
                    "package": ("_publish_target_path", str(current.backup_path)),
                    "export": ("_publish_export_path", pending.get("destination")),
                    "download": ("_publish_export_path", pending.get("destination")),
                    "release": (None, None),
                }.get(pending.get("kind"), (None, None))
                if key:
                    if not isinstance(value, str) or not Path(value).is_absolute():
                        raise PublishError("PUBLISH_CONTEXT_UNAVAILABLE")
                    prepared[key] = value
            if operation == "branch" and args.get("merge_item_id"):
                prepared["_publish_target_path"] = str(current.backup_path)
            if operation in {"export", "download"}:
                prepared["_publish_export_path"] = args["destination"]
            if operation == "import_package":
                prepared["_publish_import_path"] = args["path"]
            if operation == "run":
                from backend.core.publish.state import read_ledger, find_item
                ledger, _ = read_ledger(project.workspace_path)
                item = find_item(ledger, args.get("item_id"))
                if item["plan"].get("destination") == "formal":
                    prepared["_publish_target_path"] = str(current.backup_path)
                else:
                    prepared["_publish_source_path"] = str(current.trial_path if item["candidate"]["role"] == "trial" else current.workspace_path)
            if operation in {"verify", "execute", "reconcile"} or operation in {"prepare", "migrate"} and args.get("destination", "formal" if operation == "migrate" else "work") == "formal":
                prepared["_publish_target_path"] = str(current.backup_path)
            if operation in {"prepare", "adopt", "migrate"}:
                role = args.get("source_role")
                if operation == "adopt":
                    from backend.core.publish.state import read_ledger, find_item
                    ledger, _ = read_ledger(project.workspace_path)
                    role = find_item(ledger, args.get("item_id"))["candidate"]["role"]
                prepared["_publish_source_path"] = str(current.trial_path if role == "trial" else current.workspace_path)
                if operation in {"prepare", "migrate"} and any(row.get("source_role") == "trial" for row in args.get("contributions", [])):
                    prepared["_publish_trial_path"] = str(current.trial_path)
            return prepared
        def execute_command(args, operation=operation):
            if (str(Path(args.get("_workspace", "")).resolve()) != str(Path(project.workspace_path).resolve())
                    or not args.get("_publish_actor")):
                raise PublishError("PUBLISH_CONTEXT_UNAVAILABLE")
            if operation in {"prepare", "adopt", "migrate"} and "_publish_source_path" not in args:
                raise PublishError("PUBLISH_CONTEXT_UNAVAILABLE")
            current = current_project()
            if operation in {"package", "tag", "release", "distribution_reconcile", "export", "download", "import_package", "branch"}:
                fresh = prepare(args, operation=operation)
                if any(fresh.get(key) != args.get(key) for key in ("_publish_target_path", "_publish_export_path", "_publish_import_path")):
                    raise PublishError("PUBLISH_CONTEXT_UNAVAILABLE")
            if operation == "run":
                fresh = prepare(args, operation=operation)
                for key in ("_publish_source_path", "_publish_target_path"):
                    if fresh.get(key) != args.get(key):
                        raise PublishError("PUBLISH_CONTEXT_UNAVAILABLE")
            if operation in {"verify", "execute", "reconcile"} or operation in {"prepare", "migrate"} and args.get("destination", "formal" if operation == "migrate" else "work") == "formal":
                if "_publish_target_path" not in args or args["_publish_target_path"] != str(current.backup_path):
                    raise PublishError("PUBLISH_TARGET_CHANGED")
            if operation in {"prepare", "adopt", "migrate"}:
                fresh = prepare(args, operation=operation)
                if any(fresh.get(key) != args.get(key) for key in ("_publish_source_path", "_publish_trial_path")):
                    raise PublishError("PUBLISH_SOURCE_CHANGED")
            service = PublishService(current, actor=args["_publish_actor"], task_id=args.get("_publish_task", ""))
            try:
                return service.command(operation, {key: value for key, value in args.items() if not key.startswith("_")})
            except (OSError, RuntimeError) as exc:
                return {"error": getattr(exc, "code", "PUBLISH_OPERATION_UNCERTAIN"),
                        "effect_state": "ambiguous", "request_id": args.get("request_id"),
                        "operation_id": getattr(service, "operation_id", "")}
        def execute(args, handler=execute_command):
            try:
                return handler(args)
            except PublishError as exc:
                return {"error": exc.code, "effect_state": "not_committed", "request_id": args.get("request_id"), "operation_id": ""}
        tool = AgentTool(name="publish_" + operation,
            description="Publish " + operation + ": explicit scoped operation on fixed candidates, collaboration drafts, shared metadata or verified distribution assets. Saved queries never perform these actions.",
            parameters={"type": "object", "properties": {"request_id": _STRING, **properties},
                "required": ["request_id", *required], "additionalProperties": False},
            execute=execute, prepare_args=prepare,
            read_only=False, resources=["governance:publish"], timeout=300,
            cancellation=CancellationMode.COOPERATIVE if operation in {"verify", "run", "package"} else CancellationMode.NON_INTERRUPTIBLE,
            execution_contract=data_broker("governance.publish"))
        if operation in {"share_publish", "collaborate_send", "tag", "release", "download", "branch_apply"}:
            tool.effect, tool.approval, tool.approval_per_invocation = ToolEffect.EXTERNAL, ApprovalMode.ASK, True
            tool.resources = ["publish://" + operation]
        result[tool.name] = tool
    from backend.core.application.publish import list_items
    from backend.core.publish.state import read_ledger, find_item, projection
    def query_item(args):
        ledger, ref = read_ledger(project.workspace_path)
        return {"item": projection(find_item(ledger, args["item_id"]), ref)}
    def query_operation(args):
        from backend.core.application.publish import get_operation
        return get_operation(project, args["item_id"], args["operation_id"])
    def query_shared(args):
        from backend.core.publish.shared_query import shared_items
        return shared_items(project, **{key: value for key, value in args.items() if not key.startswith("_")})
    def query_history(args):
        from backend.core.publish.history import history_page
        return history_page(project, **{key: value for key, value in args.items() if not key.startswith("_")})
    def query_diff(args):
        from backend.core.publish.history import diff_view
        return diff_view(project, **{key: value for key, value in args.items() if not key.startswith("_")})
    for name, properties, required, query in [
        ("publish_items", {"limit": {"type": "integer", "minimum": 1, "maximum": 100}, "cursor": _STRING}, [],
            lambda args: list_items(project, limit=args.get("limit", 50), cursor=args.get("cursor", ""))),
        ("publish_item", {"item_id": _STRING}, ["item_id"], query_item),
        ("publish_shared_items", {"item_id": _STRING, "observation_id": _STRING,
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}, "cursor": _STRING}, ["item_id"], query_shared),
        ("publish_history", {"item_id": _STRING, "head": _HISTORY_HEAD,
            "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "cursor": _STRING}, ["item_id"], query_history),
        ("publish_diff", {"item_id": _STRING, "head": _HISTORY_HEAD, "base_oid": _STRING, "head_oid": _STRING,
            "snapshot_id": _STRING, "limit_bytes": {"type": "integer", "minimum": 1024, "maximum": 1048576}}, ["item_id", "base_oid"], query_diff),
        ("publish_operation", {"item_id": _STRING, "operation_id": _STRING}, ["item_id", "operation_id"], query_operation)]:
        def prepare_query(args, name=name):
            prepared = {**args, "_workspace": str(project.workspace_path)}
            if name in {"publish_history", "publish_diff"}:
                from backend.core.publish.history import history_scope
                prepared["_publish_history_path"] = history_scope(current_project(), args["item_id"], args.get("head", "candidate"))["repository"]
            return prepared
        def execute_query(args, query=query, name=name):
            if Path(args.get("_workspace", "")).resolve() != Path(project.workspace_path).resolve():
                return {"error": "PUBLISH_CONTEXT_UNAVAILABLE", "effect_state": "not_committed"}
            current_project()
            if name in {"publish_history", "publish_diff"}:
                from backend.core.publish.history import history_scope
                actual = history_scope(current_project(), args["item_id"], args.get("head", "candidate"))["repository"]
                if args.get("_publish_history_path") != actual:
                    return {"error": "PUBLISH_CONTEXT_UNAVAILABLE", "effect_state": "not_committed"}
            return query(args)
        result[name] = AgentTool(name=name, description="Read saved Publish facts without preparing, checking or executing Git.",
            parameters={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            execute=execute_query, prepare_args=prepare_query,
            read_only=True, execution_contract=data_broker("governance.publish.query"))
    return result


def invoke_local_publish(project, operation, arguments, *, queue=None, cancelled=lambda: False, operator_authorized=False):
    tool = build_publish_tools(project)["publish_" + operation]
    # This native UI is the local operator, not an Agent-selected identity.
    # Use the same declared policy; future ASK/DENY operations must route through
    # the existing permission broker instead of introducing a second grant DB.
    policy = decide_tool_operation(tool, arguments)
    if policy.disposition == PolicyDisposition.DENY or policy.disposition == PolicyDisposition.ASK and not operator_authorized:
        raise PublishError("PUBLISH_AUTHORIZATION_REQUIRED")
    if any(str(key).startswith("_") for key in arguments):
        raise PublishError("PUBLISH_INVALID_ARGUMENTS")
    from backend.core.loop.tool_pipeline import _validate_args
    try:
        _validate_args(arguments, tool.parameters)
    except ValueError as exc:
        raise PublishError("PUBLISH_INVALID_ARGUMENTS") from exc
    prepared = {**tool.prepare_args(arguments), "_publish_actor": "local_operator"}
    if policy.disposition == PolicyDisposition.ASK:
        # ApplicationServices is the trusted native operator boundary. Its
        # explicit action grants this exact invocation through the existing
        # broker; Agent calls still enter ToolPipeline and cannot set this flag.
        from backend.core.loop.permission_broker import create_permission_request, grant_from_decision, consume_exact_grant
        from backend.core.loop.publish_runtime import local_check_runtime
        runtime = local_check_runtime(project, tool, arguments["request_id"], cancelled=cancelled)
        process = runtime.ctx.process
        create_permission_request(process, {"tool_name": tool.name, "arguments": arguments,
            "resource": tool.resources[0], "purpose": "execute the explicitly selected Publish action"},
            {tool.name: tool}, project.workspace_path)
        grant = grant_from_decision(process, process.pending_decision, "allow_once")
        if grant is None or consume_exact_grant(process, tool.name, arguments) is None:
            raise PublishError("PUBLISH_AUTHORIZATION_REQUIRED")
    if cancelled():
        raise PublishError("PUBLISH_RUNTIME_STOPPED")
    if operation == "run":
        if queue is None:
            raise PublishError("PUBLISH_RUNTIME_UNAVAILABLE")
        return queue.submit(project, arguments, lambda: tool.execute(prepared))
    if operation in {"verify", "execute", "package"}:
        from backend.core.loop.publish_runtime import local_check_runtime
        from backend.core.loop.service_runtime import tool_service_scope
        runtime = local_check_runtime(project, tool, arguments["request_id"], cancelled=cancelled)
        with tool_service_scope(cancelled, runtime):
            return tool.execute(prepared)
    return tool.execute(prepared)
