"""Host boundary tests. Mocked transport cases do not prove OS containment."""
from dataclasses import replace
import pytest

from backend.core.loop.execution_contract import NATIVE_PROCESS, data_broker
from backend.core.loop.dynamic_mount import mounted_tool
from backend.core.loop.composite_runtime import tool_fingerprint
from backend.core.loop.tool_pipeline import ToolPipeline
from tests.test_composite_continuation import setup, approve
from tests.test_authored_host_bridge import definition


@pytest.mark.parametrize('mode,contract', [
    ('host_pipeline', data_broker('host.composite')),
    ('authored_host_python', data_broker('host.composite')),
    ('authored_pure_python', NATIVE_PROCESS),
    ('authored_privileged_python', NATIVE_PROCESS),
])
def test_mount_and_recovery_share_explicit_contract(mode, contract):
    tool = mounted_tool({'name': 'fixture', 'execution_mode': mode})
    assert tool.validate_execution_contract() == contract
    with pytest.raises(RuntimeError, match='requires ToolPipeline'):
        tool({})


@pytest.mark.parametrize('mode', [None, '', 'future_mode', 'host_compute'])
def test_mount_unknown_mode_refused(mode):
    with pytest.raises(ValueError, match='Unknown dynamic execution mode'):
        mounted_tool({'name': 'fixture', 'execution_mode': mode})


@pytest.mark.parametrize('contract', [NATIVE_PROCESS, data_broker('test.changed')])
def test_changed_leaf_authority_rejects_resume_before_replaying_effects(tmp_path_factory, contract):
    process, ctx, outer, leaf, calls = setup(tmp_path_factory)
    call = {'id': 'call', 'name': outer.name, 'args': {}}
    pipeline = ToolPipeline()
    assert pipeline.execute(call, outer, ctx, 'execution').suspended
    approve(process, leaf)
    replacement = replace(leaf, execution_contract=contract)
    assert tool_fingerprint(replacement) != tool_fingerprint(leaf)
    ctx.artifacts['tool_catalog'][leaf.name] = replacement
    result = pipeline.execute(call, outer, ctx, 'execution')
    assert result.is_error and result.diagnostics['code'] == 'COMPOSITE_CONTINUATION_STALE'
    assert calls == ['prefix']
    assert process.approval_grants[-1]['remaining_uses'] == 1


@pytest.mark.parametrize('frame', [
    {}, {'bridge_version': True, 'state': 'completed', 'output': {}},
    {'bridge_version': 1, 'state': 'unknown', 'call_index': 0, 'tool_name': 'read_prefix', 'arguments': {}},
    {'bridge_version': 1, 'state': 'needs_host_call', 'call_index': False, 'tool_name': 'read_prefix', 'arguments': {}},
    {'bridge_version': 1, 'state': 'needs_host_call', 'call_index': 0, 'tool_name': [], 'arguments': {}},
    {'bridge_version': 1, 'state': 'needs_host_call', 'call_index': 0, 'tool_name': 'read_prefix', 'arguments': []},
])
def test_invalid_bridge_frame_never_invokes_host_leaf(tmp_path_factory, monkeypatch, frame):
    process, ctx, _, leaf, calls = setup(tmp_path_factory)
    spec, _ = definition(tmp_path_factory, ctx.artifacts['tool_catalog'])
    tool = mounted_tool(spec)
    process.tool_registry.add_scoped(tool.name)
    pipeline = ToolPipeline()
    monkeypatch.setattr(pipeline, '_execute_isolated', lambda *a, **kw: frame)
    result = pipeline.execute({'id': 'bridge', 'name': tool.name, 'args': {}}, tool, ctx, 'execution')
    assert result.is_error and result.data['error'] == 'AUTHORED_HOST_PROTOCOL_FAILED'
    assert not calls


def test_bridge_native_denial_diagnostics_reach_pipeline(tmp_path_factory, monkeypatch):
    from backend.core.loop.process_tool_runner import ProcessToolRunner, SubprocessResult
    process, ctx, _, leaf, calls = setup(tmp_path_factory)
    spec, _ = definition(tmp_path_factory, ctx.artifacts['tool_catalog'])
    tool = mounted_tool(spec)
    process.tool_registry.add_scoped(tool.name)
    monkeypatch.setattr(ProcessToolRunner, 'run', lambda *a, **kw: SubprocessResult(
        success=False, error='native launch denied', stderr='bounded diagnostic', exit_code=23))
    result = ToolPipeline().execute({'name': tool.name, 'args': {}}, tool, ctx, 'execution')
    assert result.is_error and not calls
    assert result.diagnostics['runner']['exit_code'] == 23
    assert 'bounded diagnostic' in str(result.diagnostics)


def test_old_leaf_grant_does_not_suppress_new_permission(tmp_path_factory):
    process, ctx, outer, leaf, calls = setup(tmp_path_factory)
    approve(process, leaf)
    replacement = replace(leaf, execution_contract=data_broker('test.changed'))
    ctx.artifacts['tool_catalog'][leaf.name] = replacement
    result = ToolPipeline().execute({'name': outer.name, 'args': {}}, outer, ctx, 'execution')
    assert result.suspended and calls == ['prefix']
    assert ctx.artifacts['nested_permission']['tool'] is replacement
    assert process.approval_grants[-1]['remaining_uses'] == 1
