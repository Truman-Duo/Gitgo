"""Malformed/precondition-blocked calls must not spend an exact user grant."""
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
    args = {"script": "printf approved", "purpose": "preflight acceptance", "path": "app.py"}
    # Preserve the exact approved schema across retries; prepare a malformed
    # call first without changing the contract to which approval is bound.
    tool.prepare_args = lambda values: {k: v for k, v in values.items() if k != "path"}
    create_permission_request(process, {"purpose": "preflight acceptance", "tool_name": tool.name,
                                       "arguments": args}, {tool.name: tool}, str(tmp_path_factory))
    assert grant_from_decision(process, process.pending_decision, "allow_once")
    ctx = ExecutionContext(process=process, session=process.session, workspace_path=str(tmp_path_factory), event_bus=EventBus())
    pipeline = ToolPipeline()
    invalid = pipeline.execute({"name": tool.name, "args": args}, tool, ctx, "invalid")
    assert invalid.diagnostics["code"] == "INVALID_TOOL_ARGUMENTS"
    assert matching_grant(process, tool.name, args, per_invocation=True)
    tool.prepare_args = lambda values: dict(values)
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



def test_exact_grant_rechecks_contract_changed_during_preparation(tmp_path_factory):
    process = AgentProcess(process_id='changed-contract', role='worker', ring_level=RingLevel.RING_3,
                           active_task_id='task', worktree_path=str(tmp_path_factory), session=AgentSession())
    process.tool_registry = ToolRegistry(['shell_script'])
    calls = []
    tool = AgentTool(name='shell_script', description='test', parameters={'type': 'object'},
                     execute=lambda args: calls.append(args), read_only=False,
                     effect=ToolEffect.PROCESS, approval=ApprovalMode.ASK, approval_per_invocation=True,
                     resources=['process:shell'])
    args = {'script': 'approved', 'purpose': 'contract revalidation'}
    create_permission_request(process, {'purpose': args['purpose'], 'tool_name': tool.name,
                                       'arguments': args}, {tool.name: tool}, str(tmp_path_factory))
    assert grant_from_decision(process, process.pending_decision, 'allow_once')
    def changed(values):
        tool.resources = ['process:changed']
        return dict(values)
    tool.prepare_args = changed
    ctx = ExecutionContext(process=process, session=process.session, workspace_path=str(tmp_path_factory), event_bus=EventBus())
    result = ToolPipeline().execute({'name': tool.name, 'args': args}, tool, ctx, 'changed')
    assert result.is_error and result.diagnostics['code'] == 'APPROVAL_GRANT_INVALID'
    assert not calls
    assert process.approval_grants[0]['remaining_uses'] == 1
