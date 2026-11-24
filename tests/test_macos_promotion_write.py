"""Native promotion effects with real Git images and the common file service."""
import copy
import os
from pathlib import Path
import pytest
from tests.test_macos_promotion_snapshot import ready,staged,case,create,repository,commit_fixture
from backend.core import macos_promotion_write as write
from backend.core.loop.worktree import WorktreeError
from scripts.check_macos_worktree_client import ephemeral_privacy_fixture


@pytest.fixture
def delivery(ready):
    from backend.core.knowledge.manager import LessonManager
    manager,lease,*_=ready
    LessonManager.bind_storage(manager.repo_root,manager.storage)
    tip=manager.publish_index(lease)['commit']
    records=[]
    def prepare(versions,*,native_write):records.append(dict(file_versions=copy.deepcopy(versions),native_write=copy.deepcopy(native_write)))
    args={'_mutation_lock_root':str(manager.storage.paths.state_home/'mutation-locks'),
          '_lesson_context':{'state_home':str(manager.storage.paths.state_home)}}
    return manager,lease,tip,args,prepare,records


def apply(delivery,**kw):
    manager,lease,tip,args,prepare,records=delivery
    from backend.core.loop.promotion_candidate import commit_promotion_patch
    return commit_promotion_patch(manager,Path(lease.path),['a.bin'],b'not interpreted by native writer',
        lease.snapshot_commit,tip,args,prepare,guard_paths=['a.bin'],**kw)


def test_binary_publication_preserves_user_git_metadata(delivery):
    manager,lease,tip,args,prepare,records=delivery
    root=manager.repo_root
    before={p:p.read_bytes() for p in (root/'.git/HEAD',root/'.git/index',root/'.git/config')}
    result=apply(delivery)
    assert result['effect_state']=='committed'
    assert (root/'a.bin').read_bytes()==(Path(lease.path)/'a.bin').read_bytes()
    assert (root/'a.bin').stat().st_mode&0o777==0o644
    assert len(records)==2 and records[0]['native_write']['temporaries'][0]['identity'] is None
    assert records[-1]['native_write']['temporaries'][0]['identity']
    write.retire(manager,records[-1]['native_write'])
    assert not list(root.rglob('.gitgo-promotion-*.tmp'))
    assert all(p.read_bytes()==v for p,v in before.items())


@pytest.mark.parametrize('fault',['intent','created','replace','after-replace'])
def test_interruption_retains_exact_effect_without_replay(delivery,monkeypatch,fault):
    manager,lease,tip,args,prepare,records=delivery;root=manager.repo_root;old=(root/'a.bin').read_bytes()
    class Death(BaseException):pass
    def save(*a,**k):
        prepare(*a,**k)
        if fault=='intent' or fault=='created' and len(records)==2:raise Death()
    delivery=manager,lease,tip,args,save,records
    replace=write.os.replace
    def stopping(*a,**k):
        native='src_dir_fd' in k
        if native and fault=='replace':raise Death()
        replace(*a,**k)
        if native and fault=='after-replace':raise Death()
    monkeypatch.setattr(write.os,'replace',stopping)
    with pytest.raises(Death):apply(delivery)
    value=(root/'a.bin').read_bytes()
    assert value==((Path(lease.path)/'a.bin').read_bytes() if fault=='after-replace' else old)
    write.retire(manager,records[-1]['native_write']);write.retire(manager,records[-1]['native_write'])
    assert not list(root.rglob('.gitgo-promotion-*.tmp')) and (root/'a.bin').read_bytes()==value


def test_unknown_temporary_is_not_deleted(delivery,monkeypatch):
    manager,lease,tip,args,prepare,records=delivery
    class Death(BaseException):pass
    def save(*a,**k):prepare(*a,**k);raise Death()
    with pytest.raises(Death):apply((manager,lease,tip,args,save,records))
    intent=records[-1]['native_write'];target=manager.repo_root/intent['temporaries'][0]['path'];target.write_bytes(b'foreign')
    with pytest.raises(WorktreeError,match='unconfirmed'):write.retire(manager,intent)
    assert target.read_bytes()==b'foreign'


@pytest.mark.parametrize('kind',['changed','mode','symlink','hardlink'])
def test_foreign_preimage_blocks_before_intent(delivery,kind):
    manager,lease,tip,args,prepare,records=delivery;p=manager.repo_root/'a.bin'
    if kind=='changed':p.write_bytes(b'foreign')
    elif kind=='mode':p.chmod(0o755)
    elif kind=='symlink':p.unlink();p.symlink_to('/etc/passwd')
    else:os.link(p,manager.repo_root/'hardlink')
    with pytest.raises(WorktreeError):apply(delivery)
    assert records==[] and not list(manager.repo_root.rglob('.gitgo-promotion-*.tmp'))


def test_cancellation_after_identity_only_retires_temporary(delivery,monkeypatch):
    manager,lease,tip,args,prepare,records=delivery;old=(manager.repo_root/'a.bin').read_bytes()
    monkeypatch.setattr(write,'cancellation_requested',lambda:len(records)>=2)
    with pytest.raises(WorktreeError):apply(delivery)
    assert (manager.repo_root/'a.bin').read_bytes()==old and not list(manager.repo_root.rglob('.gitgo-promotion-*.tmp'))


