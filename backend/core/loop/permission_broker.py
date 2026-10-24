"""Task-scoped user permission requests and exact resource grants.

This is deliberately not an OS sandbox.  It is the authority bridge between a
model-visible request, a durable user decision, and ToolPipeline's canonical
invocation boundary.  Product intent is shown first; API details remain
available for audit and cryptographic binding.
"""

from __future__ import annotations

import hashlib
import json
import re
import importlib.util
import marshal
import threading
from functools import wraps
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.core.errors import error_payload


PATH_ARGUMENTS = ("path", "cwd")
_GRANT_LOCK = threading.RLock()


def _locked_grants(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        with _GRANT_LOCK:
            return fn(*args, **kwargs)
    return guarded


def arguments_digest(arguments: dict) -> str:
    public = {
        str(key): value for key, value in dict(arguments or {}).items()
        if not str(key).startswith("_")
    }
    encoded = json.dumps(
        public, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _runtime_implementation_digest() -> str:
    """Invalidate built-in approvals when source or frozen handler code changes."""
    digest = hashlib.sha256()
    modules = [f"backend.core.tools.{name}" for name in
               ("catalog", "workspace_tools", "registrations", "dynamic_tools", "runner",
                "file_operations", "file_mutation", "file_mutation_batch", "file_compat",
                "patch_projection", "document_tools", "document_service", "workspace_projection", "web_tools")]
    modules += ["backend.core.loop.agent_tool", "backend.core.loop.tool_wrappers", "backend.core.loop.tool_pipeline", "backend.core.loop.permission_broker",
                "backend.core.loop.authored_runtime", "backend.core.loop.protocol_tools.dynamic_and_completion",
                "backend.core.loop.dynamic_mount", "backend.core.loop.service_runtime", "backend.core.loop.authoring_runtime",
                "backend.core.loop.registered_test_runtime", "backend.core.loop.test_manifest", "backend.core.loop.validation_evidence",
                "backend.core.tools.registered_test", "backend.core.tools.sandbox_pytest", "backend.core.loop.candidate_workspace",
                "backend.core.loop.workspace_candidate_runtime", "backend.core.loop.document_candidate_runtime", "backend.core.loop.file_candidate_runtime",
                "backend.core.loop.promotion", "backend.core.loop.promotion_candidate", "backend.core.loop.worktree",
                "backend.core.loop.manager", "backend.core.loop.git_execution",
                "backend.core.loop.protocol_tools.child_review", "backend.core.knowledge.prewrite",
                "backend.adapters.bounded_local_reader", "backend.core.dependency_graph", "backend.core.authorship", "backend.core.filesystem", "backend.core.contract", "backend.core.storage.metadata_files", "backend.core.storage.runtime", "backend.core.storage.repositories.objects", "backend.core.storage.paths", "backend.core.storage.scope",
                "backend.core.loop.code_dossier", "backend.core.tools.contract_service", "backend.core.tools.governance_service", "backend.core.tools.memory_service",
                "backend.core.tools.memory_restore_service", "backend.core.identity.memory_archive", "backend.core.identity.snapshot", "backend.core.sync_session.state",
                "backend.core.sync_session.persist", "backend.core.sync_session.commit",
                "backend.adapters.local_git_runner", "backend.core.operations.git",
                "backend.core.knowledge.feedback", "backend.core.knowledge.harvest",
                "backend.core.tools.knowledge_service", "backend.core.sandbox_access", "backend.core.windows_access"]
    modules += ["backend.core.sandbox_read", "backend.core.sandbox_read_windows",
                "backend.core.sandbox_read_plan", "backend.core.loop.read_approval",
                "backend.core.loop.execution_contract", "backend.core.loop.process_tool_runner",
                "backend.core.sandbox", "backend.core.sandbox_windows", "backend.core.sandbox_linux",
                "backend.core.child_process", "backend.core.native_host_entry", "backend.core.git_transfer"]
    for module_name in modules:
        spec = importlib.util.find_spec(module_name)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot identify tool implementation: {module_name}")
        source = Path(spec.origin or "")
        if source.is_file() and source.suffix == ".py":
            body = source.read_bytes()
        else:
            code = spec.loader.get_code(module_name)
            if code is None:
                raise RuntimeError(f"Cannot identify frozen tool implementation: {module_name}")
            body = marshal.dumps(code)
        digest.update(module_name.encode())
        digest.update(hashlib.sha256(body).digest())
    return digest.hexdigest()


def tool_contract_digest(tool) -> str:
    """Bind approval to the Host definition, including authored source version."""
    spec = dict(getattr(tool, "composite_spec", None) or {})
    contract = {
        "name": tool.name,
        "runner_name": getattr(tool, "runner_name", ""),
        "timeout": tool.timeout, "timeout_argument": tool.timeout_argument,
        "timeout_default": tool.timeout_default,
        "approval": str(tool.approval), "approval_per_invocation": tool.approval_per_invocation,
        "execution_contract": (
            tool.execution_contract.to_dict()
            if getattr(tool, "execution_contract", None) is not None else None
        ),
        "runtime_implementation": _runtime_implementation_digest(),
        "parameters": tool.parameters,
        "effect": getattr(getattr(tool, "effect", ""), "value", str(getattr(tool, "effect", ""))),
        "resources": sorted(getattr(tool, "resources", None) or []),
        "version": spec.get("version"),
        "source_sha256": spec.get("source_sha256"),
        "definition_digest": spec.get("digest"),
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True, default=str,
                                    separators=(",", ":")).encode()).hexdigest()


def grant_is_current(grant: dict, process, tool=None) -> bool:
    # Old checkpoints remain readable, but unbound/undated approvals never
    # confer execution authority. Require a new user decision instead.
    if not isinstance(grant, dict) or grant.get("process_id") != process.process_id:
        return False
    digest = grant.get("tool_contract_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return False
    expires = grant.get("expires_at")
    if not isinstance(expires, str) or not expires:
        return False
    try:
        deadline = datetime.fromisoformat(expires)
        if deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
            return False
    except (ValueError, TypeError):
        return False
    if tool is not None and digest != tool_contract_digest(tool):
        return False
    from .read_approval import current_pin, requires_read_binding
    if grant.get("read_binding") is not None or (tool is not None and requires_read_binding(tool)):
        if current_pin(process, grant) is None:
            return False
    return True


def _has_remaining_uses(grant: dict) -> bool:
    if "remaining_uses" not in grant:
        return False
    uses = grant.get("remaining_uses")
    return (uses is None and grant.get("scope") == "task") or (type(uses) is int and uses > 0)


@_locked_grants
def matching_grant(process, tool_name: str, arguments: dict, *, per_invocation: bool,
                   consume: bool = False, tool=None) -> dict | None:
    """Find one task-bound grant using the same rule at preflight and execute.

    Keeping this match in one function prevents the Host suspension gate and
    ToolPipeline from disagreeing about whether a decision already authorizes
    the exact call.
    """
    digest = arguments_digest(arguments)
    task_id = process.active_task_id or process.process_id
    grant = next((item for item in list(process.approval_grants or []) if (
        grant_is_current(item, process, tool)
        and item.get("tool_name") == tool_name
        and item.get("task_id") == task_id
        and (
            item.get("arguments_digest") == digest
            if per_invocation or item.get("read_binding") is not None
            else (item.get("scope") == "task" or item.get("arguments_digest") == digest)
        )
        and _has_remaining_uses(item)
    )), None)
    if consume and grant is not None and grant.get("remaining_uses") is not None:
        grant["remaining_uses"] = max(0, int(grant["remaining_uses"]) - 1)
    return grant


def automatic_permission_request(process, tool, arguments: dict, workspace_path: str) -> dict:
    """Create the user-facing decision for a concrete model-requested call."""
    tool_name = str(getattr(tool, "name", "tool"))
    query = str(arguments.get("query") or "").strip()
    purpose = (
        f'search the public web for “{query[:160]}” to continue the current task'
        if tool_name == "web_search" and query
        else f'use {tool_name} to continue the current task'
    )
    declared = [
        str(item) for item in list(getattr(tool, "resources", []) or [])
        if str(item).strip()
    ]
    return create_permission_request(process, {
        "purpose": purpose,
        "tool_name": tool_name,
        "arguments": dict(arguments),
        "resource": declared[0] if declared else f"capability://{tool_name}",
    }, {tool_name: tool}, workspace_path)


def _canonical_resource(raw: str) -> Path | str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("resource is required")
    if "://" in value or value.startswith(("network:", "process:", "capability:")):
        return value
    path = Path(value).resolve(strict=False)
    if path == Path(path.anchor):
        raise ValueError("a filesystem root cannot be approved")
    return path


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def remap_project_paths(process, arguments: dict, effective_workspace: str, *, lexical=False) -> dict:
    """Map logical project-root paths into a B's isolated worktree.

    This is deterministic Host work, not a permission escalation.  A model may
    remember the user checkout's absolute path while the B is correctly running
    in an isolated worktree; both name the same logical project resource.
    """
    args = dict(arguments or {})
    effective = Path(effective_workspace).resolve(strict=False)
    project_root_raw = str(getattr(process, "workspace_root", "") or "")
    if not project_root_raw:
        return args
    project_root = Path(project_root_raw).resolve(strict=False)
    if project_root == effective:
        return args
    for key in PATH_ARGUMENTS:
        raw = args.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            continue
        resolved = candidate if lexical else candidate.resolve(strict=False)
        if _is_within(resolved, project_root):
            args[key] = str(effective / resolved.relative_to(project_root))
    return args


def out_of_scope_resources(arguments: dict, effective_workspace: str) -> list[str]:
    workspace = Path(effective_workspace).resolve(strict=False)
    outside: list[str] = []
    for key in PATH_ARGUMENTS:
        raw = arguments.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            continue
        resolved = candidate.resolve(strict=False)
        if not _is_within(resolved, workspace):
            outside.append(str(resolved))
    return list(dict.fromkeys(outside))


def create_permission_request(process, args: dict, tools: dict, workspace_path: str) -> dict:
    tool_name = str(args.get("tool_name") or "").strip()
    tool = tools.get(tool_name)
    if tool is None:
        visible = sorted(str(name) for name in tools if str(name) != "request_permission")
        raise ValueError(
            f"unknown tool for permission request: {tool_name}; "
            f"available tools: {', '.join(visible[:80])}"
        )
    from backend.core.loop.operation_policy import (
        PolicyDisposition, decide_tool_operation,
    )
    policy = decide_tool_operation(tool, dict(args.get("arguments") or {}))
    if policy.disposition == PolicyDisposition.ALLOW and str(
        getattr(getattr(tool, "effect", ""), "value", getattr(tool, "effect", ""))
    ) == "external_read":
        # A model may still follow an older prompt and explicitly ask before a
        # public read.  Return a useful no-op instead of manufacturing a card
        # or forcing an error-driven retry.
        return {
            "status": "not_required", "allowed": True,
            "tool_name": tool_name, "reason": policy.reason,
        }
    invocation_args = dict(args.get("arguments") or {})
    purpose = str(args.get("purpose") or "").strip()
    if not purpose:
        raise ValueError("purpose is required")
    resource = str(args.get("resource") or "").strip()
    if not resource:
        for key in PATH_ARGUMENTS:
            if invocation_args.get(key):
                resource = str(invocation_args[key])
                break
    if not resource:
        declared = [
            str(item) for item in list(getattr(tool, "resources", []) or [])
            if str(item).strip()
        ]
        resource = declared[0] if declared else f"capability://{tool_name}"
    from .read_approval import requires_read_binding
    bind_read = requires_read_binding(tool)
    if bind_read:
        tool.validate_execution_contract()
    effective = Path(workspace_path).resolve(strict=False)
    if bind_read:
        # Display and authorize the actual remapped leaf, not a capability URI
        # or a relative resource accidentally interpreted against the Host cwd.
        mapped = remap_project_paths(process, invocation_args, workspace_path, lexical=True)
        raw_path = Path(str(mapped.get("path") or ""))
        target = raw_path if raw_path.is_absolute() else effective / raw_path
        if "://" not in resource:
            scope = Path(str(remap_project_paths(process, {"path": resource},
                workspace_path, lexical=True)["path"]))
            scope = scope if scope.is_absolute() else effective / scope
            if not _is_within(target, scope):
                raise ValueError("requested resource does not contain the read target")
    else:
        target = _canonical_resource(resource)
    if isinstance(target, Path) and _is_within(target, effective) and not (bind_read and policy.requires_user):
        raise ValueError("resource is already inside the current execution scope")
    effect = getattr(getattr(tool, "effect", "read"), "value", str(getattr(tool, "effect", "read")))
    invocation_only = bool(getattr(tool, "approval_per_invocation", False))
    request_id = str(uuid.uuid4())
    technical = {
        "request_id": request_id,
        "tool_contract_digest": tool_contract_digest(tool),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        "tool_name": tool_name,
        "effect": effect,
        "resource": str(target),
        "arguments_digest": arguments_digest(invocation_args),
        "approval_scope": "exact_invocation" if invocation_only else "resource_or_task",
    }
    from backend.core.loop.decision_support import create_user_decision
    decision_args = {
        "kind": "permission",
        "state_topic": f"permission:{tool_name}:{target}",
        "question": f"Allow Gitgo to {purpose}?",
        "why_user_must_decide": (
            "This operation reaches beyond the Agent's current project scope. "
            "Gitgo cannot expand that scope without your explicit approval."
        ),
        "options": [
            {
                "label": "Allow once",
                "principle": "Grant only this exact invocation.",
                "immediate_effect": f"Gitgo may use {tool_name} on {target} once.",
                "downstream_effect": "A later invocation must ask again.",
                "risks": f"The operation has effect={effect} on the stated resource.",
                "reversibility": "The grant expires after one attempted invocation.",
                "recommended": effect == "read",
                "action": "allow_once",
            },
            *([] if invocation_only else [{
                "label": "Allow for this task",
                "principle": "Grant the same tool and exact resource until this task ends or 30 minutes elapse.",
                "immediate_effect": f"Repeated {tool_name} calls may use {target}.",
                "downstream_effect": "The grant is not inherited by other tasks or processes.",
                "risks": "Repeated access within the displayed scope is possible.",
                "reversibility": "Stop the task to revoke the remaining grant.",
                "recommended": False,
                "action": "allow_task",
            }]),
            {
                "label": "Deny",
                "principle": "Keep the current project boundary unchanged.",
                "immediate_effect": "The requested operation will not run.",
                "downstream_effect": "Gitgo must adjust the plan or report the limitation.",
                "risks": "The requested task may remain incomplete.",
                "reversibility": "A new, narrower permission can be requested later.",
                "recommended": effect != "read",
                "action": "deny",
            },
        ],
        "allow_free_form": True,
        "permission_request": {
            **technical,
            "purpose": purpose,
            "api_details": f"{tool_name} · {effect} · {target}",
            "arguments_preview": _arguments_preview(invocation_args),
        },
    }
    if not bind_read:
        return create_user_decision(process, decision_args)
    if _is_within(target, effective):
        decision_args["why_user_must_decide"] = "This tool requires approval for the displayed file read."
    from .read_approval import publish_read_request
    prepared = remap_project_paths(process, invocation_args, workspace_path, lexical=True)
    prepared = {**prepared, "_workspace": workspace_path,
                "_allowed_roots": [str(target)] if isinstance(target, Path) else []}
    return publish_read_request(process, decision_args, prepared, create_user_decision)


@_locked_grants
def grant_from_decision(process, pending: dict, action: str) -> dict | None:
    request = dict(pending.get("permission_request") or {})
    if not request or action not in {"allow_once", "allow_task"}:
        from .read_approval import revoke_read_request
        revoke_read_request(process, request.get("request_id", ""))
        return None
    if request.get("read_binding") is not None:
        from .read_approval import current_pin
        if pending is not process.pending_decision or current_pin(process, request) is None:
            return None
    if request.get("approval_scope") == "exact_invocation" and action != "allow_once":
        return None
    grant = {
        "grant_id": str(uuid.uuid4()),
        "request_id": str(request.get("request_id") or ""),
        "task_id": process.active_task_id or process.process_id,
        "process_id": process.process_id,
        "tool_name": str(request.get("tool_name") or ""),
        "effect": str(request.get("effect") or ""),
        "resource": str(_canonical_resource(str(request.get("resource") or ""))),
        "arguments_digest": str(request.get("arguments_digest") or ""),
        "scope": "once" if action == "allow_once" else "task",
        "remaining_uses": 1 if action == "allow_once" else None,
        "tool_contract_digest": str(request.get("tool_contract_digest") or ""),
        "expires_at": str(request.get("expires_at") or ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if request.get("read_binding") is not None:
        grant["resource"] = request["resource"]
        grant["read_binding"] = request["read_binding"]
        grant["approval_scope"] = request["approval_scope"]
        from .read_approval import bind_read_grant
        if not bind_read_grant(process, grant):
            return None
    if not grant_is_current(grant, process):
        return None
    process.approval_grants.append(grant)
    return grant


@_locked_grants
def consume_exact_grant(process, tool_name: str, arguments: dict) -> dict | None:
    """Consume one exact, task-bound approval at a non-ToolPipeline admission gate.

    Privileged tool registration has to validate source integrity before any
    code is run.  This helper keeps that special admission step on the same
    durable grant model as ordinary sensitive tool invocations.
    """
    digest = arguments_digest(arguments)
    task_id = process.active_task_id or process.process_id
    grant = next((item for item in list(process.approval_grants or []) if (
        grant_is_current(item, process)
        and item.get("task_id") == task_id
        and item.get("tool_name") == tool_name
        and item.get("arguments_digest") == digest
        and _has_remaining_uses(item)
    )), None)
    if grant is None:
        return None
    if grant.get("remaining_uses") is not None:
        grant["remaining_uses"] = max(0, int(grant["remaining_uses"]) - 1)
    return grant


def _arguments_preview(arguments: dict, *, limit: int = 1200) -> str:
    """Bounded, stable text shown to the user before approving an invocation."""
    public = {
        str(key): value for key, value in dict(arguments or {}).items()
        if not str(key).startswith("_")
    }
    rendered = json.dumps(public, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    return rendered if len(rendered) <= limit else rendered[:limit] + "\n…"


def _select_external_resources(process, tool_name: str, effect: str, arguments: dict,
                                 resources: list[str], *, tool=None) -> tuple[list[str], list[dict], dict | None]:
    if not resources:
        return [], [], None
    digest = arguments_digest(arguments)
    allowed: list[str] = []
    matched: list[dict] = []
    for raw_resource in resources:
        target = Path(raw_resource).resolve(strict=False)
        grant = next((item for item in list(process.approval_grants or []) if (
            grant_is_current(item, process, tool)
            and item.get("task_id") == (process.active_task_id or process.process_id)
            and item.get("tool_name") == tool_name
            and item.get("effect") == effect
            and _is_within(target, Path(str(item.get("resource") or "")).resolve(strict=False))
            and (item.get("scope") == "task" or item.get("arguments_digest") == digest)
            and _has_remaining_uses(item)
        )), None)
        if grant is None:
            info = error_payload(
                "RESOURCE_SCOPE_APPROVAL_REQUIRED",
                details={
                    "tool_name": tool_name, "effect": effect,
                    "resource": str(target), "arguments_digest": digest,
                },
                next_actions=[{
                    "action": "request_permission",
                    "tool_name": tool_name,
                    "resource": str(target),
                }],
            )
            return [], [], info["error_info"]
        matched.append(grant)
        allowed.append(str(Path(str(grant["resource"])).resolve(strict=False)))
    return list(dict.fromkeys(allowed)), matched, None


@_locked_grants
def authorize_external_resources(process, tool_name: str, effect: str, arguments: dict,
                                 resources: list[str], *, tool=None) -> tuple[list[str], dict | None]:
    """Preview scope without spending approval; admission must recheck it.

    This result is only for preflight. It does not authorize a later execution.
    """
    roots, _, error = _select_external_resources(process, tool_name, effect, arguments, resources, tool=tool)
    return roots, error


@_locked_grants
def admit_invocation_grants(process, tool, raw_arguments: dict, prepared_arguments: dict,
                            resources: list[str], *, require_explicit: bool, admission=None) -> tuple[list[str], dict | None]:
    """Recheck and spend the complete invocation selection under the grant lock.

    One grant may cover both explicit approval and several resource paths. All
    required grants must still be current before any is spent; each selected
    grant is spent once. This is in-process atomicity only, not durable launch
    admission or proof that an OS backend can enforce the resulting scope.
    """
    tool.validate_execution_contract()
    selected = []
    if require_explicit:
        grant = matching_grant(process, tool.name, raw_arguments,
            per_invocation=bool(tool.approval_per_invocation), tool=tool)
        if grant is None:
            return [], {"name": "APPROVAL_GRANT_INVALID", "message": "approval is no longer available"}
        selected.append(grant)
    effect = getattr(tool.effect, "value", str(tool.effect))
    roots, resource_grants, error = _select_external_resources(
        process, tool.name, effect, prepared_arguments, resources, tool=tool)
    if error is not None:
        return [], error
    selected.extend(resource_grants)
    from .read_approval import admit_read_binding
    error = admit_read_binding(process, tool, raw_arguments,
        {**prepared_arguments, "_allowed_roots": roots}, selected, admission)
    if error is not None:
        return [], error
    # Deduplicate the Host-owned record, not tool JSON or a supplied grant ID.
    for grant in {id(item): item for item in selected}.values():
        if grant["remaining_uses"] is not None:
            grant["remaining_uses"] -= 1
    return roots, None
