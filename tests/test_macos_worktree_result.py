"""Real Git commit/ref publication with substituted privilege admission."""
import base64
import json
import os
from pathlib import Path
import subprocess
import threading
import pytest
import sys

if sys.platform != 'darwin':
    pytest.skip('Darwin protected Git boundary', allow_module_level=True)

from backend.core import macos_worktree_result as result,macos_worktree as life,macos_broker_client as broker
from backend.core import macos_git_read as read,macos_git_checkout as checkout
from backend.core.sandbox_read import ReadRoot
from backend.core.sandbox_macos import task_environment
from backend.core.loop.worktree import WorktreeLease,WorktreeError
from tests.test_macos_worktree_index import staged
from tests.test_macos_worktree import case,create,repository,commit_fixture
from scripts.check_macos_worktree_client import ephemeral_privacy_fixture

@pytest.fixture
def ready(staged,tmp_path_factory,monkeypatch):
    case,calls=staged;manager,commit,_,raw,env=case;lease=create(case)
    (Path(lease.path)/'a.bin').write_bytes(b'changed\0\xff')
    manager.stage_index(lease,['a.bin']);checks=[]
    def prepared(project,root,oid,**kw):
        work=tmp_path_factory/('checkout-'+str(len(checks)));work.mkdir();checks.append(oid)
        with ReadRoot(project/'.git/objects') as source:read.capture_objects(source,project/'.git/objects').materialize(work/'object-view')
        out=work/'checkout';out.mkdir();cmd=checkout.command(work/'object-view',out,oid)
        proc=subprocess.run(cmd,env=task_environment(cmd,work,env),capture_output=True,text=True,check=True)
        image=dict(files=[[str(p.relative_to(out)),base64.b64encode(p.read_bytes()).decode(),p.stat().st_mode&0o777] for p in out.rglob('*') if p.is_file()],
                   directories=[str(p.relative_to(out)) for p in out.rglob('*') if p.is_dir()])
        return checkout.verify_result(oid,image,proc.stdout)
    monkeypatch.setattr(broker,'checkout_git_commit',prepared)
    return manager,lease,calls,checks,raw


def test_published_ref_matches_index_parent_and_preserves_user_branch(ready):
    manager,lease,calls,checks,raw=ready;root=manager.repo_root
    before={p:p.read_bytes() for p in (root/'.git/HEAD',root/'.git/index',root/'.git/config',root/'.git/refs/heads/main')}
    parent=lease.macos_binding['commit'];published=manager.publish_index(lease)
    assert published['commit']==raw('rev-parse',published['ref'])
    assert raw('rev-parse',published['commit']+'^')==parent
    assert raw('rev-parse',published['commit']+'^{tree}')==published['tree']
    assert manager.verify(lease)['valid'] and not (root/'.git/packed-refs.lock').exists()
    assert all(p.read_bytes()==v for p,v in before.items())
    assert lease.state=='leased' and not lease.result_commit # no implicit task sealing
    with pytest.raises(WorktreeError):manager.stage_index(lease,['a.bin'])
    with pytest.raises(WorktreeError):manager.publish_index(lease)
    manager.dispose(lease);assert raw('rev-parse',published['ref'])==published['commit']


@pytest.mark.parametrize('kind',['edit','mode','secret','binary_secret','header_secret'])
def test_privacy_and_staged_bytes_checked_before_commit(ready,kind):
    manager,lease,calls,checks,raw=ready;p=Path(lease.path)/'a.bin';count=len(calls)
    if kind=='edit':p.write_bytes(b'later edit')
    elif kind=='mode':p.chmod(0o755)
    else:
        # Simulate an already staged secret from an older version without
        # running the new pre-stage scanner.
        data=(b'\0\xff' if kind=='binary_secret' else b'++' if kind=='header_secret' else b'')+ephemeral_privacy_fixture()
        with pytest.raises(WorktreeError,match='privacy scan'):result.scan_files(manager,[('a.bin',data,0o644)])
        return
    with pytest.raises(WorktreeError):manager.publish_index(lease)
    assert len(calls)==count and not checks and not lease.macos_binding.get('result_update')


