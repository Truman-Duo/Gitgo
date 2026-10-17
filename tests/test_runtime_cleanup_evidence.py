"""Supervisor evidence survives the real Pipeline without replaying effects."""
import pytest
from backend.core.loop.execution_contract import NATIVE_PROCESS
from backend.core.loop.agent_tool import AgentTool
from backend.core.loop.event_bus import EventBus
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import RingLevel
from backend.core.loop.process_tool_runner import ProcessToolRunner, SubprocessResult
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tools import ToolRegistry


@pytest.mark.parametrize('outcome', ['success', 'complete', 'timeout', 'cancelled', 'business'])
def test_pipeline_preserves_runtime_cleanup_and_never_reexecutes(tmp_path_factory, monkeypatch, outcome):
    complete = outcome == 'complete'
    cleanup = {'complete': complete, 'mode': 'owned-session-only',
               'issues': [] if complete else ['detached_descendants_not_covered']}
    data = {'observed': 'effect retained', 'cleanup': {'complete': True}}
    if outcome == 'business':
        data['error'] = 'FIXTURE_DOMAIN_FAILURE'
    result = SubprocessResult(success=outcome not in ('timeout','cancelled'), data=data,
        timed_out=outcome == 'timeout', error='cancelled' if outcome == 'cancelled' else 'timeout', cleanup=cleanup)
    calls = []
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return result
    monkeypatch.setattr(ProcessToolRunner, 'run', run)
    process = AgentProcessManager().fork(parent_id=None, role='worker',
        tool_registry=ToolRegistry(['fixture']), max_steps=2, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id='cleanup-evidence')
    bus = EventBus()
    notices = []
    bus.subscribe('ToolNotice', notices.append)
    ctx = ExecutionContext(process=process, session=process.session,
        workspace_path=str(tmp_path_factory), event_bus=bus, cancellation=process.cancellation_event)
    tool = AgentTool(name='fixture', description='fixture', parameters={'type':'object'},
        execute=lambda _: pytest.fail('Must execute only through the isolated runner'),
        read_only=True, isolated=True, execution_contract=NATIVE_PROCESS)
    actual = ToolPipeline().execute({'name':'fixture','args':{}}, tool, ctx, 'cleanup-call', 0)
    assert len(calls) == 1
    assert actual.receipt['runtime_cleanup'] == cleanup
    assert actual.diagnostics['runtime_cleanup'] == cleanup
    assert len(notices) == (0 if complete else 1)
    if not complete:
        assert notices[0].data['code'] == 'PROCESS_CLEANUP_INCOMPLETE'
    if outcome in ('success','complete'):
        assert not actual.is_error
        assert actual.data == data
        assert actual.receipt['runtime_cleanup']['complete'] is complete
    else:
        assert actual.is_error
