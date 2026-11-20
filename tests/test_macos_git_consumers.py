"""Consumer dispatch: no process fallback, authority widening or lost status."""
import sys
import threading
from types import SimpleNamespace
import pytest


if sys.platform != 'darwin':
    pytest.skip('Darwin protected Git boundary', allow_module_level=True)

from backend.adapters.local_git_runner import LocalGitRunner, ProtectedLocalGitRunner
from backend.core.loop.worktree import AgentWorktreeManager, WorktreeError
from backend.core.loop.git_execution import GitBoundaryError
from backend.core.loop.git_macos import run_object_query
from backend.core.loop.native_invocation_scope import native_invocation_scope
from backend.core import macos_broker_client as broker
from backend.core.macos_git_read import QueryResult
from tests.test_macos_git_read import repository

pytestmark=pytest.mark.skipif(sys.platform!='darwin',reason='macOS consumer bridge')


@pytest.fixture
def bridge(repository,monkeypatch):
    repo,oid,_,_=repository
    calls=[]
    monkeypatch.setattr(broker,'active',lambda:True)
    monkeypatch.setattr(broker,'project_binding',lambda path:None if path==repo else (_ for _ in ()).throw(ValueError('wrong project')))
    def query(project,root,args,**options):
        calls.append((project,args,options))
        return QueryResult('',1) if args[:2]==['merge-base','--is-ancestor'] else QueryResult('blob\n',0)
    monkeypatch.setattr(broker,'query_git_objects',query)
    monkeypatch.setattr('subprocess.run',lambda *a,**k:pytest.fail('Consumer attempted raw process fallback'))
    token=broker._SESSION.set(dict(project=str(repo)))
    try:yield repo,oid,calls
    finally:broker._SESSION.reset(token)


@pytest.mark.parametrize('runner',[LocalGitRunner,ProtectedLocalGitRunner])
def test_bound_consumers_preserve_binary_output_and_false_predicate(bridge,runner):
    repo,oid,calls=bridge
    result=runner(repo).run(['cat-file','-t',oid],text=False,timeout=120)
    assert result.returncode==0 and result.stdout==b'blob\n' and result.stderr==b''
    false=runner(repo).run(['merge-base','--is-ancestor',oid,oid])
    assert false.returncode==1 and false.stdout==''
    assert len(calls)==2 and calls[0][2]['timeout']==60


@pytest.mark.parametrize('args',[[],['status'],['cat-file','-t','HEAD'],['update-ref','refs/heads/x','a'*40],
    ['merge-base','--is-ancestor','a'*40,'b'*40,'--output=/tmp/x']])
def test_protected_consumer_refuses_unsupported_before_transport(bridge,args):
    repo,_,calls=bridge
    with pytest.raises(GitBoundaryError):ProtectedLocalGitRunner(repo).run(args)
    assert not calls


def test_legacy_consumer_cannot_fallback_to_writes_in_bound_context(bridge):
    repo,oid,calls=bridge
    assert LocalGitRunner(repo).run(['update-ref','refs/heads/x',oid]).returncode==126
    with pytest.raises(GitBoundaryError):LocalGitRunner(repo).run([])
    assert not calls


@pytest.mark.parametrize('options',[{'input_bytes':b''},{'transport_identity':{}},{'author_identity':{}},
    {'capture_output':False},{'timeout':True},{'timeout':float('nan')},{'timeout':0},{'encoding':'utf-16'}])
def test_consumer_options_cannot_expand_authority(bridge,options):
    repo,oid,calls=bridge
    with pytest.raises(GitBoundaryError):run_object_query(repo,['cat-file','-t',oid],**options)
    assert not calls


def test_unprovisioned_consumer_does_not_launch(bridge,monkeypatch):
    repo,oid,calls=bridge
    monkeypatch.setattr(broker,'active',lambda:False)
    with pytest.raises(GitBoundaryError,match='GIT_BOUNDARY_UNAVAILABLE'):
        ProtectedLocalGitRunner(repo).run(['cat-file','-t',oid])
    assert not calls


def test_task_cancellation_is_forwarded_and_cross_project_scope_refused(bridge):
    repo,oid,calls=bridge;stop=threading.Event()
    ctx=SimpleNamespace(workspace_path=repo,cancellation=stop)
    with native_invocation_scope(ctx,'execution',0):
        AgentWorktreeManager._run(repo,['git','cat-file','-t',oid])
        assert calls[-1][2]['cancellation'] is stop
        ctx.workspace_path=repo.parent
        with pytest.raises(WorktreeError):AgentWorktreeManager._run(repo,['git','cat-file','-t',oid])
    assert len(calls)==1


def test_transport_failure_does_not_claim_command_never_started(bridge,monkeypatch):
    repo,oid,_=bridge
    def lost(*a,**k):raise EOFError('unknown invocation')
    monkeypatch.setattr(broker,'query_git_objects',lost)
    with pytest.raises(WorktreeError) as failure:
        AgentWorktreeManager._run(repo,['git','cat-file','-t',oid])
    assert failure.value.diagnostic['error_info']['details']['command_started'] is None


def test_worktree_check_false_preserves_negative_predicate(bridge):
    repo,oid,_=bridge
    result=AgentWorktreeManager._run(repo,['git','merge-base','--is-ancestor',oid,oid],check=False)
    assert result.returncode==1 and result.stdout==b''
    with pytest.raises(WorktreeError):AgentWorktreeManager._run(repo,['git','merge-base','--is-ancestor',oid,oid])
