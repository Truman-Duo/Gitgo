"""Real linked metadata and FD publication, with broker admission substituted.

These ordinary-user tests do not claim privileged UID/launchd acceptance.
"""
import copy
import hashlib
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

if sys.platform!='darwin':pytest.skip('Darwin linked-worktree publication',allow_module_level=True)

from backend.core import macos_worktree as lifecycle,macos_broker_client as broker
from backend.core import macos_broker_protocol as wire,macos_git_checkout as checkout
from backend.core.loop.worktree import AgentWorktreeManager,WorktreeLease,WorktreeError
from backend.core.storage import get_storage
from tests.test_macos_git_read import repository
from tests.test_macos_git_checkout import commit_fixture

pytestmark=pytest.mark.skipif(sys.platform!='darwin',reason='Darwin linked-worktree publication')


@pytest.fixture
def case(repository,monkeypatch):
    repo,_,env,raw=repository
    commit,image,proof=commit_fixture(repository)
    raw('update-ref','refs/heads/main',commit);raw('symbolic-ref','HEAD','refs/heads/main')
    store=get_storage(repo);manager=AgentWorktreeManager(repo,store)
    fd=wire.open_directory(repo)
    binding=dict(project=str(repo),identity=wire.directory_identity(fd),grant_id='a'*32)
    os.close(fd);token=broker._SESSION.set(binding)
    calls=[]
    def prepare(project,root,oid,**kw):
        assert project==repo and oid==commit;calls.append(oid)
        return checkout.verify_result(commit,image,proof)
    monkeypatch.setattr(broker,'checkout_git_commit',prepare)
    try:yield manager,commit,calls,raw,env
    finally:broker._SESSION.reset(token)


def create(case,**kw):
    manager,commit,_,_,_=case
    return manager.create(process_id='worker',task_id='test',snapshot_commit=commit,**kw)


def test_worktree_admission_distinguishes_missing_grant_and_snapshot(case):
    manager, _, calls, _, _ = case
    token = broker._SESSION.set(None)
    try:
        with pytest.raises(WorktreeError, match='authorized project broker required'):
            manager.create(process_id='worker', task_id='test')
    finally:
        broker._SESSION.reset(token)
    with pytest.raises(WorktreeError, match='sealed project snapshot is required'):
        manager.create(process_id='worker', task_id='test')
    assert manager.storage.load_worktree_state('worker') is None
    assert calls == []


def test_installed_daemon_role_carries_selected_project_to_child(case, tmp_path_factory, monkeypatch):
    from backend.core import daemon, macos_daemon_project
    from backend.core.config import Config, ConfigManager, ProjectConfig
    from backend.core.loop.manager import AgentProcessManager
    from backend.core.loop.models import RingLevel
    from backend.core.loop.tools import ToolRegistry
    from backend.core.native_host_entry import _run_daemon_role

    manager, commit, calls, _, _ = case
    binding = broker._SESSION.get()
    project = ProjectConfig(name="protected", macos_worktree={
        "service": "resident", "snapshot_commit": commit,
    })
    project.workspace_path = str(manager.repo_root)
    config_path = tmp_path_factory / "config.json"
    ConfigManager.save(Config(projects=[project]), config_path)
    monkeypatch.setenv("GITGO_CONFIG_PATH", str(config_path))

    @contextmanager
    def resident(service, workspace):
        assert service == "resident"
        token = broker._SESSION.set(binding)
        try:
            assert broker.project_binding(workspace) is not None
            yield
        finally:
            broker._SESSION.reset(token)

    def run(cfg, opened, interval, debounce, snapshot_commit):
        assert opened.name == "protected"
        apm = AgentProcessManager(worktree_manager=manager, project_snapshot_commit=snapshot_commit)
        owner = apm.fork(None, "supervisor", ToolRegistry([]), 4, RingLevel.RING_0)
        child = apm.fork(owner.process_id, "worker", ToolRegistry([]), 4, RingLevel.RING_3)
        apm.start(child.process_id, lambda: apm.materialize_process_worktree(child))
        worker = apm._threads[child.process_id]
        worker.join(10)
        assert not worker.is_alive()
        assert child.worktree.get("snapshot_commit") == commit, child.result
        assert owner.worktree.get("snapshot_commit") == commit
        assert apm.resume_process_worktree(child) == child.worktree_path

    monkeypatch.setattr(macos_daemon_project, "resident_project_session", resident)
    monkeypatch.setattr(daemon, "_run_daemon", run)
    token = broker._SESSION.set(None)
    try:
        assert _run_daemon_role(["--project", "protected"]) == 0
        assert not broker.active()
    finally:
        broker._SESSION.reset(token)
    assert calls == [commit]


