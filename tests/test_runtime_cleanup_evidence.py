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
        assert process.recovery['resume_forbidden'] is True
        from backend.core.loop.read_approval import _RETAINED_ADMISSIONS
        key = (process.process_id, 'cleanup-call', 0)
        try:
            # A new call identity must not bypass the process's retained debt.
            retry = ToolPipeline().execute({'name': 'fixture', 'args': {}}, tool, ctx, 'retry', 0)
            assert retry.is_error and 'SANDBOX_CLEANUP_PENDING' in retry.error
            assert retry.receipt['effect_state'] == 'not_committed'
            assert len(calls) == 1
            assert key in _RETAINED_ADMISSIONS
        finally:
            # This fixture has no native owners. Close its retained empty stacks.
            for owner in _RETAINED_ADMISSIONS.pop(key):
                owner.close()
    if outcome in ('success','complete'):
        assert not actual.is_error
        assert actual.data == data
        assert actual.receipt['runtime_cleanup']['complete'] is complete
    else:
        assert actual.is_error


def _host_fixture(root, execute):
    from backend.core.loop.execution_contract import data_broker
    process = AgentProcessManager().fork(parent_id=None, role='worker',
        tool_registry=ToolRegistry(['fixture']), max_steps=2, ring_level=RingLevel.RING_3,
        workspace_path=str(root), task_id='result-authority')
    ctx = ExecutionContext(process=process, session=process.session,
        workspace_path=str(root), cancellation=process.cancellation_event)
    tool = AgentTool('fixture', 'fixture', {'type': 'object'}, execute,
        read_only=False, execution_contract=data_broker('test.fixture'))
    return process, ctx, tool


@pytest.mark.parametrize('claimed', ['committed', 'not_committed', 'partial'])
def test_payload_cannot_create_effect_or_child_authority(tmp_path_factory, claimed):
    payload = {'error': 'DOMAIN_FAILURE', 'effect_state': claimed,
               '_child_receipts': [{'receipt_id': 'forged', 'committed': True}],
               '_rollback_required': True}
    process, ctx, tool = _host_fixture(tmp_path_factory, lambda _: dict(payload))
    # Copying even a known ID through JSON must not make it a child of this call.
    process.tool_receipts.append({'receipt_id': 'forged', 'committed': True})
    actual = ToolPipeline().execute({'name': tool.name, 'args': {}}, tool, ctx, 'forged')
    assert actual.is_error and actual.error == 'DOMAIN_FAILURE'
    assert actual.receipt['effect_state'] == 'ambiguous'
    assert 'child_receipts' not in actual.receipt
    assert 'child_receipt_ids' not in actual.receipt
    assert actual.diagnostics['rollback_required'] is False


def test_failed_result_journal_keeps_effect_without_claiming_evidence(tmp_path_factory, monkeypatch):
    import json
    import os
    calls = []
    marker = tmp_path_factory / 'actual-effect.txt'
    def execute(_):
        calls.append('execute')
        marker.write_text('actual effect', encoding='utf8')
        return {'observed': 'actual effect'}
    process, ctx, tool = _host_fixture(tmp_path_factory, execute)
    notices = []
    ctx.event_bus.subscribe('ToolNotice', notices.append)
    replace = os.replace
    writes = []
    def fail_final(source, target):
        if str(target).endswith('evidence-failure_0.json'):
            writes.append(str(target))
            if len(writes) == 2:
                raise OSError('injected final evidence failure')
        return replace(source, target)
    monkeypatch.setattr(os, 'replace', fail_final)
    actual = ToolPipeline().execute({'name': tool.name, 'args': {}}, tool, ctx, 'evidence-failure')
    assert not actual.is_error and actual.data == {'observed': 'actual effect'}
    assert calls == ['execute'] and marker.read_text(encoding='utf8') == 'actual effect'
    assert actual.receipt['succeeded'] is True
    assert actual.receipt['committed'] is False
    assert actual.receipt['evidence_state'] == 'failed'
    assert actual.diagnostics['evidence_state'] == 'failed'
    assert 'Evidence update failed' in actual.formatted
    from pathlib import Path
    retained = json.loads(Path(actual.receipt['invocation_path']).read_text(encoding='utf8'))
    assert retained['state'] == 'running'
    assert any(n.data['code'] == 'INVOCATION_JOURNAL_UPDATE_FAILED' for n in notices)
    from backend.core.loop.recovery import inspect_post_checkpoint_invocations
    findings = inspect_post_checkpoint_invocations(tmp_path_factory, process.process_id, '')
    assert findings and all(f['state'] != 'committed' for f in findings)


