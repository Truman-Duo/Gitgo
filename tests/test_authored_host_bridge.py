import os
from pathlib import Path
from backend.core.loop.execution_contract import data_broker
from backend.core.tools.dynamic_tools import validate_host_authored_definition
from backend.core.tools.authored_bridge import execute_authored_host_python
from backend.core.storage import StorageRuntime
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.agent_tool import AgentTool
from backend.core.loop.permission_broker import arguments_digest
from tests.test_composite_continuation import setup, approve


def definition(root, catalog):
    source = "def run(args):\n    first = call_tool('read_prefix', {})\n    second = call_tool('approved_leaf', {'value': first['value']})\n    return {'done': second['ok']}\n"
    (root / "invented.py").write_text(source, encoding="utf-8")
    spec = validate_host_authored_definition({"name": "invented_flow", "description": "test bridge",
        "parameters": {}, "source_path": "invented.py", "host_tools": ["read_prefix", "approved_leaf"],
        "tests": [{"input": {}, "expected": {"done": True}}]}, str(root), catalog)
    return spec, source


def test_invented_code_uses_real_runner_and_same_leaf_approval(tmp_path_factory, runner_transport_only):
    process, ctx, _, leaf, calls = setup(tmp_path_factory)
    catalog = ctx.artifacts["tool_catalog"]
    spec, source = definition(tmp_path_factory, catalog)
    runtime = StorageRuntime(tmp_path_factory, state_home=Path(os.environ["GITGO_STATE_HOME"]))
    try:
        saved = runtime.save_custom_tool(spec, source)
        saved = runtime.load_custom_tool(spec["name"])
        spec = {**saved["spec"], "source_ref": saved["source_ref"]}
        invented = AgentTool("invented_flow", "invented", {}, lambda args: {}, composite_spec=spec, execution_contract=data_broker("host.composite"))
        catalog[invented.name] = invented
        process.tool_registry.add_scoped(invented.name)
        process.runtime_storage = runtime; ctx.storage = runtime
        call = {"id": "invented-call", "name": invented.name, "args": {}}
        first = ToolPipeline().execute(call, invented, ctx, "execution")
        assert first.suspended and calls == ["prefix"]
        approve(process, leaf)
        resumed = ToolPipeline().execute(call, invented, ctx, "execution")
        assert not resumed.is_error and resumed.data["output"] == {"done": True}
        assert calls == ["prefix", "derived"]
        assert len(resumed.receipt["child_receipt_ids"]) == 2
    finally: runtime.close()


def test_runner_returns_requests_but_cannot_forge_host_facts(tmp_path_factory):
    process, ctx, _, leaf, calls = setup(tmp_path_factory)
    spec, source = definition(tmp_path_factory, ctx.artifacts["tool_catalog"])
    result = execute_authored_host_python({"_dynamic_spec": spec})
    assert result["state"] == "needs_host_call" and not calls
    journal = [{"tool_name": "read_prefix", "arguments_digest": arguments_digest({}), "result": {"value": "mock"}}]
    requested = execute_authored_host_python({"_dynamic_spec": spec, "_host_results": journal})
    assert requested["tool_name"] == "approved_leaf" and requested["arguments"] == {"value": "mock"}
    assert not process.tool_receipts and not process.approval_grants


def test_missing_leaf_is_a_capability_error_before_any_prefix_execution(tmp_path_factory):
    process, ctx, _, leaf, calls = setup(tmp_path_factory)
    catalog = ctx.artifacts['tool_catalog']
    spec, _ = definition(tmp_path_factory, catalog)
    invented = AgentTool('invented_flow', 'invented', {}, lambda args: {}, composite_spec=spec, execution_contract=data_broker("host.composite"))
    process.tool_registry.add_scoped(invented.name)
    process.tool_registry.remove_scoped(leaf.name)
    for missing_catalog in (False, True):
        if missing_catalog:
            catalog.pop(leaf.name)
        result = ToolPipeline().execute({'id': 'call', 'name': invented.name, 'args': {}}, invented, ctx, 'execution')
        assert result.is_error and not result.suspended
        assert result.data['error'] == 'CUSTOM_TOOL_CAPABILITY_REQUIRED'
        assert result.data['effect_state'] == 'not_committed'
        assert result.data['missing_tools'] == [leaf.name]
        assert not calls and not process.approval_grants
        assert not process.runtime_preferences.get('composite_continuations')