def make_tip(ready, changes):
    manager,lease,_,_,git=ready
    files=write.image(manager,lease.snapshot_commit);files.update(changes)
    tree={}
    for name,value in files.items():
        if value is None:continue
        raw,mode=value;oid=git('hash-object','-w','--stdin',data=raw);node=tree
        parts=name.split('/')
        for part in parts[:-1]:node=node.setdefault(part,{})
        node[parts[-1]]=(mode,oid)
    def produce(node):
        rows=[]
        for name,value in sorted(node.items()):
            if isinstance(value,dict):mode,kind,oid='040000','tree',produce(value)
            else:mode,kind,oid=('100755' if value[0]==0o755 else '100644'),'blob',value[1]
            rows.append(f'{mode} {kind} {oid}\t{name}\0'.encode())
        return git('mktree','-z',data=b''.join(rows))
    return git('-c','user.name=Test','-c','user.email=test@local.invalid','commit-tree',produce(tree),'-p',lease.snapshot_commit,'-m','publication')


def setup_write(ready,changes):
    from backend.core.knowledge.manager import LessonManager
    manager,lease,*_=ready;LessonManager.bind_storage(manager.repo_root,manager.storage)
    tip=make_tip(ready,changes);records=[]
    def prepare(versions,*,native_write):records.append(dict(file_versions=copy.deepcopy(versions),native_write=copy.deepcopy(native_write)))
    args={'_mutation_lock_root':str(manager.storage.paths.state_home/'mutation-locks'),
          '_lesson_context':{'state_home':str(manager.storage.paths.state_home)}}
    return manager,lease,tip,args,prepare,records


def test_create_delete_and_executable_mode_in_one_batch(ready):
    changes={'a.bin':None,'nested/run.sh':(b'#!/bin/sh\necho new\n',0o644),'new.bin':(b'\0\xff',0o755)}
    manager,lease,tip,args,prepare,records=setup_write(ready,changes)
    result=write.commit(manager,list(changes),lease.snapshot_commit,tip,args,prepare)
    assert result['effect_state']=='committed' and len(result['file_results'])==3
    assert not (manager.repo_root/'a.bin').exists()
    for name in ('new.bin','nested/run.sh'):
        assert (manager.repo_root/name).read_bytes()==changes[name][0]
        assert (manager.repo_root/name).stat().st_mode&0o777==changes[name][1]
    assert not list(manager.repo_root.rglob('.gitgo-promotion-*.tmp'))


def test_partial_batch_keeps_completed_effect_and_retains_untouched_file(ready,monkeypatch):
    changes={'a.bin':(b'first',0o644),'nested/run.sh':(b'second',0o755)}
    manager,lease,tip,args,prepare,records=setup_write(ready,changes);old=(manager.repo_root/'nested/run.sh').read_bytes()
    replace=write.os.replace;calls=[]
    def stop(*a,**k):
        if 'src_dir_fd' in k:
            calls.append(1)
            if len(calls)==2:raise OSError('injected second replacement failure')
        return replace(*a,**k)
    monkeypatch.setattr(write.os,'replace',stop)
    with pytest.raises(WorktreeError) as caught:write.commit(manager,list(changes),lease.snapshot_commit,tip,args,prepare)
    assert caught.value.diagnostic['effect_state']=='partial'
    assert (manager.repo_root/'a.bin').read_bytes()==b'first' and (manager.repo_root/'nested/run.sh').read_bytes()==old
    assert not list(manager.repo_root.rglob('.gitgo-promotion-*.tmp'))
    write.retire(manager,records[-1]['native_write'])
    assert (manager.repo_root/'a.bin').read_bytes()==b'first'


@pytest.mark.parametrize('kind',['missing-parent','metadata','omitted-change','secret'])
def test_unsupported_or_incomplete_candidate_has_no_target_effect(ready,kind):
    changes={'new/file':(b'new',0o644)} if kind=='missing-parent' else {'a.bin':(ephemeral_privacy_fixture() if kind=='secret' else b'new',0o644)}
    manager,lease,tip,args,prepare,records=setup_write(ready,changes)
    paths=['.git/config'] if kind=='metadata' else ['unrelated'] if kind=='omitted-change' else list(changes)
    old=(manager.repo_root/'a.bin').read_bytes()
    with pytest.raises((WorktreeError,OSError,ValueError)):write.commit(manager,paths,lease.snapshot_commit,tip,args,prepare)
    assert records==[] and (manager.repo_root/'a.bin').read_bytes()==old and not (manager.repo_root/'new').exists()


def test_packaged_probe_persists_intent_and_retires_without_replay(ready,monkeypatch):
    import sys
    from scripts.check_macos_git_broker import promotion_write_cases
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    manager,lease,_,checks,git=ready
    empty_tree=git('mktree',data=b'')
    empty=git('-c','user.name=Test','-c','user.email=test@local.invalid','commit-tree',empty_tree,'-m','empty')
    blob=git('hash-object','-w','--stdin',data=b'gitgo protected object\0\xff\r\n')
    tree=git('mktree',data=f'100644 blob {blob}\tdata.bin\n'.encode())
    commit=git('-c','user.name=Test','-c','user.email=test@local.invalid','commit-tree',tree,'-p',empty,'-m','data')
    before=len(checks);cases=promotion_write_cases(manager,empty,commit)
    assert len(cases)==4 and len(checks)==before+10 and all(c['matched'] for c in cases)
    assert not (manager.repo_root/'data.bin').exists()
