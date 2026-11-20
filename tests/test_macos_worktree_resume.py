"""Continuation retains business edits without reusing source execution authority."""
import copy
from pathlib import Path
import pytest
import sys

if sys.platform != 'darwin':
    pytest.skip('Darwin protected Git boundary', allow_module_level=True)

from backend.core.loop.worktree import WorktreeLease,WorktreeError
from backend.core import macos_worktree as life
from tests.test_macos_worktree_result import ready,staged,case,create,repository,commit_fixture
from scripts.check_macos_worktree_client import ephemeral_privacy_fixture


def resume(manager,previous,**kw):
    return manager.create(process_id='continued',task_id='t',snapshot_commit=previous.snapshot_commit,
        upstream_commits=previous.upstream_commits,resume_from=previous,**kw)


def test_resume_preserves_full_delta_and_fresh_authority(ready):
    manager,previous,_,_,git=ready;old=Path(previous.path)
    (old/'new.bin').write_bytes(b'new\0\xff');(old/'new.bin').chmod(0o755)
    root=manager.repo_root
    before={p:p.read_bytes() for p in (root/'.git/HEAD',root/'.git/index',root/'.git/config')}
    lease=resume(manager,previous);work=Path(lease.path)
    assert (work/'a.bin').read_bytes()==b'changed\0\xff'
    assert (work/'new.bin').read_bytes()==b'new\0\xff' and (work/'new.bin').stat().st_mode&0o777==0o755
    assert lease.resumed_from_process_id==previous.process_id
    assert lease.continuation_commit==previous.continuation_commit==previous.macos_binding['result']['commit']
    assert previous.state=='leased' and not previous.result_commit and not previous.has_promotable_result
    assert lease.macos_binding['commit']==previous.macos_binding['commit']==lease.snapshot_commit
    assert lease.macos_binding['checkout_commit']==lease.continuation_commit
    assert lease.macos_binding['work_identity']!=previous.macos_binding['work_identity']
    assert lease.macos_binding['metadata']!=previous.macos_binding['metadata']
    assert 'result' not in lease.macos_binding and 'runtime_merged' not in lease.macos_binding
    assert not lease.result_commit and manager.verify(lease)['valid']
    manager.seal(lease)
    assert lease.state=='sealed' and lease.own_commit and lease.changed_files==['a.bin','new.bin']
    assert git('rev-parse',lease.result_commit+'^')==previous.macos_binding['commit']
    assert all(p.read_bytes()==v for p,v in before.items())
    manager.dispose(lease);manager.dispose(previous)


def test_resume_preserves_deletion_and_empty_result(ready):
    manager,previous,*_=ready
    (Path(previous.path)/'a.bin').unlink()
    lease=resume(manager,previous)
    assert not (Path(lease.path)/'a.bin').exists()
    manager.seal(lease);assert lease.changed_files==['a.bin']
    manager.dispose(lease);manager.dispose(previous)


@pytest.mark.parametrize('field,value',[('snapshot_commit','0'*40),('upstream_commits',['0'*40]),
    ('state','sealed'),('promoted',True),('continuation_commit','0'*40)])
def test_forged_or_stale_source_rejected_before_publication(ready,field,value):
    manager,previous,calls,checks,_=ready;forged=copy.deepcopy(previous);setattr(forged,field,value)
    before=len(calls),len(checks)
    with pytest.raises(WorktreeError):resume(manager,forged)
    assert (len(calls),len(checks))==before and manager.storage.load_worktree_state('continued') is None


def test_resume_same_process_and_review_rejected(ready):
    manager,previous,calls,checks,_=ready;before=len(calls),len(checks)
    with pytest.raises(WorktreeError):resume(manager,previous,mode='review')
    with pytest.raises(WorktreeError):manager.create(process_id=previous.process_id,task_id='t',snapshot_commit=previous.snapshot_commit,resume_from=previous)
    assert (len(calls),len(checks))==before


def test_published_checkpoint_cannot_silently_drop_later_source_edit(ready):
    manager,previous,*_=ready;lease=resume(manager,previous);manager.dispose(lease)
    (Path(previous.path)/'a.bin').write_bytes(b'after checkpoint')
    with pytest.raises(WorktreeError,match='checkpoint differs'):resume(manager,previous)
    assert manager.storage.load_worktree_state('continued')['state']=='disposed'


