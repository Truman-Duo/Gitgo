"""Host-owned continuation for nested, approval-gated declarative calls."""
from __future__ import annotations

import hashlib
import json
import time
import math

from .completion_gates import contract_digest
from .permission_broker import arguments_digest, matching_grant, tool_contract_digest


def tool_fingerprint(tool):
    tool.validate_execution_contract()
    body = {"execution_contract_digest": tool_contract_digest(tool), "name": tool.name, "parameters": tool.parameters,
        "effect": str(tool.effect), "approval": str(tool.approval),
        "per_invocation": tool.approval_per_invocation, "runner": tool.runner_name,
        "definition": tool.composite_spec or {}, "resources": tool.resources or []}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def call_key(call, execution_id, call_index):
    return execution_id + ":" + str(call.get("id") or call_index)


def state_for(process, key, tool, inputs, catalog):
    state = process.runtime_preferences.get("composite_continuations", {}).get(key)
    if state is None:
        return None
    expiry = float(state.get("expires_at", 0))
    if (not math.isfinite(expiry) or state.get("outer_authorized") is not True
            or state.get("task_id") != (process.active_task_id or process.process_id)
            or state.get("process_id") != process.process_id
            or state.get("contract_digest") != contract_digest(process)
            or state.get("tool_fingerprint") != tool_fingerprint(tool)
            or state.get("arguments_digest") != arguments_digest(inputs)
            or expiry <= time.time()):
        raise ValueError("COMPOSITE_CONTINUATION_STALE: existing effects retained; inspect before retry")
    for name, fingerprint in state.get("components", {}).items():
        if name not in catalog or tool_fingerprint(catalog[name]) != fingerprint:
            raise ValueError("COMPOSITE_COMPONENT_CHANGED: existing effects retained; inspect before retry")
    known = {r.get("receipt_id") for r in process.tool_receipts}
    if any(r.get("receipt_id") not in known for r in state["receipts"]):
        raise ValueError("COMPOSITE_RECEIPT_MISSING: committed prefix cannot be replayed")
    return state


def discard(process, execution_id):
    states = process.runtime_preferences.get("composite_continuations", {})
    for key in list(states):
        if key.startswith(execution_id + ":") or key.startswith(execution_id + "__"):
            states.pop(key)


def new_state(process, key, tool, inputs, catalog, components):
    state = {"process_id": process.process_id, "task_id": process.active_task_id or process.process_id,
        "contract_digest": contract_digest(process), "tool_fingerprint": tool_fingerprint(tool),
        "arguments_digest": arguments_digest(inputs), "expires_at": time.time() + 1800,
        "components": {name: tool_fingerprint(catalog[name]) for name in components},
        "cursor": 0, "steps": {}, "receipts": [], "files": [], "outer_authorized": True}
    process.runtime_preferences.setdefault("composite_continuations", {})[key] = state
    return state


def run(pipeline, tool, args, ctx, execution_id, call_index, key):
    from backend.core.tools.dynamic_tools import resolve_step_arguments
    from .operation_policy import decide_tool_operation, is_effectful_mutation
    from .event_bus import ToolEvent

    catalog = ctx.artifacts.get("tool_catalog") or {}
    inputs = {k: v for k, v in args.items() if not str(k).startswith("_")}
    spec = tool.composite_spec or {}
    from .tool_surface import component_blocker
    blocker = component_blocker(ctx.process, spec, catalog)
    if blocker:
        previous = ctx.process.runtime_preferences.get('composite_continuations', {}).get(key) or {}
        return {**blocker, 'effect_state': 'partial' if previous.get('receipts') else 'not_committed'}
    state = state_for(ctx.process, key, tool, inputs, catalog)
    if state is None:
        state = {"process_id": ctx.process.process_id,
            "task_id": ctx.process.active_task_id or ctx.process.process_id,
            "contract_digest": contract_digest(ctx.process), "tool_fingerprint": tool_fingerprint(tool),
            "arguments_digest": arguments_digest(inputs), "expires_at": time.time() + 1800,
            "components": {s["tool"]: tool_fingerprint(catalog[s["tool"]]) for s in spec.get("steps", []) if s["tool"] in catalog},
            "cursor": 0, "steps": {}, "receipts": [], "files": [], "outer_authorized": True}
    states = ctx.process.runtime_preferences.setdefault("composite_continuations", {})
    states[key] = state
    initial_receipt_count = len(state["receipts"])
    child_execution = execution_id + "__" + hashlib.sha256(key.encode()).hexdigest()[:16]
    for index in range(state["cursor"], len(spec.get("steps", []))):
        step = spec["steps"][index]; step_id = step["id"]; name = step["tool"]
        base = catalog.get(name)
        if base is None or base is tool:
            return {"error": "DYNAMIC_COMPONENT_UNAVAILABLE", "failed_step": step_id}
        resolved = resolve_step_arguments(step.get("arguments", {}), inputs=inputs, steps=state["steps"])
        policy = decide_tool_operation(base, resolved)
        child_key = call_key({"id": step_id}, child_execution, index)
        resumed_child = state_for(ctx.process, child_key, base, resolved, catalog) if base.composite_spec else None
        if policy.requires_user and resumed_child is None and matching_grant(ctx.process, name, resolved,
                per_invocation=base.approval_per_invocation, tool=base) is None:
            ctx.artifacts["nested_permission"] = {"tool": base, "arguments": resolved}
            return {"_suspended": True}
        child = pipeline.execute({"name": name, "args": resolved, "id": step_id}, base, ctx, child_execution, index)
        if child.suspended:
            return {"_suspended": True}
        receipt = {**dict(child.receipt or {}), "composite_tool": tool.name, "composite_step": step_id}
        state["receipts"].append(receipt)
        # Child facts are durable before the root can wait on another permission.
        if receipt.get("receipt_id") and not any(r.get("receipt_id") == receipt["receipt_id"] for r in ctx.process.tool_receipts):
            ctx.process.tool_receipts.append(receipt)
        data = dict(child.data or {}); state["steps"][step_id] = data
        state["files"] = sorted(set(state["files"]) | pipeline._result_files(data))
        state["cursor"] = index + 1
        ctx.event_bus.emit(ToolEvent("CompositeStepCompleted", child_execution, name, index,
            data={"composite_tool": tool.name, "step_id": step_id, "is_error": child.is_error,
                  "error": child.error, "duration_ms": child.duration_ms, "receipt": receipt}))
        if child.is_error:
            states.pop(key, None)
            return {"error": "DYNAMIC_STEP_FAILED", "failed_step": step_id, "step_error": child.error,
                "step_diagnostics": child.diagnostics, "steps": state["steps"], "_child_receipts": state["receipts"],
                "files": state["files"], "_rollback_required": any(
                    is_effectful_mutation(r.get("effect", "read")) and r.get("committed") for r in state["receipts"][initial_receipt_count:]),
                "retained_prefix_receipt_ids": [r.get("receipt_id") for r in state["receipts"][:initial_receipt_count]]}
    states.pop(key, None)
    return {"completed": True, "definition_digest": spec.get("digest", ""), "definition_version": spec.get("version", 1),
        "steps": state["steps"], "_child_receipts": state["receipts"], "files": state["files"]}
