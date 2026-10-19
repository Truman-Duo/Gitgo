"""A pure invented program drives existing capabilities through the Host."""
import hashlib
import time
from .composite_runtime import state_for, new_state, call_key
from .permission_broker import matching_grant, arguments_digest
from .operation_policy import decide_tool_operation


def run(pipeline, tool, args, ctx, execution_id, call_index, key, spec, *, timeout, diagnostics, runtime_metadata):
    catalog = ctx.artifacts.get("tool_catalog") or {}
    from .tool_surface import component_blocker
    blocker = component_blocker(ctx.process, spec, catalog)
    if blocker:
        previous = ctx.process.runtime_preferences.get('composite_continuations', {}).get(key) or {}
        return {**blocker, 'effect_state': 'partial' if previous.get('receipts') else 'not_committed'}
    inputs = {k: v for k, v in args.items() if not str(k).startswith("_")}
    state = state_for(ctx.process, key, tool, inputs, catalog) or new_state(
        ctx.process, key, tool, inputs, catalog, spec["host_tools"])
    runtime_metadata["child_receipts"] = state["receipts"]
    journal = state.setdefault("host_results", [])
    child_execution = execution_id + "__" + hashlib.sha256(key.encode()).hexdigest()[:16]
    deadline = time.monotonic() + timeout
    def failed(code, detail):
        runtime_metadata["effect_state"] = "partial" if state["receipts"] else "ambiguous"
        # Keep committed prefix evidence on protocol/launch failures. It must
        # remain inspectable and must never be replayed as a fresh operation.
        return {"error": code, "detail": detail,
            "effect_state": "partial" if state["receipts"] else "ambiguous",
            "_child_receipts": state["receipts"], "files": state["files"]}
    for _ in range(21):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return failed("AUTHORED_HOST_TIMEOUT", "The Host bridge exhausted its invocation time allowance")
        response = pipeline._execute_isolated("authored_host_python",
            {**inputs, "_workspace": str(ctx.workspace_path), "_dynamic_spec": spec, "_host_results": journal},
            remaining, cancellation_event=ctx.cancellation, diagnostics=diagnostics, runtime_metadata=runtime_metadata)
        cleanup = runtime_metadata.get("cleanup")
        if isinstance(cleanup, dict) and (cleanup.get("complete") is not True or cleanup.get("issues")):
            return failed("PROCESS_CLEANUP_INCOMPLETE", "Bridge cleanup is unconfirmed; no further Host calls may start")
        if not isinstance(response, dict) or type(response.get("bridge_version")) is not int or response["bridge_version"] != 1:
            return failed("AUTHORED_HOST_PROTOCOL_FAILED", str(response.get("error") or "invalid bridge frame")
                if isinstance(response, dict) else "invalid bridge frame")
        if response.get("state") == "completed" and "output" in response:
            ctx.process.runtime_preferences["composite_continuations"].pop(key, None)
            return {"completed": True, "output": response["output"], "_child_receipts": state["receipts"], "files": state["files"]}
        index, name, resolved = response.get("call_index"), response.get("tool_name"), response.get("arguments")
        if (response.get("state") != "needs_host_call" or type(index) is not int
                or not isinstance(name, str) or not isinstance(resolved, dict)):
            return failed("AUTHORED_HOST_PROTOCOL_FAILED", "invalid Host call frame")
        if index != len(journal) or index >= 20 or name not in spec["host_tools"] or name not in catalog:
            return failed("AUTHORED_HOST_SEQUENCE_CHANGED", "requested call is outside the declared sequence")
        base = catalog[name]
        child_key = call_key({"id": str(index)}, child_execution, index)
        resuming = state_for(ctx.process, child_key, base, resolved, catalog) if base.composite_spec else None
        if decide_tool_operation(base, resolved).requires_user and resuming is None and matching_grant(
                ctx.process, name, resolved, per_invocation=base.approval_per_invocation, tool=base) is None:
            ctx.artifacts["nested_permission"] = {"tool": base, "arguments": resolved}
            runtime_metadata["suspended"] = True
            return {"_suspended": True}
        child = pipeline.execute({"name": name, "args": resolved, "id": str(index)}, base, ctx, child_execution, index)
        if child.suspended:
            runtime_metadata["suspended"] = True
            return {"_suspended": True}
        receipt = {**dict(child.receipt), "composite_tool": tool.name, "composite_step": str(index)}
        state["receipts"].append(receipt)
        if receipt.get("receipt_id"):
            ctx.process.tool_receipts.append(receipt)
        if child.is_error:
            runtime_metadata["effect_state"] = "partial"
            ctx.process.runtime_preferences["composite_continuations"].pop(key, None)
            return {"error": "AUTHORED_HOST_LEAF_FAILED", "step_diagnostics": child.diagnostics,
                "effect_state": "partial", "_child_receipts": state["receipts"], "detail": child.error}
        data = dict(child.data or {})
        state["files"] = sorted(set(state["files"]) | pipeline._result_files(data))
        journal.append({"tool_name": name, "arguments_digest": arguments_digest(resolved), "result": data})
    return failed("AUTHORED_HOST_CALL_LIMIT", "Host bridge call limit exceeded")