def test_configured_promotion_cannot_reach_the_workspace_without_user_approval(case, monkeypatch):
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.manager import AgentProcessManager
    from backend.core.loop.models import RingLevel
    from backend.core.loop.permission_broker import automatic_permission_request
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.tools import ToolRegistry

    worktrees, commit, _, _, _ = case
    manager = AgentProcessManager(worktree_manager=worktrees, project_snapshot_commit=commit)
    process = manager.fork(
        None, "supervisor", ToolRegistry(["promote_agent_changes"]), 6, RingLevel.RING_0,
        task_id="user-approval", actor_kind="supervisor", task_kind="supervisor",
        capability_profile_id="supervisor.control",
    )
    catalog = build_internal_tools(process, {}, str(worktrees.repo_root), None, None, None)
    tool = catalog["promote_agent_changes"]
    ctx = ExecutionContext(process, process.session, str(worktrees.repo_root), storage=worktrees.storage)
    ctx.artifacts["tool_catalog"] = catalog
    attempted = []
    monkeypatch.setattr(manager, "promote_process_results", lambda *a, **kw: attempted.append(a))
    args = {"process_ids": ["child"]}
    result = ToolPipeline().execute({"name": tool.name, "args": args}, tool, ctx, "unapproved")
    assert result.is_error and "SENSITIVE_TOOL_APPROVAL_REQUIRED" in result.formatted
    assert attempted == []
    decision = automatic_permission_request(process, tool, args, str(worktrees.repo_root))
    assert {option["action"] for option in decision["options"]} == {"allow_once", "deny"}


def test_real_linked_worktree_identity_persistence_and_disposal(case):
    manager,commit,calls,raw,env=case
    root=manager.repo_root
    before={p: p.read_bytes() for p in [root/'.git/HEAD',root/'.git/index',root/'.git/config']}
    lease=create(case);path=Path(lease.path);binding=lease.macos_binding
    assert lease.state=='leased' and binding['phase']=='published' and calls==[commit]
    assert (path/'a.bin').read_bytes()==b'\xff\0\r\n' and (path/'nested/run.sh').stat().st_mode&0o777==0o755
    loaded=WorktreeLease.from_dict(manager.storage.load_worktree_state('worker'))
    assert loaded.macos_binding==binding and manager.verify(loaded)['valid']
    def git(*args):return subprocess.run(['/usr/bin/git',*args],cwd=path,env=env,capture_output=True,check=True).stdout
    assert git('rev-parse','HEAD').strip().decode()==commit
    assert Path(git('rev-parse','--path-format=absolute','--git-common-dir').strip().decode())==root/'.git'
    assert git('write-tree').strip().decode()==raw('rev-parse',commit+'^{tree}')
    # write-tree may add a cache-tree extension: restore the expected Host index
    # only in this fixture, then demonstrate protected verification rejects drift.
    index=root/'.git/worktrees'/binding['metadata']/'index'
    original=checkout.canonical_index((('a.bin',b'\xff\0\r\n',0o644),('nested/run.sh',b'#!/bin/sh\n',0o755),('.gitattributes',b'*.bin text eol=lf filter=hostile\n',0o644)))
    if index.read_bytes()!=original:
        assert not manager.verify(loaded)['valid'];index.write_bytes(original)
    assert manager.verify(loaded)['valid']
    manager.dispose(loaded)
    assert not path.exists() and not (root/'.git/worktrees'/binding['metadata']).exists()
    assert loaded.state=='disposed' and loaded.macos_binding['phase']=='removed'
    assert all(p.read_bytes()==content for p,content in before.items())
    manager.recover_owned(loaded)
    assert calls==[commit]


@pytest.mark.parametrize('fault',['pointer','head','index','directory','metadata_directory'])
def test_binding_rejects_spoofed_or_replaced_worktrees(case,fault):
    manager,_,_,_,_=case;lease=create(case);path=Path(lease.path)
    meta=manager.repo_root/'.git/worktrees'/lease.macos_binding['metadata']
    if fault=='pointer':(path/'.git').write_text('gitdir: /private/etc\n')
    elif fault=='head':(meta/'HEAD').write_text('a'*40+'\n')
    elif fault=='index':(meta/'index').write_bytes(b'bad')
    else:
        selected=path if fault=='directory' else meta
        selected.rename(selected.with_name(selected.name+'-original'));selected.mkdir()
    assert not manager.verify(lease)['valid']
    if fault in ('directory','metadata_directory'):
        with pytest.raises(WorktreeError):manager.dispose(lease)
        assert selected.is_dir()


