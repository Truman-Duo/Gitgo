"""Real private Git objects and Host index, with privilege admission substituted."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import pytest
if sys.platform!='darwin':pytest.skip('macOS protected index',allow_module_level=True)
from backend.core import macos_worktree_index as index,macos_worktree as life,macos_broker_client as broker
from backend.core.loop.worktree import WorktreeLease,WorktreeError
from backend.core.macos_git_read import QueryResult
from backend.core.macos_git_checkout import canonical_index,object_oid
from tests.test_macos_worktree import case,create,repository,commit_fixture
from tests.test_macos_git_write import run


@pytest.fixture
def staged(case,tmp_path_factory,monkeypatch):
    manager,_,_,_,_=case;calls=[]
    def produce(project,root,query,data,author,**kw):
        assert project==manager.repo_root
        work=tmp_path_factory/('producer-'+str(len(calls)));work.mkdir();calls.append(query)
        return QueryResult(run(project,work,query,data,author)+'\n',0)
    monkeypatch.setattr(broker,'produce_git_objects',produce)
    return case,calls


def test_index_codec_matches_git_checkout_and_rejects_extensions():
    files=[('a',b'abc',0o644),('nested/z',b'\0\xff',0o755)]
    entries=[(n,object_oid('blob',data),mode) for n,data,mode in files]
    assert index.encode(entries)==canonical_index(files)
    assert index.decode(index.encode(entries))==entries
    raw=index.encode(entries)[:-20]+b'TREE\0\0\0\0'
    with pytest.raises(WorktreeError):index.decode(raw+hashlib.sha1(raw).digest())


def test_selected_add_modify_delete_preserve_unselected_and_root(staged,tmp_path_factory):
    case,calls=staged;manager,commit,_,raw,env=case;lease=create(case);path=Path(lease.path)
    root=manager.repo_root
    before={p:p.read_bytes() for p in (root/'.git/HEAD',root/'.git/index',root/'.git/config')}
    (path/'a.bin').write_bytes(b'\xffnew\0\r\n');(path/'nested/run.sh').unlink()
    (path/'new').mkdir();(path/'new/run').write_bytes(b'#!/bin/sh\necho ok\n');(path/'new/run').chmod(0o755)
    result=manager.stage_index(lease,['a.bin','nested/run.sh','new/run'])
    assert len(calls)==4 and manager.verify(lease)['valid']
    meta=root/'.git/worktrees'/lease.macos_binding['metadata'];content=(meta/'index').read_bytes()
    entries=index.decode(content)
    assert [n for n,_,_ in entries]==['.gitattributes','a.bin','new/run']
    assert dict((n,m) for n,_,m in entries)['new/run']==0o755
    assert result['tree']==index.tree_plan(entries)[0] and raw('cat-file','-t',result['tree'])=='tree'
    test_index=tmp_path_factory/'check.index';test_index.write_bytes(content)
    actual=subprocess.run(['/usr/bin/git','write-tree'],cwd=path,env={**env,'GIT_INDEX_FILE':str(test_index)},capture_output=True,check=True).stdout.decode().strip()
    assert actual==result['tree'] and (meta/'HEAD').read_text()==commit+'\n'
    assert all(p.read_bytes()==v for p,v in before.items())
    # An omitted path keeps its staged bytes even if working bytes later change.
    (path/'new/run').write_bytes(b'not staged');(path/'a.bin').write_bytes(b'second')
    result2=manager.stage_index(lease,['a.bin'])
    entries2=index.decode((meta/'index').read_bytes())
    assert next(x for x in entries2 if x[0]=='new/run')==next(x for x in entries if x[0]=='new/run')
    assert result2['tree']!=result['tree'] and manager.verify(lease)['valid']
    manager.dispose(lease)


@pytest.mark.parametrize('bad',[['../escape'],['.git'],['.gitgo/x'],['/absolute'],['a.bin','a.bin'],['x','X'],['dir/../x'],[]])
def test_invalid_selections_never_produce_objects(staged,bad):
    case,calls=staged;manager,*_=case;lease=create(case)
    with pytest.raises((WorktreeError,ValueError,RuntimeError)):manager.stage_index(lease,bad)
    assert not calls and not lease.macos_binding.get('index_update')


@pytest.mark.parametrize('kind',['symlink','hardlink','fifo','directory'])
def test_selected_special_files_refused(staged,kind):
    case,calls=staged;manager,*_=case;lease=create(case);path=Path(lease.path)/'new'
    if kind=='symlink':path.symlink_to('/private/etc/passwd')
    elif kind=='hardlink':os.link(Path(lease.path)/'a.bin',path)
    elif kind=='fifo':os.mkfifo(path)
    else:path.mkdir()
    with pytest.raises((WorktreeError,OSError)):manager.stage_index(lease,['new'])
    assert not calls and not lease.macos_binding.get('index_update')


def test_existing_index_lock_is_retained(staged):
    case,calls=staged;manager,*_=case;lease=create(case)
    lock=manager.repo_root/'.git/worktrees'/lease.macos_binding['metadata']/'index.lock';lock.write_bytes(b'other owner')
    with pytest.raises(WorktreeError,match='existing Git index lock'):manager.stage_index(lease,['a.bin'])
    assert lock.read_bytes()==b'other owner' and not calls and not lease.macos_binding.get('index_update')


@pytest.mark.parametrize('fault',['cancel','wrong_result','lost_result','rename'])
def test_failed_stage_never_resumes_or_replays(staged,monkeypatch,fault):
    case,calls=staged;manager,*_=case;lease=create(case);path=Path(lease.path);(path/'a.bin').write_bytes(b'changed')
    meta=manager.repo_root/'.git/worktrees'/lease.macos_binding['metadata'];old=(meta/'index').read_bytes()
    stop=threading.Event();original=broker.produce_git_objects
    def produce(*a,**k):
        result=original(*a,**k)
        if fault=='cancel':stop.set()
        if fault=='wrong_result':return QueryResult('0'*40+'\n',0)
        if fault=='lost_result':raise EOFError('lost cleanup')
        return result
    monkeypatch.setattr(broker,'produce_git_objects',produce)
    replace=os.replace
    def crash(src,dst,**kw):
        replace(src,dst,**kw)
        if src=='index.lock':raise OSError('Host exited after rename')
    if fault=='rename':monkeypatch.setattr(index.os,'replace',crash)
    with pytest.raises((WorktreeError,EOFError,OSError)):manager.stage_index(lease,['a.bin'],cancellation=stop)
    assert not manager.verify(lease)['valid'] and lease.macos_binding['index_update']
    assert ((meta/'index').read_bytes()==old)==(fault!='rename')
    count=len(calls);saved=WorktreeLease.from_dict(manager.storage.load_worktree_state(lease.process_id))
    manager.recover_owned(saved)
    assert saved.state=='failed' and not path.exists() and len(calls)==count
    manager.recover_owned(saved);assert len(calls)==count


def test_unknown_native_cleanup_blocks_index_recovery(staged,monkeypatch):
    case,calls=staged;manager,*_=case;lease=create(case)
    lease.macos_binding['index_update']=dict(phase='producing');manager._persist(lease)
    monkeypatch.setattr('backend.core.storage.native_invocations.pending_native_invocations',lambda *a,**k:[dict(code='NATIVE_CLEANUP_UNCONFIRMED')])
    with pytest.raises(WorktreeError,match='native execution/recovery debt'):manager.recover_owned(lease)
    assert Path(lease.path).is_dir() and not calls