def test_privacy_failure_creates_no_target_lease(ready):
    manager,previous,calls,checks,_=ready
    (Path(previous.path)/'secret').write_bytes(ephemeral_privacy_fixture())
    before=len(calls),len(checks)
    with pytest.raises(WorktreeError,match='privacy'):resume(manager,previous)
    assert (len(calls),len(checks))==before and manager.storage.load_worktree_state('continued') is None


@pytest.mark.parametrize('phase',['reserved','building','prepared','published'])
def test_resume_creation_failure_retains_checkpoint_and_recovery_does_not_replay(ready,monkeypatch,phase):
    manager,previous,calls,checks,_=ready;persist=manager._persist
    def fail(value):
        persist(value)
        if value.process_id=='continued' and value.macos_binding.get('phase')==phase:raise OSError('Host interrupted')
    monkeypatch.setattr(manager,'_persist',fail)
    with pytest.raises(OSError):resume(manager,previous)
    monkeypatch.setattr(manager,'_persist',persist)
    saved=WorktreeLease.from_dict(manager.storage.load_worktree_state('continued'))
    before=len(calls),len(checks);manager.recover_owned(saved);manager.recover_owned(saved)
    assert (len(calls),len(checks))==before and not Path(saved.path).exists()
    assert manager.verify(previous)['valid'] and previous.continuation_commit


def test_checkpoint_acknowledgement_loss_requires_reload_not_automatic_resume(ready,monkeypatch):
    manager,previous,*_=ready;persist=manager._persist
    def lost_ack(value):
        persist(value)
        if value.process_id==previous.process_id and value.continuation_commit:raise OSError('lost checkpoint acknowledgement')
    monkeypatch.setattr(manager,'_persist',lost_ack)
    with pytest.raises(OSError):resume(manager,previous)
    assert manager.storage.load_worktree_state('continued') is None
    monkeypatch.setattr(manager,'_persist',persist)
    with pytest.raises(WorktreeError,match='stale continuation'):resume(manager,previous)
    loaded=WorktreeLease.from_dict(manager.storage.load_worktree_state(previous.process_id))
    manager.recover_owned(loaded)
    assert manager.storage.load_worktree_state('continued') is None
    lease=resume(manager,loaded);assert manager.verify(lease)['valid']
    manager.dispose(lease);manager.dispose(loaded)


def test_runtime_outputs_from_old_execution_are_not_inherited(ready):
    from scripts.check_macos_worktree_client import seed_runtime_outputs
    from backend.core.macos_worktree_runtime import NAMES
    manager,previous,*_=ready
    seed_runtime_outputs(previous.path,'old-only')
    lease=resume(manager,previous)
    for name in NAMES:
        target=Path(lease.path)/'.gitgo'/name;root=manager.repo_root/'.gitgo'/name
        assert (target.read_bytes() if target.exists() else None)==(root.read_bytes() if root.exists() else None)
    manager.dispose(lease);manager.dispose(previous)


def test_occupied_destination_does_not_freeze_source(ready):
    manager,previous,calls,checks,_=ready
    existing=manager.create(process_id='continued',task_id='t',snapshot_commit=previous.snapshot_commit)
    before=len(calls),len(checks)
    with pytest.raises(WorktreeError,match='existing process'):resume(manager,previous)
    assert (len(calls),len(checks))==before and not previous.macos_binding.get('result')
    manager.dispose(existing)


@pytest.mark.parametrize('field',['runtime_update','runtime_merged'])
def test_unfinished_runtime_publication_blocks_continuation(ready,field):
    manager,previous,calls,checks,_=ready
    previous.macos_binding[field]={'pending':'evidence'};manager._persist(previous)
    before=len(calls),len(checks)
    with pytest.raises(WorktreeError,match='settled unpublished'):resume(manager,previous)
    assert (len(calls),len(checks))==before and manager.storage.load_worktree_state('continued') is None


def test_packaged_resume_rejection_probe_makes_no_native_calls(ready):
    from scripts.check_macos_worktree_client import resume_rejections
    manager,source,calls,checks,_=ready;before=len(calls),len(checks)
    cases=resume_rejections(manager,source)
    assert {r['case'] for r in cases}=={'forged','review','same_process','private_key'}
    assert (len(calls),len(checks))==before
    assert manager.verify(source)['valid'] and not source.continuation_commit
