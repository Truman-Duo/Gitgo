"""Seal an explicit immutable result; recover acknowledgement without replay."""
import copy
import json
from pathlib import Path
import threading
import pytest
import sys

if sys.platform != 'darwin':
    pytest.skip('Darwin protected Git boundary', allow_module_level=True)

from backend.core import macos_worktree_seal as seal,macos_worktree as life,macos_broker_client as broker
from backend.core.loop.worktree import WorktreeLease,WorktreeError
from tests.test_macos_worktree_result import ready,staged,case,create,repository,commit_fixture


@pytest.fixture
def published(ready):
    manager,lease,calls,checks,raw=ready;value=manager.publish_index(lease)
    return manager,lease,calls,checks,raw,value


def test_public_seal_records_verified_result_and_preserves_head(published):
    manager,lease,calls,checks,raw,value=published
    meta=manager.repo_root/'.git/worktrees'/lease.macos_binding['metadata']
    before={p:p.read_bytes() for p in (meta/'HEAD',manager.repo_root/'.git/HEAD',manager.repo_root/'.git/index')}
    count=len(calls);manager.seal(lease)
    assert lease.state=='sealed' and lease.has_promotable_result
    assert lease.result_commit==lease.own_commit==value['commit'] and lease.changed_files==['a.bin']
    assert manager.verify(lease)['valid'] and len(calls)==count
    assert all(p.read_bytes()==v for p,v in before.items())
    stored=manager.storage.load_worktree_state(lease.process_id)
    saved=WorktreeLease.from_dict(stored);manager.recover_owned(saved)
    assert manager.storage.load_worktree_state(lease.process_id)==stored
    assert saved.macos_binding==lease.macos_binding and saved.result_commit==lease.result_commit
    manager.dispose(lease);assert raw('rev-parse',value['ref'])==value['commit']


@pytest.mark.parametrize('change',['added','ignored','deleted','bytes','mode','symlink','hardlink','fifo','metadata','metadata_alias'])
def test_no_unstaged_file_or_runtime_output_is_silently_discarded(published,change):
    import os
    manager,lease,calls,checks,raw,value=published;p=Path(lease.path);before=len(checks)
    if change=='added':(p/'new').write_bytes(b'not staged')
    if change=='ignored':(p/'.gitignore').write_text('ignored\n');(p/'ignored').write_bytes(b'not staged')
    if change=='deleted':(p/'a.bin').unlink()
    if change=='bytes':(p/'a.bin').write_bytes(b'later')
    if change=='mode':(p/'a.bin').chmod(0o755)
    if change=='symlink':(p/'external').symlink_to('/etc/passwd')
    if change=='hardlink':os.link(p/'a.bin',p/'linked')
    if change=='fifo':os.mkfifo(p/'pipe')
    if change=='metadata':(p/'.gitgo').mkdir();(p/'.gitgo/dependency_feedback.json').write_text('{}')
    if change=='metadata_alias':(p/'nested/.GITGO').mkdir()
    with pytest.raises((WorktreeError,OSError,ValueError)):manager.seal(lease)
    assert lease.state=='leased' and not lease.result_commit and len(checks)==before
    assert manager.storage.load_worktree_state(lease.process_id)['state']=='leased'


def test_unchanged_runtime_inputs_allowed_changed_inputs_retained(published):
    manager,lease,calls,checks,raw,value=published
    source=manager.repo_root/'.gitgo';source.mkdir(exist_ok=True)
    (source/'dependency_feedback.json').write_text('{"entries":[]}')
    life.copy_runtime_inputs(manager,lease,{})
    path=Path(lease.path)/'.gitgo/dependency_feedback.json';saved=path.read_bytes()
    path.write_text('{"entries":[{"dependent":"new"}]}')
    with pytest.raises(WorktreeError,match='dependency feedback'):manager.seal(lease)
    assert path.read_text()!='{"entries":[]}' and not lease.result_commit
    path.write_bytes(saved);manager.seal(lease);assert manager.verify(lease)['valid']
    assert (source/'dependency_feedback.json').read_bytes()==saved