def test_host_registration_keeps_asset_but_does_not_mount_unavailable_leaves(tmp_path_factory, runner_transport_only):
    from backend.core.loop.executor import _build_internal_tools
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.tools import ToolRegistry
    _, ctx, _, leaf, _ = setup(tmp_path_factory)
    catalog = ctx.artifacts['tool_catalog']
    spec, _ = definition(tmp_path_factory, catalog)
    p = AgentProcess('supervisor', 'supervisor', RingLevel.RING_3, actor_kind='supervisor',
        active_task_id='task', session=AgentSession(), worktree_path=str(tmp_path_factory),
        tool_registry=ToolRegistry(['author_tool', 'read_prefix']))
    import os
    from pathlib import Path
    runtime = StorageRuntime(tmp_path_factory, state_home=Path(os.environ['GITGO_STATE_HOME']))
    p.runtime_storage = runtime
    try:
        tool = _build_internal_tools(p, catalog, str(tmp_path_factory))['author_tool']
        payload = {'operation': 'register', 'name': spec['name'], 'description': 'Host bridge', 'authority_mode': 'host',
            'parameters': {}, 'source_path': 'invented.py', 'host_tools': spec['host_tools'],
            'tests': [{'input': {}, 'expected': {'done': True}, 'host_results': [
                {'tool_name': 'read_prefix', 'arguments_digest': arguments_digest({}), 'result': {'value': 'mock'}},
                {'tool_name': leaf.name, 'arguments_digest': arguments_digest({'value': 'mock'}), 'result': {'ok': True}}]}]}
        from backend.core.loop.execution_context import ExecutionContext
        context = ExecutionContext(process=p, session=p.session, workspace_path=str(tmp_path_factory),
            cancellation=p.cancellation_event, storage=runtime)
        response = ToolPipeline().execute({"name": tool.name, "args": payload}, tool, context, "registration")
        assert not response.is_error, response.formatted
        result = response.data
        assert result['registered'] and result['mounted'] is False
        assert result['blocked_on'] == 'CUSTOM_TOOL_CAPABILITY_REQUIRED'
        assert result['warnings'] and 'error' not in result
        assert runtime.load_custom_tool(spec['name'])['name'] == spec['name']
        assert spec['name'] not in p.dynamic_tools
        blocked = tool.execute({'operation': 'use', 'name': spec['name']})
        assert blocked['error'] == 'CUSTOM_TOOL_CAPABILITY_REQUIRED'
        p.tool_registry.add_scoped(leaf.name)  # Explicit fixture capability, not approval.
        assert tool.execute({'operation': 'use', 'name': spec['name']})['mounted']
        assert not p.approval_grants
    finally:
        runtime.close()


def test_lease_refresh_preserves_compatible_scoped_tools_without_leaf_grants(tmp_path_factory):
    from backend.core.loop.executor import _build_internal_tools
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.capabilities import CapabilityProfiles
    from backend.core.tools.catalog import build_workspace_tools
    p = AgentProcess('supervisor', 'supervisor', RingLevel.RING_3, actor_kind='supervisor',
        active_task_id='task', session=AgentSession(), worktree_path=str(tmp_path_factory),
        capability_profile_id='supervisor.control', tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools('supervisor.control')))
    spec = {'name': 'saved_bridge', 'execution_mode': 'authored_host_python', 'host_tools': ['read_file', 'shell_script']}
    p.dynamic_tools['saved_bridge'] = AgentTool('saved_bridge', 'fixture registered bridge', {}, lambda args: {}, composite_spec=spec, execution_contract=data_broker("host.composite"))
    catalog = build_workspace_tools(tmp_path_factory)
    tools = _build_internal_tools(p, catalog, str(tmp_path_factory))
    result = tools['request_self_execute'].execute({'profile_id': 'development.workspace',
        'reason': 'Explicit fixture bounded execution', 'intended_actions': ['Invoke registered bridge']})
    assert result['granted'] and p.tool_registry.has('saved_bridge')
    assert p.tool_registry.has('shell_script') and not p.approval_grants