@pytest.mark.parametrize('phase',['producing','verified','packed-locked','prepared','renamed','committed'])
def test_publication_failure_reconciles_without_replay(ready,monkeypatch,phase):
    manager,lease,calls,checks,raw=ready;persist=manager._persist
    def save(value):
        persist(value)
        intent=value.macos_binding.get('result_update') or {}
        if intent.get('phase')==phase or phase=='committed' and value.macos_binding.get('result'):
            raise OSError('Host died')
    monkeypatch.setattr(manager,'_persist',save)
    rename=life.rename_exclusive
    def death(fd,old,new):
        rename(fd,old,new)
        if old.endswith('.lock'):raise OSError('after ref rename')
    if phase=='renamed':monkeypatch.setattr(life,'rename_exclusive',death)
    with pytest.raises(OSError):manager.publish_index(lease)
    monkeypatch.setattr(manager,'_persist',persist)
    saved=WorktreeLease.from_dict(manager.storage.load_worktree_state(lease.process_id));count=len(calls)
    manager.recover_owned(saved)
    if phase=='committed':
        assert manager.verify(saved)['valid'] and saved.macos_binding['result']
        manager.dispose(saved)
    else:
        assert saved.state=='failed' and not Path(saved.path).exists()
        assert bool(saved.macos_binding['result_interrupted']['ref'])==(phase=='renamed')
    manager.recover_owned(saved)
    assert len(calls)==count and not (manager.repo_root/'.git/packed-refs.lock').exists()


def test_unrecorded_lock_retained_and_recovery_refuses_adoption(ready,monkeypatch):
    manager,lease,calls,checks,raw=ready;write=life.write_file
    def death(fd,name,data,*a,**k):
        write(fd,name,data,*a,**k)
        if name=='packed-refs.lock':raise OSError('before identity saved')
    monkeypatch.setattr(life,'write_file',death)
    with pytest.raises(OSError):manager.publish_index(lease)
    with pytest.raises(WorktreeError,match='unrecorded'):manager.recover_owned(lease)
    assert Path(lease.path).exists() and (manager.repo_root/'.git/packed-refs.lock').exists()


def test_ref_tamper_blocks_verified_result(ready):
    manager,lease,*_=ready;value=manager.publish_index(lease)
    (manager.repo_root/'.git'/value['ref']).write_text('0'*40+'\n')
    assert not manager.verify(lease)['valid']


def test_cancel_after_production_never_publishes(ready,monkeypatch):
    manager,lease,calls,checks,raw=ready;stop=threading.Event();produce=broker.produce_git_objects
    def cancelling(*a,**kw):
        value=produce(*a,**kw);stop.set();return value
    monkeypatch.setattr(broker,'produce_git_objects',cancelling)
    with pytest.raises(WorktreeError):manager.publish_index(lease,cancellation=stop)
    manager.recover_owned(lease)
    assert lease.state=='failed' and not lease.macos_binding['result_interrupted']['ref']


def test_secret_stage_does_not_produce_objects(ready):
    manager,lease,calls,checks,raw=ready;count=len(calls)
    (Path(lease.path)/'a.bin').write_bytes(b'++'+ephemeral_privacy_fixture())
    with pytest.raises(WorktreeError,match='privacy scan'):manager.stage_index(lease,['a.bin'])
    assert len(calls)==count and not lease.macos_binding.get('index_update')


def test_existing_pack_lock_retained_before_commit(ready):
    manager,lease,calls,checks,raw=ready;count=len(calls)
    lock=manager.repo_root/'.git/packed-refs.lock';lock.write_bytes(b'other owner')
    with pytest.raises(WorktreeError,match='existing packed-refs lock'):manager.publish_index(lease)
    assert len(calls)==count and not lease.macos_binding.get('result_update') and lock.read_bytes()==b'other owner'


def test_unknown_native_debt_blocks_result_recovery(ready,monkeypatch):
    manager,lease,*_=ready
    lease.macos_binding['result_update']={'phase':'producing'};manager._persist(lease)
    monkeypatch.setattr('backend.core.storage.native_invocations.pending_native_invocations',lambda *a,**k:[{}])
    with pytest.raises(WorktreeError,match='native recovery debt'):manager.recover_owned(lease)
    assert Path(lease.path).is_dir()


def test_git_pack_refs_preserves_recorded_result(ready):
    manager,lease,calls,checks,raw=ready;value=manager.publish_index(lease)
    raw('pack-refs','--all','--prune')
    assert not (manager.repo_root/'.git'/value['ref']).exists()
    assert manager.verify(lease)['valid']
    manager.recover_owned(lease);assert manager.verify(lease)['valid']
    manager.dispose(lease);assert raw('rev-parse',value['ref'])==value['commit']


