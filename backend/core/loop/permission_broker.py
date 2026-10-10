"""Task-scoped user permission requests and exact resource grants.

This is deliberately not an OS sandbox.  It is the authority bridge between a
model-visible request, a durable user decision, and ToolPipeline's canonical
invocation boundary.  Product intent is shown first; API details remain
available for audit and cryptographic binding.
"""

from __future__ import annotations

import hashlib
import json
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
    for name in ("catalog", "workspace_tools", "registrations", "dynamic_tools", "runner"):
        module_name = f"backend.core.tools.{name}"
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
    if grant.get("process_id") != process.process_id:
        return False
    expires = grant.get("expires_at")
    if expires:
        try:
            deadline = datetime.fromisoformat(str(expires))
            if deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
                return False
        except (ValueError, TypeError):
            return False
    elif grant.get("tool_contract_digest"):
        return False
    if tool is not None:
        # Legacy approvals cannot authorize sensitive native code.
        if grant.get("tool_contract_digest") != tool_contract_digest(tool):
            return not getattr(tool, "approval_per_invocation", False) and not grant.get("tool_contract_digest")
    return True


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
            if per_invocation
            else (item.get("scope") == "task" or item.get("arguments_digest") == digest)
        )
        and (item.get("remaining_uses") is None or int(item.get("remaining_uses") or 0) > 0)
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


def remap_project_paths(process, arguments: dict, effective_workspace: str) -> dict:
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
        resolved = candidate.resolve(strict=False)
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
    target = _canonical_resource(resource)
    effective = Path(workspace_path).resolve(strict=False)
    if isinstance(target, Path) and _is_within(target, effective):
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
    return create_user_decision(process, {
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
    })


@_locked_grants
def grant_from_decision(process, pending: dict, action: str) -> dict | None:
    request = dict(pending.get("permission_request") or {})
    if not request or action not in {"allow_once", "allow_task"}:
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
        and (item.get("remaining_uses") is None or int(item.get("remaining_uses") or 0) > 0)
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


@_locked_grants
def authorize_external_resources(process, tool_name: str, effect: str, arguments: dict,
                                 resources: list[str], *, tool=None) -> tuple[list[str], dict | None]:
    if not resources:
        return [], None
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
            and (item.get("remaining_uses") is None or int(item.get("remaining_uses") or 0) > 0)
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
            return [], info["error_info"]
        matched.append(grant)
        allowed.append(str(Path(str(grant["resource"])).resolve(strict=False)))
    for grant in matched:
        if grant.get("remaining_uses") is not None:
            grant["remaining_uses"] = max(0, int(grant["remaining_uses"]) - 1)
    return list(dict.fromkeys(allowed)), None
