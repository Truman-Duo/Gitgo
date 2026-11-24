"""Promotion comparison reads literal bytes/modes without repository Git filters."""
from pathlib import Path
import pytest
from backend.core.loop.promotion import _matches
from tests.test_macos_worktree_result import ready,staged,case,create,repository,commit_fixture


def test_raw_binary_mode_presence_and_unrelated_paths(ready):
    manager,lease,_,_,git=ready
    commit=lease.snapshot_commit;p=manager.repo_root/'a.bin'
    original=p.read_bytes()
    assert _matches(manager,commit,['a.bin','absent'])
    p.write_bytes(b'changed\0\xff');assert not _matches(manager,commit,['a.bin'])
    p.write_bytes(original);p.chmod(0o755);assert not _matches(manager,commit,['a.bin'])
    p.chmod(0o644);assert _matches(manager,commit,['a.bin'])
    p.unlink();assert not _matches(manager,commit,['a.bin'])
    (manager.repo_root/'absent').write_bytes(b'new');assert not _matches(manager,commit,['absent'])
    assert _matches(manager,commit,['different-absent'])


def test_symlink_and_directory_are_never_treated_as_absent(ready):
    manager,lease,*_=ready;root=manager.repo_root
    (root/'alias').symlink_to(root/'a.bin')
    (root/'broken').symlink_to(root/'nonexistent')
    (root/'directory').mkdir()
    for name in ('alias','broken','directory'):
        assert not _matches(manager,lease.snapshot_commit,[name])


def test_no_repository_subprocess_or_filter(ready,monkeypatch):
    manager,lease,*_=ready
    def forbidden(*a,**k):raise AssertionError('unrestricted Git invocation')
    monkeypatch.setattr(manager,'_git_text',forbidden);monkeypatch.setattr(manager,'_git_bytes',forbidden);monkeypatch.setattr(manager,'_run',forbidden)
    assert _matches(manager,lease.snapshot_commit,['a.bin'])


@pytest.mark.parametrize('paths',[['../outside'],['/etc/passwd'],['.git/config'],['a.bin','a.bin']])
def test_invalid_paths_fail_before_checkout(ready,paths):
    manager,lease,_,checks,_=ready
    assert not _matches(manager,lease.snapshot_commit,paths)
    assert checks==[]


def test_packaged_promotion_precondition_probe_uses_real_commit_bytes(ready):
    from scripts.check_macos_git_broker import promotion_read_cases,promotion_record_cases
    manager,lease,_,checks,git=ready
    blob=git('hash-object','-w','--stdin',data=b'gitgo protected object\0\xff\r\n')
    tree=git('mktree',data=f'100644 blob {blob}\tdata.bin\n'.encode())
    empty=git('mktree',data=b'')
    def commit(tree):return git('-c','user.name=Test','-c','user.email=test@local.invalid','commit-tree',tree,'-m','comparison fixture')
    cases=promotion_read_cases(manager,commit(empty),commit(tree))+promotion_record_cases(manager,commit(empty),commit(tree))
    assert len(cases)==21 and len(checks)==15 and all(c['matched'] for c in cases)
    assert not (manager.repo_root/'data.bin').exists()


def test_record_versions_use_one_bound_read_without_repository_git(ready,monkeypatch):
    import hashlib
    from backend.core.loop.promotion_candidate import matches_record
    manager,lease,_,checks,_=ready
    digest=hashlib.sha256((manager.repo_root/'a.bin').read_bytes()).hexdigest()
    record={'file_versions':[{'path':'a.bin','before_sha256':digest,'after_sha256':digest},
                             {'path':'absent','before_sha256':'','after_sha256':''}]}
    def forbidden(*a,**k):raise AssertionError('unrestricted Git/read path')
    monkeypatch.setattr(manager,'_git_bytes',forbidden)
    monkeypatch.setattr('backend.core.loop.promotion_candidate._bytes',forbidden)
    for side in ('before','after'):
        before=len(checks)
        assert matches_record(manager,lease.snapshot_commit,['a.bin','absent'],record,side)
        assert len(checks)==before+1
    record['file_versions'][0]['after_sha256']='0'*64
    assert not matches_record(manager,lease.snapshot_commit,['a.bin','absent'],record,'after')
    assert matches_record(manager,lease.snapshot_commit,['a.bin','absent'],record,'before')
    (manager.repo_root/'a.bin').chmod(0o755)
    assert not matches_record(manager,lease.snapshot_commit,['a.bin','absent'],record,'before')


