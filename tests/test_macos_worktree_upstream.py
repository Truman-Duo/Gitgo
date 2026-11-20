import subprocess
from pathlib import Path
import pytest
import sys

if sys.platform != 'darwin':
    pytest.skip('Darwin protected Git boundary', allow_module_level=True)

from backend.core import macos_broker_client as broker,macos_git_read as read
from backend.core.sandbox_read import ReadRoot
from backend.core.loop.worktree import WorktreeError
from tests.test_macos_worktree_result import ready,staged,case,create,repository,commit_fixture


def wire_queries(manager,monkeypatch,tmp):
    tmp.mkdir();calls=[]
    def query(project,root,args,**kw):
        directory=tmp/str(len(calls));calls.append(args)
        with ReadRoot(project/'.git/objects') as opened:view=read.capture_objects(opened,project/'.git/objects')
        view.materialize(directory)
        result=subprocess.run(read.read_command(directory,args),capture_output=True,text=True)
        return read.QueryResult(result.stdout,result.returncode)
    monkeypatch.setattr(broker,'query_git_objects',query)
    return calls


def commit(git,tree,*parents):
    return git('-c','user.name=Test','-c','user.email=test@local.invalid','commit-tree',tree,*sum((['-p',p] for p in parents),[]),'-m','upstream')


def test_linear_predecessors_checkout_tip_preserve_snapshot_and_root(ready,monkeypatch,tmp_path_factory):
    manager,lease,*rest=ready;git=rest[-1];base=lease.snapshot_commit;tree=git('rev-parse',base+'^{tree}')
    one=commit(git,tree,base);two=commit(git,tree,one);calls=wire_queries(manager,monkeypatch,tmp_path_factory/'queries')
    prior={p:p.read_bytes() for p in (manager.repo_root/'.git/HEAD',manager.repo_root/'.git/index')}
    next_lease=manager.create(process_id='upstream',task_id='t',snapshot_commit=base,upstream_commits=[one,two])
    assert next_lease.snapshot_commit==base and next_lease.upstream_commits==[one,two]
    assert next_lease.macos_binding['commit']==two and manager.verify(next_lease)['valid']
    assert len(calls)==4 and all(p.read_bytes()==v for p,v in prior.items())
    manager.recover_owned(next_lease);assert len(calls)==4
    manager.dispose(next_lease)


@pytest.mark.parametrize('kind',['fork','blob','order','duplicate','too_many','review'])
def test_bad_upstream_has_no_worktree_side_effect(ready,monkeypatch,tmp_path_factory,kind):
    manager,lease,*rest=ready;git=rest[-1];base=lease.snapshot_commit;tree=git('rev-parse',base+'^{tree}')
    one=commit(git,tree,base);two=commit(git,tree,one);up=[one];extra={}
    if kind=='fork':up=[commit(git,tree)]
    if kind=='blob':up=[git('hash-object','-w','--stdin',data=b'blob')]
    if kind=='order':up=[two,one]
    if kind=='duplicate':up=[one,one]
    if kind=='too_many':up=['0'*40]*9
    if kind=='review':extra={'mode':'review'}
    wire_queries(manager,monkeypatch,tmp_path_factory/'queries')
    with pytest.raises(WorktreeError):manager.create(process_id='bad',task_id='t',snapshot_commit=base,upstream_commits=up,**extra)
    assert manager.storage.load_worktree_state('bad') is None


def test_packaged_rejection_cases_use_real_git_and_leave_no_authority(ready,monkeypatch,tmp_path_factory):
    from scripts.check_macos_git_broker import upstream_rejections
    manager,lease,*rest=ready;git=rest[-1];base=lease.snapshot_commit;tree=git('rev-parse',base+'^{tree}')
    tip=commit(git,tree,base);fork=commit(git,tree)
    blob=git('hash-object','-w','--stdin',data=b'not a commit')
    calls=wire_queries(manager,monkeypatch,tmp_path_factory/'queries')
    cases=upstream_rejections(manager,base,tip,fork,blob)
    assert len(cases)==6 and all(x['rejected'] and x['no_worktree_side_effect'] for x in cases)
    assert len(calls)==5