@pytest.mark.parametrize('after',[False,True])
def test_interrupted_acknowledgement_reloads_only_the_durable_outcome(published,monkeypatch,after):
    manager,lease,calls,checks,raw,value=published;persist=manager._persist
    def crash(candidate):
        if after:persist(candidate)
        if candidate.state=='sealed':raise OSError('Host died at acknowledgement')
        if not after:persist(candidate)
    monkeypatch.setattr(manager,'_persist',crash)
    with pytest.raises(OSError):manager.seal(lease)
    assert lease.state=='leased' and not lease.result_commit
    monkeypatch.setattr(manager,'_persist',persist)
    saved=WorktreeLease.from_dict(manager.storage.load_worktree_state(lease.process_id));count=len(checks)
    manager.recover_owned(saved);manager.recover_owned(saved)
    assert saved.state==('sealed' if after else 'leased')
    assert bool(saved.result_commit)==after and len(checks)==count
    assert raw('rev-parse',value['ref'])==value['commit']


@pytest.mark.parametrize('failure',['cancel','lost','drift','debt'])
def test_failure_during_image_verification_cannot_seal(published,monkeypatch,failure):
    manager,lease,calls,checks,raw,value=published;original=broker.checkout_git_commit;stop=threading.Event();seen=[];debt=[]
    if failure=='debt':monkeypatch.setattr('backend.core.storage.native_invocations.pending_native_invocations',lambda *a,**k:debt)
    def failing(*a,**kw):
        seen.append(a[2]);image=original(*a,**kw)
        if failure=='cancel':stop.set()
        if failure=='lost':raise EOFError('unknown cleanup')
        if failure=='drift':(Path(lease.path)/'a.bin').write_bytes(b'late mutation')
        if failure=='debt':debt.append({})
        return image
    monkeypatch.setattr(broker,'checkout_git_commit',failing)
    with pytest.raises((WorktreeError,EOFError)):seal.seal(manager,lease,cancellation=stop)
    assert lease.state=='leased' and not lease.result_commit
    assert manager.storage.load_worktree_state(lease.process_id)['state']=='leased'


def test_low_level_seal_still_requires_explicit_result(ready):
    manager,lease,calls,checks,raw=ready;before=len(calls)
    with pytest.raises(WorktreeError,match='explicitly staged'):seal.seal(manager,lease)
    assert len(calls)==before and not lease.result_commit


@pytest.mark.parametrize('field',['result_commit','own_commit','changed_files','ack'])
def test_inconsistent_durable_seal_fields_refuse_execution(published,field):
    manager,lease,*_=published;manager.seal(lease)
    if field=='ack':lease.macos_binding['seal']['commit']='0'*40
    else:setattr(lease,field,[] if field=='changed_files' else '0'*40)
    manager._persist(lease)
    assert not manager.verify(lease)['valid']


def test_no_change_result_retains_no_own_delta(staged,monkeypatch):
    case,calls=staged;manager,commit,_,raw,env=case;lease=create(case)
    # Original fixture checkout supplies only the baseline. The real result
    # fixture helper supplies arbitrary verified images for this test too.
    from backend.core import macos_git_checkout as checkout,macos_git_read as read
    from backend.core.sandbox_read import ReadRoot
    import tempfile,subprocess,base64
    def prepared(project,root,oid,**kw):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);out=work/'checkout';out.mkdir()
            with ReadRoot(project/'.git/objects') as native:read.capture_objects(native,project/'.git/objects').materialize(work/'object-view')
            command=checkout.command(work/'object-view',out,oid)
            process=subprocess.run(command,capture_output=True,text=True,check=True)
            image=dict(files=[[str(p.relative_to(out)),base64.b64encode(p.read_bytes()).decode(),p.stat().st_mode&0o777] for p in out.rglob('*') if p.is_file()],directories=[str(p.relative_to(out)) for p in out.rglob('*') if p.is_dir()])
            return checkout.verify_result(oid,image,process.stdout)
    monkeypatch.setattr(broker,'checkout_git_commit',prepared)
    manager.stage_index(lease,['a.bin']);manager.publish_index(lease);manager.seal(lease)
    assert not lease.changed_files and not lease.own_commit and lease.has_promotable_result
    manager.dispose(lease);assert lease.has_promotable_result
