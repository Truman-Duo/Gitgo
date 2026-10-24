from backend.core.loop.execution_contract import data_broker
import pytest

from backend.core.loop.agent_tool import AgentTool, ApprovalMode
from backend.core.loop.event_bus import EventBus
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tool_execution import ToolExecution
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.permission_broker import arguments_digest
from backend.core.loop.manager import SessionStore
from backend.core.tools.dynamic_tools import validate_definition


def setup(root):
    calls = []
    prefix = AgentTool("read_prefix", "prefix", {}, lambda args: calls.append("prefix") or {"value": "derived"}, composable=True, execution_contract=data_broker("test.fixture"))
    leaf = AgentTool("approved_leaf", "leaf", {"value": {"type": "string"}},
        lambda args: calls.append(args["value"]) or {"ok": True}, composable=True,
        approval=ApprovalMode.ASK, approval_per_invocation=True, execution_contract=data_broker("test.fixture"))
    catalog = {prefix.name: prefix, leaf.name: leaf}
    spec = validate_definition({"name": "read_then_approved", "description": "test", "parameters": {}, "steps": [
        {"id": "prefix", "tool": prefix.name, "arguments": {}},
        {"id": "approved", "tool": leaf.name, "arguments": {"value": "${steps.prefix.value}"}}]}, catalog)
    outer = AgentTool(spec["name"], "wrapper", {}, lambda args: {}, composite_spec=spec, execution_contract=data_broker("host.composite"))
    catalog[outer.name] = outer
    process = AgentProcess("p", "worker", RingLevel.RING_3, session=AgentSession(),
        active_task_id="task", tool_registry=ToolRegistry(list(catalog)), worktree_path=str(root))
    ctx = ExecutionContext(process=process, session=process.session, workspace_path=str(root),
        event_bus=EventBus(), artifacts={"tool_catalog": catalog})
    return process, ctx, outer, leaf, calls


def approve(process, leaf, value="derived"):
    from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
    create_permission_request(process, {"tool_name": leaf.name, "arguments": {"value": value},
        "purpose": "Approve the exact fixture call"}, {leaf.name: leaf}, process.worktree_path)
    assert grant_from_decision(process, process.pending_decision, "allow_once")


def test_prefix_survives_checkpoint_and_runs_once(tmp_path_factory):
    process, ctx, outer, leaf, calls = setup(tmp_path_factory)
    call = {"id": "provider-call", "name": outer.name, "args": {}}
    first = ToolPipeline().execute(call, outer, ctx, "execution")
    assert first.suspended and calls == ["prefix"]
    assert ctx.artifacts["nested_permission"]["arguments"] == {"value": "derived"}
    store = SessionStore(str(tmp_path_factory), state_home=tmp_path_factory / "state")
    try:
        store.save_process_checkpoint(process)
        saved = store.load_process_state(process.process_id)
        process.runtime_preferences = saved["runtime_state"]["runtime_preferences"]
        process.tool_receipts = saved["receipts"]
        approve(process, leaf)
        resumed = ToolPipeline().execute(call, outer, ctx, "execution")
        assert not resumed.is_error and not resumed.suspended
        assert calls == ["prefix", "derived"]
        assert process.approval_grants[0]["remaining_uses"] == 0
        assert not process.runtime_preferences["composite_continuations"]
    finally: store.close()


@pytest.mark.parametrize("change", ["definition", "arguments", "scope", "receipt", "expiry"])
def test_stale_continuation_cannot_repeat_prefix(tmp_path_factory, change):
    process, ctx, outer, leaf, calls = setup(tmp_path_factory)
    call = {"id": "provider-call", "name": outer.name, "args": {}}
    assert ToolPipeline().execute(call, outer, ctx, "execution").suspended
    approve(process, leaf)
    if change == "definition": leaf.parameters["properties"]["other"] = {"type": "string"}
    if change == "arguments": call["args"] = {"new": True}
    if change == "scope": process.task_constraints.append("new requirement")
    if change == "receipt": process.tool_receipts.clear()
    if change == "expiry": next(iter(process.runtime_preferences["composite_continuations"].values()))["expires_at"] = 0
    result = ToolPipeline().execute(call, outer, ctx, "execution")
    assert result.is_error and result.diagnostics["code"] == "COMPOSITE_CONTINUATION_STALE"
    assert calls == ["prefix"]


def test_ordered_batch_does_not_start_suffix_while_leaf_waits(tmp_path_factory):
    process, ctx, outer, leaf, calls = setup(tmp_path_factory)
    suffix = AgentTool("later_call", "later", {}, lambda args: calls.append("later") or {}, execution_contract=data_broker("test.fixture"))
    catalog = {**ctx.artifacts["tool_catalog"], suffix.name: suffix}
    execution = ToolExecution("execution", ctx, tool_calls=[{"id": "root", "name": outer.name, "args": {}}, {"name": suffix.name, "args": {}}])
    execution.begin()
    try:
        results = execution.execute_batch(catalog)
        assert len(results) == 1 and results[0].suspended and calls == ["prefix"]
    finally: execution.commit()