@pytest.mark.parametrize('bad',['missing','different','duplicate','symlink'])
def test_packed_result_cannot_change_authority(ready,bad):
    manager,lease,calls,checks,raw=ready;value=manager.publish_index(lease)
    raw('pack-refs','--all','--prune');packed=manager.repo_root/'.git/packed-refs'
    row=(value['commit']+' '+value['ref']+'\n')
    text=packed.read_text();assert row in text
    if bad=='missing':packed.write_text(text.replace(row,''))
    elif bad=='different':packed.write_text(text.replace(row,'0'*40+' '+value['ref']+'\n'))
    elif bad=='duplicate':packed.write_text(text+row)
    else:
        copy=packed.with_name('old-packed-refs');packed.rename(copy);packed.symlink_to(copy)
    assert not manager.verify(lease)['valid']


def test_artifact_reads_exact_commit_after_workspace_disposal(ready):
    manager,lease,calls,checks,raw=ready
    p=Path(lease.path)/'nested/run.sh';p.write_text('printable result\n')
    manager.stage_index(lease,['nested/run.sh']);value=manager.publish_index(lease)
    import hashlib
    digest=hashlib.sha256(p.read_bytes()).hexdigest()
    first=manager.read_artifact(value['commit'],'nested/run.sh',sha256=digest,offset=3)
    assert first['content']=='ntable result\n' and first['sha256']==digest and not first['working_tree_bytes_verified']
    manager.dispose(lease);raw('pack-refs','--all','--prune')
    second=manager.read_artifact(value['commit'],'nested/run.sh')
    assert second['content']=='printable result\n' and second['sealed_commit']==value['commit']
    with pytest.raises(WorktreeError,match='binary'):manager.read_artifact(value['commit'],'a.bin')
    with pytest.raises(WorktreeError,match='digest mismatch'):manager.read_artifact(value['commit'],'nested/run.sh',sha256='0'*64)
    with pytest.raises(WorktreeError):manager.read_artifact(value['commit'],'missing')


@pytest.mark.parametrize('path',['/nested/run.sh','../a.bin','.git/config','nested/../a.bin','nested\\run.sh'])
def test_artifact_path_rejected_before_admission(ready,path):
    manager,lease,calls,checks,raw=ready;value=manager.publish_index(lease);count=len(checks)
    with pytest.raises((WorktreeError,ValueError,RuntimeError)):manager.read_artifact(value['commit'],path)
    assert len(checks)==count


def test_artifact_does_not_return_bytes_after_unknown_cleanup(ready,monkeypatch):
    manager,lease,calls,checks,raw=ready;value=manager.publish_index(lease)
    def unknown(*a,**k):raise EOFError('cleanup unknown')
    monkeypatch.setattr(broker,'checkout_git_commit',unknown)
    with pytest.raises(EOFError):manager.read_artifact(value['commit'],'nested/run.sh')


@pytest.mark.parametrize('kind',['writable','hardlink'])
def test_unsafe_packed_ref_file_refused(ready,kind):
    manager,lease,calls,checks,raw=ready;value=manager.publish_index(lease)
    raw('pack-refs','--all','--prune');packed=manager.repo_root/'.git/packed-refs'
    if kind=='writable':packed.chmod(0o666)
    else:os.link(packed,packed.with_name('packed-copy'))
    assert not manager.verify(lease)['valid']


def test_ref_packed_after_unlock_before_final_record_retained_as_interrupted(ready,monkeypatch):
    manager,lease,calls,checks,raw=ready;persist=manager._persist
    def before_final(value):
        if value.macos_binding.get('result'):raise OSError('lost final acknowledgement')
        persist(value)
    monkeypatch.setattr(manager,'_persist',before_final)
    with pytest.raises(OSError):manager.publish_index(lease)
    monkeypatch.setattr(manager,'_persist',persist)
    saved=WorktreeLease.from_dict(manager.storage.load_worktree_state(lease.process_id))
    intent=saved.macos_binding['result_update'];ref='refs/gitgo/results/'+intent['id']
    raw('pack-refs','--all','--prune')
    count=len(calls);manager.recover_owned(saved)
    assert saved.state=='failed' and saved.macos_binding['result_interrupted']['ref']==ref
    assert raw('rev-parse',ref)==intent['commit'] and len(calls)==count
    manager.recover_owned(saved);assert len(calls)==count