@pytest.mark.parametrize('kind',['null','missing-row','duplicate','foreign','bad-hash','bad-side','extra-key','non-list'])
def test_corrupt_recovery_record_fails_without_checkout(ready,kind):
    from backend.core.loop.promotion_candidate import matches_record
    manager,lease,_,checks,_=ready
    row={'path':'a.bin','before_sha256':'0'*64,'after_sha256':'0'*64}
    record={'file_versions':[row]};side='after'
    if kind=='null':record['file_versions']=None
    elif kind=='missing-row':record['file_versions']=[]
    elif kind=='duplicate':record['file_versions']=[row,row]
    elif kind=='foreign':row['path']='elsewhere'
    elif kind=='bad-hash':row['before_sha256']='not a digest'
    elif kind=='bad-side':side='unknown'
    elif kind=='extra-key':row['unexpected']='value'
    elif kind=='non-list':record['file_versions']={'a.bin':row}
    count=len(checks)
    assert not matches_record(manager,lease.snapshot_commit,['a.bin'],record,side)
    assert len(checks)==count


def test_legacy_and_empty_recovery_records(ready):
    from backend.core.loop.promotion_candidate import matches_record
    manager,lease,*_=ready
    assert matches_record(manager,lease.snapshot_commit,['a.bin'],{},'after')
    assert matches_record(manager,lease.snapshot_commit,[],{'file_versions':[]},'after')
    assert not matches_record(manager,lease.snapshot_commit,[],{'file_versions':None},'after')


@pytest.mark.parametrize('effect',['before','after','foreign'])
def test_legacy_prepared_record_requires_migration_without_replay(ready,monkeypatch,effect):
    import hashlib,json
    from backend.core.loop.promotion import _promote
    from backend.core.loop.worktree import WorktreeError
    manager,lease,_,_,git=ready;root=manager.repo_root
    before=lease.snapshot_commit;old=(root/'a.bin').read_bytes();new=b'recovery target\0\xfe'
    blob=git('hash-object','-w','--stdin',data=new)
    tree=git('mktree',data=f'100644 blob {blob}\ta.bin\n'.encode())
    after=git('-c','user.name=Test','-c','user.email=test@local.invalid','commit-tree',tree,'-p',before,'-m','recovery')
    key=manager._ref_id('record-recovery');storage=manager.storage
    previous={'state':'committed','snapshot_commit':before,'result_commit':before,'applied_commits':[],'paths':[]}
    record={'state':'prepared','snapshot_commit':before,'before_commit':before,'result_commit':after,
            'paths':['a.bin'],'applied_commits':[after],'previous':previous,
            'file_versions':[{'path':'a.bin','before_sha256':hashlib.sha256(old).hexdigest(),
                              'after_sha256':hashlib.sha256(new).hexdigest()}]}
    ref=storage.put_blob(json.dumps(record).encode(),media_type='application/json')
    assert storage.commit_state_revision('agent_promotion',key,ref,expected_ref=None,revision_key='seed')
    (root/'a.bin').write_bytes({'before':old,'after':new,'foreign':b'foreign'}[effect])
    saved=(root/'a.bin').read_bytes()
    class StopAfterObservation(Exception):pass
    def stop(*a,**k):raise StopAfterObservation
    monkeypatch.setattr(manager,'_add_detached',stop)
    def forbidden(*a,**k):raise AssertionError('replayed a filesystem effect')
    monkeypatch.setattr(manager,'_git_input',forbidden)
    with pytest.raises(WorktreeError,match='legacy promotion requires explicit migration'):
        _promote(manager,snapshot_commit=before,ordered_commits=[after],promotion_id='record-recovery')
    current=json.loads(storage.read_blob(storage.get_state_ref('agent_promotion',key)))
    assert current==record
    assert (root/'a.bin').read_bytes()==saved


def test_incremental_promotion_new_guards_still_match_commit(ready):
    import hashlib
    from backend.core.loop.promotion_candidate import matches_record
    manager,lease,*_=ready
    digest=hashlib.sha256((manager.repo_root/'a.bin').read_bytes()).hexdigest()
    record={'paths':['a.bin'],'file_versions':[{'path':'a.bin','before_sha256':digest,'after_sha256':digest}]}
    assert matches_record(manager,lease.snapshot_commit,['a.bin','new.bin'],record,'after')
    (manager.repo_root/'new.bin').write_bytes(b'foreign')
    assert not matches_record(manager,lease.snapshot_commit,['a.bin','new.bin'],record,'after')
    record['paths'].append('new.bin')
    assert not matches_record(manager,lease.snapshot_commit,['a.bin','new.bin'],record,'after')
