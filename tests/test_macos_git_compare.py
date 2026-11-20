"""Compare protected consumers with real Git on a clean test repository."""
import subprocess
import threading
from pathlib import Path
import pytest
import sys

if sys.platform != 'darwin':
    pytest.skip('Darwin protected Git boundary', allow_module_level=True)

from backend.adapters.local_git_runner import ProtectedLocalGitRunner
from backend.core import macos_git_compare as compare,macos_broker_client as broker
from backend.core.loop.git_execution import GitBoundaryError
from backend.core.loop.worktree import WorktreeError
from tests.test_macos_worktree_result import ready,staged,case,create,repository,commit_fixture


@pytest.fixture
def commits(ready):
    manager,lease,calls,checks,raw=ready;p=Path(lease.path)
    (p/'nested/run.sh').unlink();(p/'added').write_bytes(b'hello\0world');(p/'added').chmod(0o755)
    manager.stage_index(lease,['nested/run.sh','added'])
    return manager,lease,manager.publish_index(lease),checks


def test_tree_and_diff_match_git_bytes_without_touching_worktree(commits):
    manager,lease,published,checks=commits;parent=lease.macos_binding['commit'];tip=published['commit']
    runner=ProtectedLocalGitRunner(manager.repo_root)
    queries=[['ls-tree','-rz',tip,'--'],['ls-tree','-rz',parent,'--','nested'],
        ['ls-tree','-rz',tip,'--','missing','added'],
        ['diff','--no-renames','--name-only','-z',parent,tip,'--'],
        ['diff','--no-renames','--name-only','-z',parent+'..'+tip,'--'],
        ['diff','--no-renames','--name-only','-z',tip,tip,'--']]
    before={p:p.read_bytes() for p in (manager.repo_root/'.git/HEAD',manager.repo_root/'.git/index')}
    for args in queries:
        expected=subprocess.run(['/usr/bin/git',*args],cwd=manager.repo_root,capture_output=True,check=True).stdout
        result=runner.run(args,text=False)
        assert result.stdout==expected and result.returncode==0 and result.stderr==b''
    assert runner.run(queries[3]).stdout=='a.bin\0added\0nested/run.sh\0'
    assert all(p.read_bytes()==b for p,b in before.items())
    # The upper promotion consumer's exact interface is now available.
    from backend.core.loop.promotion import _tree
    tree=_tree(manager,tip,['added','nested/run.sh'])
    assert tree=={'added':('100755',compare.checkout.object_oid('blob',b'hello\0world'))}
    manager.dispose(lease)
    assert runner.run(queries[3],text=False).stdout==b'a.bin\0added\0nested/run.sh\0'


@pytest.mark.parametrize('args',[
    ['ls-tree','-rz','HEAD','--'],['ls-tree','-rz','a'*40,'--','../x'],
    ['ls-tree','-rz','a'*40,'--',':(glob)*'],['ls-tree','-rz','a'*40,'--','*.txt'],
    ['ls-tree','-rz','a'*40,'--','.git/config'],['ls-tree','-rz','a'*40,'--','x','x'],
    ['ls-tree','-rz','a'*40,'--','x','--output=/tmp/x'],
    ['diff','--cached','--name-only','-z'],['diff','--no-renames','--name-only','-z','a'*40+'...'+ 'b'*40,'--'],
    ['diff','--no-renames','--name-only','-z','a'*40,'b'*40,'--','file'],
    ['diff','--no-renames','--name-only','-z','HEAD','b'*40,'--'],
])
def test_unsupported_selectors_refuse_before_any_transport(ready,args):
    manager,lease,calls,checks,raw=ready;before=len(checks)
    with pytest.raises((GitBoundaryError,ValueError)):ProtectedLocalGitRunner(manager.repo_root).run(args)
    assert len(checks)==before


@pytest.mark.parametrize('failure',['lost','cancel','replaced','timeout'])
def test_two_image_failure_never_returns_partial_output(commits,monkeypatch,failure):
    manager,lease,published,checks=commits;original=broker.checkout_git_commit;seen=[];stop=threading.Event()
    def failing(*a,**kw):
        seen.append(a[2]);image=original(*a,**kw)
        if len(seen)==1:
            if failure=='lost':raise EOFError('unknown cleanup')
            if failure=='cancel':stop.set()
            if failure=='replaced':(manager.repo_root/'.git/worktrees'/lease.macos_binding['metadata']/'HEAD').write_text('a'*40+'\n')
            if failure=='timeout':monkeypatch.setattr(compare.time,'monotonic',lambda:10**20)
        return image
    monkeypatch.setattr(broker,'checkout_git_commit',failing)
    monkeypatch.setattr('backend.core.loop.native_invocation_scope.current_native_cancellation',lambda p:stop)
    with pytest.raises(GitBoundaryError) as exc:
        ProtectedLocalGitRunner(Path(lease.path)).run(['diff','--no-renames','--name-only','-z',lease.macos_binding['commit'],published['commit'],'--'])
    assert len(seen)==1 and exc.value.command_started is None


def test_precancelled_and_wrong_project_scope_do_not_read(ready,monkeypatch):
    manager,lease,calls,checks,raw=ready;stop=threading.Event();stop.set()
    monkeypatch.setattr('backend.core.loop.native_invocation_scope.current_native_cancellation',lambda p:stop)
    with pytest.raises(GitBoundaryError):ProtectedLocalGitRunner(manager.repo_root).run(['ls-tree','-rz',lease.macos_binding['commit'],'--'])
    assert not checks


def test_mode_only_unicode_directory_and_same_commit_budget(ready):
    manager,lease,calls,checks,raw=ready;p=Path(lease.path);parent=lease.macos_binding['commit']
    (p/'nested/run.sh').chmod(0o644);(p/'资料').mkdir();(p/'资料/space name').write_bytes('你好'.encode())
    manager.stage_index(lease,['nested/run.sh','资料/space name'])
    published=manager.publish_index(lease);tip=published['commit'];runner=ProtectedLocalGitRunner(manager.repo_root)
    result=runner.run(['diff','--no-renames','--name-only','-z',parent,tip,'--'],text=False)
    assert result.stdout==b'a.bin\0nested/run.sh\0'+'资料/space name\0'.encode()
    listed=runner.run(['ls-tree','-rz',tip,'--','资料'],text=False)
    assert listed.stdout==b'100644 blob '+compare.checkout.object_oid('blob','你好'.encode()).encode()+b'\t'+'资料/space name\0'.encode()
    before=len(checks)
    assert runner.run(['diff','--no-renames','--name-only','-z',tip,tip,'--']).stdout==''
    assert len(checks)==before+1
