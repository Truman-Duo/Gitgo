"""A pure invented program drives existing capabilities through the Host."""
import hashlib
from .composite_runtime import state_for, new_state, call_key
from .permission_broker import matching_grant, arguments_digest
from .operation_policy import decide_tool_operation


def run(pipeline, tool, args, ctx, execution_id, call_index, key, spec):
    catalog = ctx.artifacts.get("tool_catalog") or {}
    from .tool_surface import component_blocker
    blocker = component_blocker(ctx.process, spec, catalog)
    if blocker:
        previous = ctx.process.runtime_preferences.get('composite_continuations', {}).get(key) or {}
        return {**blocker, 'effect_state': 'partial' if previous.get('receipts') else 'not_committed'}
    inputs = {k: v for k, v in args.items() if not str(k).startswith("_")}
    state = state_for(ctx.process, key, tool, inputs, catalog) or new_state(
        ctx.process, key, tool, inputs, catalog, spec["host_tools"])
    journal = state.setdefault("host_results", [])
    child_execution = execution_id + "__" + hashlib.sha256(key.encode()).hexdigest()[:16]
    for _ in range(21):
        response = pipeline._execute_isolated("authored_host_python",
            {**inputs, "_workspace": str(ctx.workspace_path), "_dynamic_spec": spec, "_host_results": journal},
            tool.timeout, cancellation_event=ctx.cancellation)
        if response.get("bridge_version") != 1:
            return {"error": "AUTHORED_HOST_PROTOCOL_FAILED", "detail": str(response.get("error") or "invalid bridge frame")}
        if response["state"] == "completed":
            ctx.process.runtime_preferences["composite_continuations"].pop(key, None)
            return {"completed": True, "output": response["output"], "_child_receipts": state["receipts"], "files": state["files"]}
        index, name, resolved = response["call_index"], response["tool_name"], response["arguments"]
        if index != len(journal) or name not in spec["host_tools"] or name not in catalog:
            return {"error": "AUTHORED_HOST_SEQUENCE_CHANGED"}
        base = catalog[name]
        child_key = call_key({"id": str(index)}, child_execution, index)
        resuming = state_for(ctx.process, child_key, base, resolved, catalog) if base.composite_spec else None
        if decide_tool_operation(base, resolved).requires_user and resuming is None and matching_grant(
                ctx.process, name, resolved, per_invocation=base.approval_per_invocation) is None:
            ctx.artifacts["nested_permission"] = {"tool": base, "arguments": resolved}
            return {"_suspended": True}
        child = pipeline.execute({"name": name, "args": resolved, "id": str(index)}, base, ctx, child_execution, index)
        if child.suspended:
            return {"_suspended": True}
        receipt = {**dict(child.receipt), "composite_tool": tool.name, "composite_step": str(index)}
        state["receipts"].append(receipt)
        if receipt.get("receipt_id"):
            ctx.process.tool_receipts.append(receipt)
        if child.is_error:
            ctx.process.runtime_preferences["composite_continuations"].pop(key, None)
            return {"error": "AUTHORED_HOST_LEAF_FAILED", "step_diagnostics": child.diagnostics,
                "effect_state": "partial", "_child_receipts": state["receipts"], "detail": child.error}
        data = dict(child.data or {})
        state["files"] = sorted(set(state["files"]) | pipeline._result_files(data))
        journal.append({"tool_name": name, "arguments_digest": arguments_digest(resolved), "result": data})
    return {"error": "AUTHORED_HOST_CALL_LIMIT", "_child_receipts": state["receipts"]}