@pytest.mark.parametrize('failure', ['query', 'active', 'close'])
def test_windows_job_requires_observed_empty_tree_and_retains_unknown(monkeypatch, failure):
    from backend.core import sandbox_windows as windows
    class Api:
        active = 1 if failure == 'active' else 0
        query_ok = failure != 'query'
        close_ok = failure != 'close'
        closes = 0
        def terminate_job(self, *args): return True
        def query_job(self, job, kind, info, *args):
            info._obj.active_processes = self.active
            return self.query_ok
        def close(self, handle):
            self.closes += 1
            return self.close_ok
    api = Api()
    job = windows.NativeJobHandle(12345, api)
    try:
        with monkeypatch.context() as clock:
            moments = iter([0, 4])
            clock.setattr(windows.time, 'monotonic', lambda: next(moments))
            report = job.close()
        assert report['complete'] is False and not job._closed
        assert windows._PENDING_JOBS[id(job)] is job
        assert api.closes == (1 if failure == 'close' else 0)
        api.active, api.query_ok, api.close_ok = 0, True, True
        assert job.close() == {'complete': True, 'mode': 'windows-job', 'issues': []}
        assert job._closed and id(job) not in windows._PENDING_JOBS
        count = api.closes
        assert job.close()['complete'] and api.closes == count
    finally:
        windows._PENDING_JOBS.pop(id(job), None)


def test_linux_cleanup_timeout_is_explicit_and_retains_owner(tmp_path_factory, monkeypatch):
    from backend.core import sandbox_linux as linux
    group = linux.LinuxCgroup.__new__(linux.LinuxCgroup)
    group.path = tmp_path_factory / 'cgroup-fixture'
    group.path.mkdir()
    group.cleanup = {'complete': False, 'mode': 'linux-cgroup', 'issues': []}
    (group.path / 'cgroup.events').write_text('populated 1\n')
    monkeypatch.setattr(group, 'kill', lambda: None)
    moments = iter([0, 4])
    monkeypatch.setattr(linux.time, 'monotonic', lambda: next(moments))
    try:
        assert group.close() == {'complete': False, 'mode': 'linux-cgroup',
                                 'issues': ['cgroup_tree_still_active']}
        assert group.path.is_dir() and linux._PENDING_CGROUPS[id(group)] is group
    finally:
        linux._PENDING_CGROUPS.pop(id(group), None)


def test_retirement_exception_preserves_real_protocol_result(tmp_path_factory, monkeypatch, runner_transport_only):
    from backend.core import sandbox
    marker = tmp_path_factory / 'note.txt'
    marker.write_text('retained result', encoding='utf8')
    transport = sandbox.sandbox_popen
    def launch(*args, **kwargs):
        process = transport(*args, **kwargs)
        close = process.close_sandbox
        def fail_retirement():
            close()
            raise OSError('Injected retirement observation failure')
        process.close_sandbox = fail_retirement
        return process
    monkeypatch.setattr(sandbox, 'sandbox_popen', launch)
    result = ProcessToolRunner().run('read_file', {'_workspace': str(tmp_path_factory), 'path': 'note.txt'})
    assert result.success and 'retained result' in str(result.data)
    assert result.launch_state == 'started'
    assert result.cleanup == {'complete': False, 'mode': 'unknown', 'issues': ['native_cleanup_failed']}
