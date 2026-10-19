"""Permission protocol tests; these fixtures do not prove OS containment."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from backend.core.loop.agent_tool import AgentTool, ApprovalMode
from backend.core.loop.execution_contract import data_broker
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.gate import GateResult
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
from backend.core.loop.session import AgentSession
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tools import ToolRegistry


def fixture(root, *, sensitive=False, external=True, required=()):
    workspace = root / 'project'
    workspace.mkdir()
    target = root / 'outside'
    target.mkdir()
    calls = []
    tool = AgentTool('read_fixture', 'permission fixture',
        {'type': 'object', 'properties': {'path': {'type': 'string'},
            'cwd': {'type': 'string'}, 'purpose': {'type': 'string'}}, 'required': list(required)},
        lambda args: calls.append(dict(args)) or {'ok': True},
        execution_contract=data_broker('test.fixture'),
        approval=ApprovalMode.ASK if sensitive else ApprovalMode.ALLOW,
        approval_per_invocation=sensitive)
    process = AgentProcess('admission', 'worker', RingLevel.RING_3,
        active_task_id='task', session=AgentSession(), worktree_path=str(workspace),
        tool_registry=ToolRegistry([tool.name]))
    ctx = ExecutionContext(process, process.session, str(workspace))
    args = {'path': str(target)} if external else {}
    create_permission_request(process, {'tool_name': tool.name, 'arguments': args,
        'resource': str(target) if external else 'capability://read_fixture',
        'purpose': 'Exercise the exact fixture operation'}, {tool.name: tool}, str(workspace))
    grant = grant_from_decision(process, process.pending_decision, 'allow_once')
    assert grant
    process.pending_decision = None
    return process, ctx, tool, args, grant, calls


class DenyGate:
    def check(self, process, name):
        return GateResult(False, error='fixture gate refused')


@pytest.mark.parametrize('sensitive,external', [(False, True), (True, False), (True, True)])
def test_pipeline_gate_denial_preserves_every_grant(tmp_path_factory, sensitive, external):
    process, ctx, tool, args, grant, calls = fixture(tmp_path_factory, sensitive=sensitive, external=external)
    result = ToolPipeline(DenyGate()).execute({'name': tool.name, 'args': args}, tool, ctx, 'denied')
    assert result.is_error
    assert not calls
    assert grant['remaining_uses'] == 1


def test_pipeline_invalid_prepared_arguments_preserve_resource_grant(tmp_path_factory):
    _, ctx, tool, args, grant, calls = fixture(tmp_path_factory, required=('purpose',))
    result = ToolPipeline().execute({'name': tool.name, 'args': args}, tool, ctx, 'invalid')
    assert result.diagnostics['code'] == 'INVALID_TOOL_ARGUMENTS'
    assert not calls
    assert grant['remaining_uses'] == 1


def test_once_covers_both_sensitive_call_and_its_resource(tmp_path_factory):
    _, ctx, tool, args, grant, calls = fixture(tmp_path_factory, sensitive=True)
    pipeline = ToolPipeline()
    result = pipeline.execute({'name': tool.name, 'args': args}, tool, ctx, 'first')
    assert not result.is_error, result.error
    assert len(calls) == 1
    assert calls[0]['_allowed_roots'] == [args['path']]
    assert grant['remaining_uses'] == 0
    assert pipeline.execute({'name': tool.name, 'args': args}, tool, ctx, 'second').is_error
    assert len(calls) == 1


def test_pipeline_late_cancellation_does_not_spend_grant(tmp_path_factory):
    _, ctx, tool, args, grant, calls = fixture(tmp_path_factory)
    ctx.event_bus.subscribe('ToolBeforeHooksStarted', lambda _: ctx.cancel())
    result = ToolPipeline().execute({'name': tool.name, 'args': args}, tool, ctx, 'cancelled')
    assert result.is_error and result.diagnostics['code'] == 'TOOL_CANCELLED'
    assert not calls
    assert grant['remaining_uses'] == 1


def test_pipeline_rechecks_resource_grant_after_hooks(tmp_path_factory):
    _, ctx, tool, args, grant, calls = fixture(tmp_path_factory)
    ctx.event_bus.subscribe('ToolBeforeHooksStarted', lambda _: grant.update(expires_at='2000-01-01T00:00:00+00:00'))
    result = ToolPipeline().execute({'name': tool.name, 'args': args}, tool, ctx, 'expired')
    assert result.is_error
    assert not calls
    assert grant['remaining_uses'] == 1


def test_missing_resource_never_partially_consumes_selection(tmp_path_factory):
    from backend.core.loop.permission_broker import admit_invocation_grants
    process, _, tool, args, grant, _ = fixture(tmp_path_factory, sensitive=True)
    roots, error = admit_invocation_grants(process, tool, args, args,
        [args['path'], str(tmp_path_factory / 'unapproved')], require_explicit=True)
    assert error and roots == []
    assert grant['remaining_uses'] == 1


def test_shared_grant_is_consumed_once_for_multiple_resources(tmp_path_factory):
    from backend.core.loop.permission_broker import admit_invocation_grants
    process, _, tool, args, grant, _ = fixture(tmp_path_factory, sensitive=True)
    grant['remaining_uses'] = 2
    roots, error = admit_invocation_grants(process, tool, args, args,
        [args['path'], str(tmp_path_factory / 'outside' / 'child')], require_explicit=True)
    assert error is None and roots == [args['path']]
    assert grant['remaining_uses'] == 1


def test_concurrent_admissions_cannot_share_one_use(tmp_path_factory):
    from backend.core.loop.permission_broker import admit_invocation_grants
    process, _, tool, args, grant, _ = fixture(tmp_path_factory, sensitive=True)
    barrier = Barrier(2)
    def admit():
        barrier.wait(timeout=5)
        return admit_invocation_grants(process, tool, args, args, [args['path']], require_explicit=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result(timeout=10) for future in [pool.submit(admit), pool.submit(admit)]]
    assert sum(error is None for _, error in (first, second)) == 1
    assert grant['remaining_uses'] == 0


@pytest.mark.parametrize('uses', ['1', 'invalid', True, -1, 0, {}, float('nan'), None])
def test_malformed_or_exhausted_use_count_is_not_authority(tmp_path_factory, uses):
    from backend.core.loop.permission_broker import admit_invocation_grants
    process, _, tool, args, grant, calls = fixture(tmp_path_factory, sensitive=True)
    grant['remaining_uses'] = uses
    roots, error = admit_invocation_grants(process, tool, args, args, [args['path']], require_explicit=True)
    assert error and roots == [] and not calls


def test_task_grant_missing_use_count_is_rejected(tmp_path_factory):
    from backend.core.loop.permission_broker import admit_invocation_grants
    process, _, tool, args, grant, _ = fixture(tmp_path_factory)
    grant['scope'] = 'task'
    del grant['remaining_uses']
    roots, error = admit_invocation_grants(process, tool, args, args, [args['path']], require_explicit=False)
    assert error and roots == []


def test_valid_task_resource_grant_remains_reusable(tmp_path_factory):
    process, ctx, tool, args, _, calls = fixture(tmp_path_factory)
    process.approval_grants.clear()
    create_permission_request(process, {'tool_name': tool.name, 'arguments': args,
        'resource': args['path'], 'purpose': 'Read this resource during this task'},
        {tool.name: tool}, ctx.workspace_path)
    grant = grant_from_decision(process, process.pending_decision, 'allow_task')
    assert grant and grant['remaining_uses'] is None
    for index in range(2):
        assert not ToolPipeline().execute({'name': tool.name, 'args': args}, tool, ctx, str(index)).is_error
    assert len(calls) == 2 and grant['remaining_uses'] is None


def test_distinct_grants_are_not_deduplicated_by_display_id(tmp_path_factory):
    from backend.core.loop.permission_broker import admit_invocation_grants
    process, _, tool, args, grant, _ = fixture(tmp_path_factory, sensitive=True)
    resource_grant = dict(grant)
    grant['resource'] = 'capability://read_fixture'
    process.approval_grants.append(resource_grant)
    roots, error = admit_invocation_grants(process, tool, args, args, [args['path']], require_explicit=True)
    assert error is None and roots == [args['path']]
    assert grant['remaining_uses'] == resource_grant['remaining_uses'] == 0


def test_pipeline_admission_resolution_error_is_visible_before_consumption(tmp_path_factory, monkeypatch):
    from backend.core.loop import permission_broker
    _, ctx, tool, args, grant, calls = fixture(tmp_path_factory)
    def unavailable(*args, **kwargs):
        raise OSError('fixture resource became unavailable')
    ctx.event_bus.subscribe('ToolBeforeHooksStarted',
        lambda _: monkeypatch.setattr(permission_broker, '_select_external_resources', unavailable))
    result = ToolPipeline().execute({'name': tool.name, 'args': args}, tool, ctx, 'unavailable')
    assert result.is_error and result.diagnostics['code'] == 'SANDBOX_POLICY_INVALID'
    assert result.receipt['effect_state'] == 'not_committed'
    assert not calls and grant['remaining_uses'] == 1
