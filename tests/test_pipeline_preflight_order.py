"""Malformed/precondition-blocked calls must not spend an exact user grant."""
import pytest
from backend.core.loop.agent_tool import AgentTool, ToolEffect, ApprovalMode
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.event_bus import EventBus
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.permission_broker import create_permission_request, grant_from_decision, matching_grant


def test_one_use_grant_survives_invalid_arguments_and_missing_prerequisites(tmp_path_factory):
    process = AgentProcess(process_id="preflight", role="worker", ring_level=RingLevel.RING_3,
                           active_task_id="task", worktree_path=str(tmp_path_factory), session=AgentSession())
    process.tool_registry = ToolRegistry(["shell_script"])
    workflow = EngineeringWorkflow(process)
    workflow.configure({"profiles": ["tdd"], "nodes": [
        {"id": "red", "kind": "check", "test_id": "regression", "passed": False},
        {"id": "change", "kind": "change", "files": ["app.py"], "depends_on": ["red"]},
        {"id": "green", "kind": "check", "test_id": "regression", "passed": True, "depends_on": ["change"]},
    ]})
    calls = []
    tool = AgentTool(name="shell_script", description="test", parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                     execute=lambda args: calls.append(args) or {"success": True}, read_only=False,
                     effect=ToolEffect.PROCESS, approval=ApprovalMode.ASK, approval_per_invocation=True)
    args = {"script": "printf approved", "purpose": "preflight acceptance"}
    create_permission_request(process, {"purpose": "preflight acceptance", "tool_name": tool.name,
                                       "arguments": args}, {tool.name: tool}, str(tmp_path_factory))
    assert grant_from_decision(process, process.pending_decision, "allow_once")
    ctx = ExecutionContext(process=process, session=process.session, workspace_path=str(tmp_path_factory), event_bus=EventBus())
    pipeline = ToolPipeline()
    invalid = pipeline.execute({"name": tool.name, "args": args}, tool, ctx, "invalid")
    assert invalid.diagnostics["code"] == "INVALID_TOOL_ARGUMENTS"
    assert matching_grant(process, tool.name, args, per_invocation=True)
    tool.parameters = {"type": "object"}
    blocked = pipeline.execute({"name": tool.name, "args": args}, tool, ctx, "blocked")
    assert blocked.diagnostics["code"] == "ENGINEERING_PREREQUISITE_REQUIRED"
    assert matching_grant(process, tool.name, args, per_invocation=True)
    assert not calls
    process.tool_receipts.append({"receipt_id": "red", "task_id": "task", "succeeded": True, "committed": True,
                                 "tool_name": "run_test", "effect": "process", "test_id": "regression",
                                 "test_passed": False, "test_completed": True, "test_seeds": [42]})
    success = pipeline.execute({"name": tool.name, "args": args}, tool, ctx, "allowed")
    assert not success.is_error and len(calls) == 1
    assert matching_grant(process, tool.name, args, per_invocation=True) is None


def test_search_authority_uses_effective_route_and_overrides_model_arguments(tmp_path_factory):
    process = AgentProcess(process_id="search", role="worker", ring_level=RingLevel.RING_0,
        provider_id="configured-main", active_task_id="task", session=AgentSession())
    process.runtime_preferences.update(effective_provider_id="actual-backup", web_search_mode="provider")
    process.tool_registry = ToolRegistry(["web_search"])
    captured = []
    tool = AgentTool(name="web_search", description="test", parameters={"type": "object"},
        execute=lambda args: captured.append(args) or {"count": 1}, read_only=True,
        effect=ToolEffect.EXTERNAL_READ, approval=ApprovalMode.ALLOW)
    ctx = ExecutionContext(process=process, session=process.session,
                           workspace_path=str(tmp_path_factory), event_bus=EventBus())
    result = ToolPipeline().execute({"name": "web_search", "args": {
        "query": "ripgrep", "_web_search_provider_id": "model-spoofed"}}, tool, ctx, "search-call")
    assert not result.is_error
    assert captured[0]["_web_search_provider_id"] == "actual-backup"
    assert captured[0]["_web_search_mode"] == "provider"