def test_lookalike_record_and_existing_target_never_grant_authority(case):
    manager,_,calls,_,_=case;lease=create(case)
    fake=copy.deepcopy(lease);fake.macos_binding['work_identity'][1]+=1
    assert not manager.verify(fake)['valid']
    with pytest.raises(WorktreeError):manager.dispose(fake)
    with pytest.raises(WorktreeError):create(case)
    assert calls==[lease.snapshot_commit] and Path(lease.path).exists()


@pytest.mark.parametrize('phase',['reserved','building','prepared','published'])
def test_interrupted_creation_retires_known_objects_without_replay(case,monkeypatch,phase):
    manager,commit,calls,_,_=case;original=manager._persist
    # Persist the transition, then model a hard Host exit by disabling all
    # subsequent journal writes, including the exception cleanup path.
    dead=False
    def crash(lease):
        nonlocal dead
        if dead:raise OSError('Host gone')
        original(lease)
        if lease.macos_binding.get('phase')==phase:
            dead=True;raise OSError('Host gone')
    monkeypatch.setattr(manager,'_persist',crash)
    with pytest.raises(OSError):create(case)
    monkeypatch.setattr(manager,'_persist',original)
    lease=WorktreeLease.from_dict(manager.storage.load_worktree_state('worker'))
    manager.recover_owned(lease)
    assert lease.state=='failed' and lease.macos_binding['phase']=='removed'
    assert not Path(lease.path).exists() and calls==[commit]
    assert not (manager.repo_root/'.git/worktrees'/lease.macos_binding['metadata']).exists()
    manager.recover_owned(lease)
    assert calls==[commit]


def test_missing_identity_after_mkdir_is_retained_not_adopted(case,monkeypatch):
    manager,_,calls,_,_=case;original=manager._persist
    def crash(lease):
        if lease.macos_binding.get('phase')=='building':raise OSError('lost identity write')
        if lease.state=='failed':raise OSError('Host gone')
        original(lease)
    monkeypatch.setattr(manager,'_persist',crash)
    with pytest.raises(OSError):create(case)
    monkeypatch.setattr(manager,'_persist',original)
    lease=WorktreeLease.from_dict(manager.storage.load_worktree_state('worker'))
    stage=manager.worktrees_root/lease.macos_binding['stage']
    assert stage.exists() and 'work_identity' not in lease.macos_binding
    with pytest.raises(WorktreeError,match='unrecorded'):manager.recover_owned(lease)
    assert stage.exists() and len(calls)==1


def test_retirement_recovers_after_quarantine_and_unlinks_links_without_following(case,monkeypatch):
    manager,_,calls,_,_=case;lease=create(case);path=Path(lease.path)
    outside=manager.worktrees_root/'outside';outside.mkdir();(outside/'keep').write_bytes(b'keep')
    (path/'escape').symlink_to(outside,target_is_directory=True)
    original=lifecycle.erase
    monkeypatch.setattr(lifecycle,'erase',lambda *a,**kw:(_ for _ in ()).throw(OSError('crash after rename')))
    with pytest.raises(OSError):manager.dispose(lease)
    assert not path.exists() and (manager.worktrees_root/lease.macos_binding['trash']).exists()
    monkeypatch.setattr(lifecycle,'erase',original)
    lease=WorktreeLease.from_dict(manager.storage.load_worktree_state('worker'));manager.recover_owned(lease)
    assert lease.state=='disposed'
    before=manager.storage.load_worktree_state('worker')
    manager.recover_owned(lease)
    assert manager.storage.load_worktree_state('worker')==before
    assert lease.macos_binding['phase']=='removed' and (outside/'keep').read_bytes()==b'keep' and len(calls)==1


def test_recovery_path_uses_durable_binding_and_never_resumes_failed_creation(case,monkeypatch):
    from backend.core.loop.recovery import inspect_worktree_recovery
    manager,_,calls,_,_=case;lease=create(case)
    assert inspect_worktree_recovery(lease.to_dict(),storage=manager.storage)==[]
    lease.state='creating';manager._persist(lease)
    findings=inspect_worktree_recovery(lease.to_dict(),storage=manager.storage)
    assert findings[0]['code']=='WORKTREE_NOT_RESUMABLE' and findings[0]['cleanup']=='removed'
    assert not Path(lease.path).exists() and len(calls)==1


