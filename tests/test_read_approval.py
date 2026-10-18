"""Actual OS pins and authority lifecycle; native launch acceptance is separate."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.core.loop.agent_tool import ApprovalMode
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.models import AgentProcess, RingLevel, ProcessStatus
from backend.core.loop.session import AgentSession
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.permission_broker import (create_permission_request, grant_from_decision,
    grant_is_current, admit_invocation_grants)
from backend.core.loop.read_approval import (AdmissionResources, revoke_read_requests,
    current_pin, ReadApprovalPin)
from backend.core.sandbox_read import ReadBoundaryError
from backend.core.sandbox_read_plan import ReadPlan, approved_read_scope
from backend.core.tools.catalog import build_workspace_tools
from backend.core.tools.workspace_tools import read_file
from backend.core.tools.registrations import file_read

pytestmark = pytest.mark.skipif(sys.platform not in {'linux', 'win32'}, reason='Native read backend required')


@pytest.fixture
def fixture(tmp_path_factory):
    root = tmp_path_factory / 'project'
    root.mkdir()
    (root / 'dir').mkdir()
    (root / 'dir' / 'note.txt').write_text('original', encoding='utf8')
    (root / 'other.txt').write_text('other', encoding='utf8')
    tool = replace(build_workspace_tools(root)['read_file'], approval=ApprovalMode.ASK)
    process = AgentProcess('reader', 'worker', RingLevel.RING_3,
        active_task_id='task', session=AgentSession(), worktree_path=str(root),
        tool_registry=ToolRegistry([tool.name]))
    args = {'path': 'dir/note.txt'}
    ctx = ExecutionContext(process, process.session, str(root))
    try:
        yield process, tool, root, args, ctx
    finally:
        revoke_read_requests(process)


def request(fixture):
    process, tool, root, args, _ = fixture
    return create_permission_request(process, {'tool_name': tool.name, 'arguments': args,
        'resource': 'capability://read_file', 'purpose': 'Read this note'}, {tool.name: tool}, str(root))


def approve(fixture, scope='allow_once'):
    process, _, _, _, _ = fixture
    request(fixture)
    grant = grant_from_decision(process, process.pending_decision, scope)
    assert grant
    process.pending_decision = None
    return grant


def admit(fixture, admission, args=None):
    process, tool, root, original, _ = fixture
    args = original if args is None else args
    return admit_invocation_grants(process, tool, args,
        {**args, '_workspace': str(root)}, [], require_explicit=True, admission=admission)


def test_capture_is_metadata_only_and_serializable(fixture, monkeypatch):
    def no_data(*a, **kw):
        pytest.fail('Host must not read file data during approval capture')
    monkeypatch.setattr(os, 'read', no_data)
    from backend.core.sandbox_read import ReadRoot
    monkeypatch.setattr(ReadRoot, 'open', no_data)
    card = request(fixture)
    assert json.loads(json.dumps(card))['permission_request']['read_binding']['version'] == 1
    assert len(fixture[0]._read_approval_pins) == 1


@pytest.mark.parametrize('handler', [read_file, file_read])
def test_live_binding_allows_content_edit_and_closes_once(fixture, handler):
    process, tool, root, args, _ = fixture
    grant = approve(fixture)
    (root / args['path']).write_text('updated same object', encoding='utf8')
    prepared = {**args, '_workspace': str(root)}
    with AdmissionResources() as admission:
        assert admit(fixture, admission) == ([], None)
        assert grant['remaining_uses'] == 0
        active = admission.read_binding
        with approved_read_scope(active.payload(prepared), prepared):
            assert handler(prepared)['content'] == 'updated same object'
    assert not process._read_approval_pins
    assert not grant_is_current(grant, process, tool)
    with pytest.raises(ReadBoundaryError):
        active.payload(prepared)
    (root / args['path']).rename(root / 'released.txt')


def test_task_grant_is_exact_and_cannot_switch_file_or_window(fixture):
    process, tool, _, args, _ = fixture
    grant = approve(fixture, 'allow_task')
    for changed in ({'path': 'other.txt'}, {**args, 'offset': 1}):
        with AdmissionResources() as admission:
            assert admit(fixture, admission, changed)[1]
            assert admission.read_binding is None
    for _ in range(2):
        with AdmissionResources() as admission:
            assert admit(fixture, admission) == ([], None)
            assert admission.read_binding is not None
    assert grant_is_current(grant, process, tool)
    assert grant['remaining_uses'] is None


def test_duplicate_approval_cannot_mint_second_grant(fixture):
    process = fixture[0]
    request(fixture)
    assert grant_from_decision(process, process.pending_decision, 'allow_once')
    assert grant_from_decision(process, process.pending_decision, 'allow_once') is None
    assert len(process.approval_grants) == 1


@pytest.mark.parametrize('action', ['deny', 'discuss'])
def test_denial_releases_waiting_pins(fixture, action):
    process, _, root, args, _ = fixture
    request(fixture)
    assert grant_from_decision(process, process.pending_decision, action) is None
    assert not process._read_approval_pins
    (root / args['path']).rename(root / 'released.txt')


@pytest.mark.parametrize('mode', ['cancel', 'terminal', 'expire', 'task_change', 'recover'])
def test_lifecycle_invalidates_and_releases_binding(fixture, mode):
    process, tool, root, args, ctx = fixture
    grant = approve(fixture, 'allow_task')
    pin = current_pin(process, grant)
    if mode == 'cancel':
        ctx.cancel()
    elif mode == 'terminal':
        from backend.core.loop.lifecycle import transition
        transition(process, ProcessStatus.COMPLETED)
    elif mode == 'expire':
        pin.deadline = 0
    elif mode == 'task_change':
        process.active_task_id = 'another-task'
    else:
        serialized = json.loads(json.dumps(process.approval_grants))
        revoke_read_requests(process)
        process.approval_grants = serialized
    assert not grant_is_current(grant, process, tool)
    (root / args['path']).rename(root / 'released.txt')


def test_recovered_pending_decision_does_not_restore_handles(fixture):
    process = fixture[0]
    request(fixture)
    saved = json.loads(json.dumps(process.pending_decision))
    revoke_read_requests(process)
    process.pending_decision = saved
    assert grant_from_decision(process, saved, 'allow_once') is None
    assert not process.approval_grants


def test_revocation_holds_pins_until_active_read_finishes(fixture):
    process, _, root, args, _ = fixture
    approve(fixture, 'allow_task')
    with AdmissionResources() as admission:
        assert admit(fixture, admission)[1] is None
        revoke_read_requests(process)
        with pytest.raises(ReadBoundaryError):
            admission.read_binding.remaining()
        if sys.platform == 'win32':
            with pytest.raises(OSError):
                root.rename(root.with_name('still-pinned'))
    (root / args['path']).rename(root / 'released.txt')


@pytest.mark.parametrize('part', ['file', 'parent', 'workspace'])
def test_replacement_during_wait_is_blocked_or_invalidates(fixture, part):
    process, tool, root, args, _ = fixture
    grant = approve(fixture)
    target = {'file': root / args['path'], 'parent': root / 'dir', 'workspace': root}[part]
    moved = target.with_name(target.name + '-original')
    if sys.platform == 'win32' and part != 'file':
        with pytest.raises(OSError):
            target.rename(moved)
        with AdmissionResources() as admission:
            assert admit(fixture, admission)[1] is None
    else:
        target.rename(moved)
        if part == 'file':
            target.write_text('replacement', encoding='utf8')
        else:
            target.mkdir()
            if part == 'workspace':
                (target / 'dir').mkdir()
            (root / args['path']).write_text('replacement', encoding='utf8')
        with AdmissionResources() as admission:
            assert admit(fixture, admission)[1]['name'] == 'APPROVAL_OBJECT_CHANGED'
        assert grant['remaining_uses'] == 1


def test_replace_after_admission_is_rejected_at_actual_read(fixture):
    _, _, root, args, _ = fixture
    approve(fixture)
    prepared = {**args, '_workspace': str(root)}
    with AdmissionResources() as admission:
        assert admit(fixture, admission)[1] is None
        payload = admission.read_binding.payload(prepared)
        # Metadata-only Windows handles may allow rename while preventing
        # unlink. Rebinding the original name must still fail identity checks.
        (root / args['path']).rename(root / 'original-moved')
        (root / args['path']).write_text('attacker', encoding='utf8')
        with approved_read_scope(payload, prepared):
            result = read_file(prepared)
        assert result['error'] == 'READ_ERROR' and 'content' not in result


def test_concurrent_once_admissions_have_one_winner(fixture):
    grant = approve(fixture)
    barrier = Barrier(2)
    def run():
        with AdmissionResources() as admission:
            barrier.wait(5)
            return admit(fixture, admission)[1] is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(run), pool.submit(run)
        assert [a.result(10), b.result(10)].count(True) == 1
    assert grant['remaining_uses'] == 0


@pytest.mark.parametrize('mutation', ['identity', 'expiry', 'resource', 'digest', 'missing_binding'])
def test_mutated_grant_cannot_supply_new_authority(fixture, mutation):
    process, tool, _, _, _ = fixture
    grant = approve(fixture)
    if mutation == 'identity':
        grant['read_binding']['file_identity'][2] += 1
    elif mutation == 'missing_binding':
        del grant['read_binding']
    else:
        key = {'expiry': 'expires_at', 'resource': 'resource', 'digest': 'arguments_digest'}[mutation]
        grant[key] += 'changed'
    assert not grant_is_current(grant, process, tool)


@pytest.mark.parametrize('change', [lambda d: d.update(version=True), lambda d: d.update(extra=1),
    lambda d: d.update(root='/'), lambda d: d.update(relative='../escape'),
    lambda d: d.update(file_identity=['win32', True, 1]), lambda d: d.update(file_identity=['linux', 1, 2**129])])
def test_child_plan_has_closed_schema(fixture, change):
    payload = deepcopy(request(fixture)['permission_request']['read_binding'])
    change(payload)
    with pytest.raises(ReadBoundaryError):
        ReadPlan.from_dict(payload)


def test_pipeline_transports_only_host_binding_and_releases_on_runner_failure(fixture, monkeypatch):
    process, tool, root, args, ctx = fixture
    grant = approve(fixture)
    captured = []
    def run(self, name, supplied, **kwargs):
        captured.append(kwargs['read_binding'].payload(supplied))
        assert '_approved_read' not in supplied
        raise RuntimeError('launch failed')
    monkeypatch.setattr('backend.core.loop.process_tool_runner.ProcessToolRunner.run', run)
    result = ToolPipeline().execute({'name': tool.name, 'args': {**args, '_approved_read': {'forged': True}}}, tool, ctx, 'launch')
    assert result.is_error and result.diagnostics["code"] == "SANDBOX_POLICY_INVALID"
    assert not captured and grant["remaining_uses"] == 1
    result = ToolPipeline().execute({"name": tool.name, "args": args}, tool, ctx, "launch")
    assert result.is_error and len(captured) == 1
    assert grant['remaining_uses'] == 0
    assert not process._read_approval_pins
    (root / args['path']).rename(root / 'released.txt')


def test_failed_publication_releases_every_pin(fixture, monkeypatch):
    process, _, root, args, _ = fixture
    def fail(*a):
        raise RuntimeError('publication failed')
    monkeypatch.setattr('backend.core.loop.decision_support.create_user_decision', fail)
    with pytest.raises(RuntimeError, match='publication failed'):
        request(fixture)
    assert not process._read_approval_pins
    (root / args['path']).rename(root / 'released.txt')



def test_host_and_child_must_declare_same_file_action(fixture):
    from backend.core.loop.execution_contract import NATIVE_PROCESS
    mismatched = replace(fixture[1], execution_contract=NATIVE_PROCESS)
    with pytest.raises(ValueError, match='file actions disagree'):
        mismatched.validate_execution_contract()


def test_once_record_cannot_be_reinterpreted_as_task_grant(fixture):
    grant = approve(fixture)
    grant.update(scope='task', remaining_uses=None)
    assert not grant_is_current(grant, fixture[0], fixture[1])


def test_pending_read_card_names_actual_file_and_exact_task_scope(fixture):
    card = request(fixture)
    resource = str(fixture[2] / fixture[3]['path'])
    assert card['permission_request']['resource'] == resource
    assert resource in card['permission_request']['api_details']
    assert card['permission_request']['approval_scope'] == 'exact_read_object_and_arguments'


def test_expiry_timer_releases_waiting_directory_without_another_call(fixture):
    from backend.core.loop.read_approval import ReadApprovalPin
    import time
    process, _, root, args, _ = fixture
    record = {'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=0.08)).isoformat()}
    pin = ReadApprovalPin(process, record, {**args, '_workspace': str(root)})
    try:
        pin._timer.join(timeout=3)
        assert not pin._timer.is_alive()
        assert pin._revoked
        root.rename(root.with_name('released-root'))
    finally:
        pin.close()




def test_revocation_before_result_delivery_stops_tree_and_hides_data(fixture, monkeypatch):
    from backend.core.loop.process_tool_runner import ProcessToolRunner
    import backend.core.process_control as control
    process, _, root, args, _ = fixture
    approve(fixture)
    stopped = []
    class Child:
        _gitgo_job_handle = None
        stdin = stdout = stderr = None
        returncode = 0
        def poll(self):
            return None
        def close_sandbox(self):
            assert stopped
    class Communication:
        def __init__(self, child):
            pass
        def communicate(self, **kw):
            revoke_read_requests(process)
            return json.dumps({'success': True, 'data': {'content': 'must not escape'}}), ''
        def stderr_snapshot(self):
            return ''
    monkeypatch.setattr('backend.core.sandbox.sandbox_popen', lambda *a, **k: Child())
    monkeypatch.setattr(control, 'attach_kill_job', lambda child: None)
    monkeypatch.setattr(control, 'close_job', lambda job: None)
    monkeypatch.setattr(ProcessToolRunner, '_kill_tree', lambda *a: stopped.append(True))
    monkeypatch.setattr('backend.core.sandbox_io.BoundedCommunication', Communication)
    with AdmissionResources() as admission:
        assert admit(fixture, admission)[1] is None
        result = ProcessToolRunner().run('read_file', {**args, '_workspace': str(root)},
                                       read_binding=admission.read_binding)
        assert stopped and not result.success and result.data is None
        assert 'must not escape' not in str(result)


def test_per_invocation_read_cannot_expand_to_task_scope(fixture):
    process, tool, root, args, ctx = fixture
    sensitive = replace(tool, approval_per_invocation=True)
    selected = (process, sensitive, root, args, ctx)
    request(selected)
    assert grant_from_decision(process, process.pending_decision, 'allow_task') is None
    assert not process.approval_grants
    assert grant_from_decision(process, process.pending_decision, 'allow_once')


def test_pending_scope_tamper_cannot_widen_permission(fixture):
    process = fixture[0]
    request(fixture)
    process.pending_decision['permission_request']['approval_scope'] = 'resource_or_task'
    assert grant_from_decision(process, process.pending_decision, 'allow_task') is None


def test_external_scope_binding_still_matches_exact_file(fixture):
    process, tool, root, _, ctx = fixture
    target = root.parent / 'external-note.txt'
    target.write_text('external', encoding='utf8')
    args = {'path': str(target)}
    selected = (process, tool, root, args, ctx)
    grant = approve(selected)
    with AdmissionResources() as admission:
        roots, error = admit_invocation_grants(process, tool, args,
            {**args, '_workspace': str(root)}, [str(target)],
            require_explicit=True, admission=admission)
        assert error is None and roots == [str(target)]
        assert grant['remaining_uses'] == 0
        assert admission.read_binding.payload({**args, '_workspace': str(root),
                                               '_allowed_roots': roots})['root'] == str(root.parent)



@pytest.mark.parametrize('resource', [None, 'dir', 'dir/note.txt'])
def test_relative_resource_is_bound_to_workspace_and_narrowed(fixture, resource):
    process, tool, root, args, _ = fixture
    card = create_permission_request(process, {'tool_name': tool.name,
        'arguments': args, 'purpose': 'Read note', **({'resource': resource} if resource else {})},
        {tool.name: tool}, str(root))
    assert card['permission_request']['resource'] == str(root / args['path'])
    assert card['permission_request']['read_binding']['root'] == str(root)


def test_unrelated_displayed_resource_cannot_approve_different_file(fixture):
    process, tool, root, args, _ = fixture
    with pytest.raises(ValueError, match='does not contain'):
        create_permission_request(process, {'tool_name': tool.name, 'arguments': args,
            'purpose': 'Read note', 'resource': str(root / 'other.txt')}, {tool.name: tool}, str(root))
    assert process.pending_decision is None and not process._read_approval_pins


def test_approval_path_uses_same_worktree_mapping_as_pipeline(fixture):
    process, tool, root, args, _ = fixture
    project = root.parent / 'logical-project'
    project.mkdir()
    process.workspace_root = str(project)
    raw = {'path': str(project / args['path'])}
    card = create_permission_request(process, {'tool_name': tool.name, 'arguments': raw,
        'purpose': 'Read mapped note', 'resource': raw['path']}, {tool.name: tool}, str(root))
    assert card['permission_request']['resource'] == str(root / args['path'])
    assert grant_from_decision(process, process.pending_decision, 'allow_once')
    with AdmissionResources() as admission:
        _, error = admit_invocation_grants(process, tool, raw,
            {'path': str(root / args['path']), '_workspace': str(root)}, [],
            require_explicit=True, admission=admission)
        assert error is None and admission.read_binding is not None


@pytest.mark.parametrize('report', ['complete', 'incomplete', 'missing'])
def test_launch_failure_keeps_approved_read_pins_until_cleanup_confirmed(fixture, monkeypatch, report):
    from backend.core.sandbox import SandboxDenied
    from backend.core.loop.read_approval import _RETAINED_ADMISSIONS
    process, tool, root, args, ctx = fixture
    approve(fixture)
    launches = []
    def launch(*a, **kw):
        launches.append('attempt')
        failure = SandboxDenied('SANDBOX_LAUNCH_DENIED', 'Injected post-creation failure')
        failure.effect_state = 'ambiguous'
        if report != 'missing':
            failure.cleanup = {'complete': report == 'complete', 'mode': 'fixture',
                               'issues': [] if report == 'complete' else ['job_exit_query_failed']}
        raise failure
    monkeypatch.setattr('backend.core.sandbox.sandbox_popen', launch)
    key = (process.process_id, 'launch-cleanup', 0)
    try:
        actual = ToolPipeline().execute({'name': tool.name, 'args': args}, tool, ctx, 'launch-cleanup')
        assert actual.is_error and launches == ['attempt']
        assert actual.receipt['launch_state'] == 'unknown'
        assert actual.receipt['runtime_cleanup']['complete'] is (report == 'complete')
        revoke_read_requests(process)
        if report == 'complete':
            assert key not in _RETAINED_ADMISSIONS
        else:
            assert key in _RETAINED_ADMISSIONS and process.recovery['resume_forbidden']
            if sys.platform == 'win32':
                with pytest.raises(OSError):
                    root.rename(root.with_name('must-remain-pinned'))
            denied = ToolPipeline().execute({'name': tool.name, 'args': args}, tool, ctx, 'second-launch')
            assert denied.is_error and launches == ['attempt']
    finally:
        # The injected failure never created a native process. Release only the
        # real metadata pins owned by this fixture after testing debt retention.
        for owner in _RETAINED_ADMISSIONS.pop(key, []):
            owner.close()
    (root / args['path']).rename(root / 'released-after-confirmation.txt')