@pytest.mark.parametrize('policy', ['on_demand_v2', 'on_demand_v3'])
def test_stale_semantic_boundary_does_not_consume_exact_grant_or_start_effect(tmp_path_factory, policy):
    from backend.core.loop.alignment_runtime import AlignmentRuntime
    process = AgentProcess(process_id='boundary', role='supervisor', actor_kind='supervisor',
        ring_level=RingLevel.RING_3, active_task_id='task', worktree_path=str(tmp_path_factory), session=AgentSession())
    calls = []
    tool = AgentTool(name='shell_script', description='test', parameters={'type': 'object'},
        execute=lambda args: calls.append(args) or {'success': True}, effect=ToolEffect.PROCESS,
        approval=ApprovalMode.ASK, approval_per_invocation=True)
    process.tool_registry = ToolRegistry([tool.name])
    runtime = AlignmentRuntime(process)
    runtime.apply({'status': 'ready', 'questions': [], 'interpretations': []}, runtime.payload('Run the specified script'))
    process.update_context_snapshot(lambda ctx: {**ctx, 'alignment_runtime': {
        **ctx['alignment_runtime'], 'review_policy': policy}})
    args = {'script': 'printf approved', 'purpose': 'test exact authority'}
    create_permission_request(process, {'purpose': args['purpose'], 'tool_name': tool.name, 'arguments': args},
        {tool.name: tool}, str(tmp_path_factory))
    assert grant_from_decision(process, process.pending_decision, 'allow_once')
    process.task_constraints.append('New execution constraint')
    ctx = ExecutionContext(process=process, session=process.session,
        workspace_path=str(tmp_path_factory), event_bus=EventBus())
    pipeline = ToolPipeline()
    blocked = pipeline.execute({'name': tool.name, 'args': args}, tool, ctx, 'stale')
    assert blocked.diagnostics['code'] == 'ALIGNMENT_REVIEW_REQUIRED'
    assert not calls
    assert matching_grant(process, tool.name, args, per_invocation=True)
    runtime.apply({'status': 'ready', 'questions': [], 'interpretations': []}, runtime.payload())
    result = pipeline.execute({'name': tool.name, 'args': args}, tool, ctx, 'fresh')
    assert not result.is_error and len(calls) == 1
    assert matching_grant(process, tool.name, args, per_invocation=True) is None


def test_private_resource_roots_cannot_escape_through_relative_paths(tmp_path_factory):
    from backend.core.tools.catalog import build_workspace_tools
    root = tmp_path_factory / "workspace"
    root.mkdir()
    (tmp_path_factory / "secret.txt").write_text("outside", encoding="utf-8")
    tools = build_workspace_tools(root)
    p = AgentProcess(process_id="private", role="worker", ring_level=RingLevel.RING_3,
                     active_task_id="task", worktree_path=str(root), session=AgentSession())
    p.tool_registry = ToolRegistry(["read_file"])
    ctx = ExecutionContext(process=p, session=p.session, workspace_path=str(root), event_bus=EventBus())
    result = ToolPipeline().execute({"name": "read_file", "args": {"path": "../secret.txt", "_allowed_roots": [str(tmp_path_factory)], "_workspace": str(tmp_path_factory)}}, tools["read_file"], ctx, "spoof")
    assert result.is_error
    assert "outside" not in str(result.data)


def test_parent_permission_does_not_approve_sensitive_composite_leaf(tmp_path_factory):
    from backend.core.tools.dynamic_tools import validate_definition
    calls = []
    leaf = AgentTool(name="sensitive_leaf", description="sensitive", parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
                     execute=lambda args: calls.append(args) or {"ok": True}, composable=True,
                     effect=ToolEffect.PROCESS, approval=ApprovalMode.ASK, approval_per_invocation=True)
    spec = validate_definition({"name": "wrap_sensitive", "description": "wrap", "parameters": {"type": "object", "properties": {}},
        "steps": [{"id": "leaf", "tool": leaf.name, "arguments": {"value": "approved?"}}]}, {leaf.name: leaf})
    parent = AgentTool(name=spec["name"], description="wrapper", parameters=spec["parameters"], execute=lambda _: {}, effect=spec["effect"], composite_spec=spec)
    p = AgentProcess(process_id="nested", role="worker", ring_level=RingLevel.RING_3, active_task_id="task", worktree_path=str(tmp_path_factory), session=AgentSession())
    p.tool_registry = ToolRegistry([leaf.name, parent.name])
    ctx = ExecutionContext(process=p, session=p.session, workspace_path=str(tmp_path_factory), event_bus=EventBus(),
                           artifacts={"tool_catalog": {leaf.name: leaf, parent.name: parent}, "approvals": {parent.name}})
    result = ToolPipeline().execute({"name": parent.name, "args": {}}, parent, ctx, "nested")
    assert result.suspended and not calls
    assert ctx.artifacts["nested_permission"]["tool"] is leaf
    assert result.data["execution_state"] == "awaiting_user"