def test_project_grant_required_even_with_valid_record(case):
    manager,_,_,_,_=case;lease=create(case);token=broker._SESSION.set(None)
    try:
        assert not manager.verify(lease)['valid']
        with pytest.raises(WorktreeError):manager.dispose(lease)
    finally:broker._SESSION.reset(token)
    assert Path(lease.path).exists()


def test_runtime_metadata_native_reads_and_private_write(case):
    manager,_,_,_,_=case;source=manager.repo_root/'.gitgo';source.mkdir()
    (source/'test_manifest.json').write_bytes(b'{"fixture":true}')
    lease=create(case)
    assert (Path(lease.path)/'.gitgo/test_manifest.json').read_bytes()==b'{"fixture":true}'
    assert lease.runtime_offsets['test_manifest.json']==16


def test_packed_head_and_symlink_ref_boundary(case):
    manager,commit,_,raw,_=case
    raw('pack-refs','--all','--prune')
    assert lifecycle.root_head(manager)==commit
    ref=manager.repo_root/'.git/refs/heads/main';ref.symlink_to(manager.repo_root/'.git/packed-refs')
    with pytest.raises((OSError,ValueError,WorktreeError)):lifecycle.root_head(manager)


def test_protected_queries_use_registered_lease_and_original_task_journal(case,monkeypatch):
    from types import SimpleNamespace
    import threading
    from backend.adapters.local_git_runner import ProtectedLocalGitRunner
    from backend.core.loop.native_invocation_scope import native_invocation_scope,prepare_native_record
    from backend.core.loop.execution_contract import NativeInvocation
    from backend.core.macos_git_read import QueryResult
    from backend.core.loop.git_execution import GitBoundaryError
    manager,commit,_,_,_=case;lease=create(case);stop=threading.Event();seen=[]
    ctx=SimpleNamespace(workspace_path=Path(lease.path),storage=manager.storage,cancellation=stop,
        process=SimpleNamespace(process_id='worker'),session=SimpleNamespace(session_id='worktree-session'))
    def query(project,root,args,**kw):
        assert project==manager.repo_root and kw['cancellation'] is stop
        inv=NativeInvocation.compile('macos_git_read',dict(_workspace=str(project),query=args,view_sha256='a'*64),10)
        record=prepare_native_record(inv,expected_workspace=project)
        seen.append(record)
        record.finish(dict(launch_state='not_started'))
        return QueryResult('commit\n',0)
    monkeypatch.setattr(broker,'query_git_objects',query)
    runner=ProtectedLocalGitRunner(Path(lease.path))
    with native_invocation_scope(ctx,'execution-worktree',3):
        assert runner.run(['rev-parse','HEAD']).stdout==commit+'\n'
        assert runner.run(['cat-file','-t',commit]).stdout=='commit\n'
        assert seen[0].storage is manager.storage
        assert seen[0].record['execution_id']=='execution-worktree' and seen[0].record['call_index']==3
        ctx.process.process_id='other'
        with pytest.raises(GitBoundaryError):runner.run(['cat-file','-t',commit])
        ctx.process.process_id='worker';stop.set()
        with pytest.raises(GitBoundaryError):runner.run(['rev-parse','HEAD'])
    assert len(seen)==1
    fake=manager.worktrees_root/'fake';fake.mkdir();(fake/'.git').write_bytes((Path(lease.path)/'.git').read_bytes())
    with pytest.raises(GitBoundaryError):ProtectedLocalGitRunner(fake).run(['rev-parse','HEAD'])
    (Path(lease.path)/'.git').write_text('gitdir: /private/etc\n')
    with pytest.raises(GitBoundaryError):runner.run(['rev-parse','HEAD'])


def test_reservation_rejects_concurrent_host_before_overwriting_row(case):
    manager,commit,calls,_,_=case
    with lifecycle.parent_lock(manager):
        with pytest.raises(WorktreeError,match='another Host'):create(case)
    assert manager.storage.load_worktree_state('worker') is None and not calls


def test_dotted_branch_head_is_supported(case):
    manager,commit,_,raw,_=case
    raw('update-ref','refs/heads/release/alpha.3.1',commit)
    raw('symbolic-ref','HEAD','refs/heads/release/alpha.3.1')
    assert lifecycle.root_head(manager)==commit
