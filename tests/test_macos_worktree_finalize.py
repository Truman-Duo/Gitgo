"""Public seal merges verified runtime outputs before acknowledging completion."""
import copy
import json
from pathlib import Path
import pytest
import sys

if sys.platform != 'darwin':
    pytest.skip('Darwin protected Git boundary', allow_module_level=True)

from backend.core import macos_worktree_runtime as runtime,macos_broker_client as broker
from backend.core.loop.worktree import WorktreeLease,WorktreeError
from backend.core.storage import metadata_files
from backend.core.dependency_graph import DependencyGraph,_save_graph,record_dependency_feedback,record_tool_observation
from tests.test_macos_worktree_runtime import evidence,observation
from tests.test_macos_worktree_seal import published,ready,staged,case,create,repository,commit_fixture


def test_public_seal_automatically_merges_three_channels(published):
    manager,lease,*_=published;evidence(Path(lease.path)/'.gitgo')
    manager.seal(lease)
    assert lease.state=='sealed' and lease.macos_binding['runtime_merged'] and manager.verify(lease)['valid']
    root=manager.repo_root/'.gitgo';assert (root/runtime.NAMES[1]).read_bytes()==observation('child')
    before={n:(root/n).read_bytes() for n in runtime.NAMES};manager.recover_owned(lease);manager.dispose(lease)
    assert all((root/n).read_bytes()==v for n,v in before.items())


def test_canonical_feedback_tool_cache_does_not_block_completion(published):
    manager,lease,*_=published;work=Path(lease.path);root=manager.repo_root/'.gitgo';root.mkdir(exist_ok=True)
    # The root's derived cache remains its own, refreshed through its existing
    # fingerprint checks. The worker cache never replaces it.
    _save_graph(DependencyGraph(manager.repo_root));before=(root/runtime.CACHES[0]).read_bytes()
    record_tool_observation(work,task_id='cache-test',tool_name='read_file',files=['nested/run.sh','.gitattributes'])
    value=record_dependency_feedback(work,dependent='nested/run.sh',dependency='.gitattributes',confirmed=True)
    assert 'warnings' not in value and (work/'.gitgo'/runtime.CACHES[0]).is_file()
    child_cache=(work/'.gitgo'/runtime.CACHES[0]).read_bytes()
    manager.seal(lease)
    assert lease.state=='sealed' and (root/runtime.CACHES[0]).read_bytes()==before
    assert lease.macos_binding['runtime_merged']['derived_caches']=={runtime.CACHES[0]:runtime.digest(child_cache)}
    assert lease.macos_binding['seal']['derived_caches']==lease.macos_binding['runtime_merged']['derived_caches']
    assert json.loads((root/runtime.NAMES[2]).read_bytes())['entries'][0]['confirmed']


@pytest.mark.parametrize('failure',['unstaged','cancel','producer','drift','secret'])
def test_business_verification_precedes_runtime_effects(published,monkeypatch,failure):
    import threading
    from backend.core import macos_worktree_seal
    manager,lease,*_=published;work=Path(lease.path);evidence(work/'.gitgo');stop=threading.Event()
    if failure=='unstaged':(work/'a.bin').write_bytes(b'not staged')
    if failure=='secret':monkeypatch.setattr('backend.core.macos_worktree_result.scan_files',lambda *a:(_ for _ in ()).throw(WorktreeError('secret')))
    original=broker.checkout_git_commit
    def changed(*a,**kw):
        image=original(*a,**kw)
        if failure=='producer':raise EOFError('no cleanup confirmation')
        if failure=='cancel':stop.set()
        if failure=='drift':(work/'a.bin').write_bytes(b'late change')
        return image
    monkeypatch.setattr(broker,'checkout_git_commit',changed)
    with pytest.raises((WorktreeError,EOFError)):macos_worktree_seal.seal(manager,lease,cancellation=stop)
    assert lease.state=='leased' and not lease.macos_binding.get('runtime_merged') and not lease.macos_binding.get('runtime_update')
    assert not (manager.repo_root/'.gitgo/test_manifest.json').exists()


@pytest.mark.parametrize('phase',['intent','first','last','merged','sealed'])
def test_public_seal_crash_does_not_implicitly_resume_task(published,monkeypatch,phase):
    manager,lease,*_=published;work=Path(lease.path);evidence(work/'.gitgo');persist=manager._persist;write=metadata_files.write_metadata_bytes;count=[]
    def crash_save(value):
        persist(value)
        if phase=='intent' and value.macos_binding.get('runtime_update'):raise OSError('death')
        if phase=='merged' and value.macos_binding.get('runtime_merged'):raise OSError('death')
        if phase=='sealed' and value.state=='sealed':raise OSError('death')
    def crash_write(*a,**kw):
        result=write(*a,**kw);count.append(a[1])
        if phase=='first' and len(count)==1 or phase=='last' and len(count)==3:raise OSError('death')
        return result
    monkeypatch.setattr(manager,'_persist',crash_save);monkeypatch.setattr(metadata_files,'write_metadata_bytes',crash_write)
    with pytest.raises(OSError):manager.seal(lease)
    monkeypatch.setattr(manager,'_persist',persist);monkeypatch.setattr(metadata_files,'write_metadata_bytes',write)
    lease=WorktreeLease.from_dict(manager.storage.load_worktree_state(lease.process_id));root=manager.repo_root/'.gitgo'
    before={n:(root/n).read_bytes() if (root/n).exists() else None for n in runtime.NAMES}
    manager.recover_owned(lease);manager.recover_owned(lease)
    assert all(((root/n).read_bytes() if (root/n).exists() else None)==v for n,v in before.items())
    assert (lease.state=='sealed')==(phase=='sealed')
    if phase in ('intent','first'):
        with pytest.raises(WorktreeError):manager.seal(lease)
        manager.merge_runtime_outputs(lease)
    if lease.state!='sealed':manager.seal(lease)
    assert (root/runtime.NAMES[1]).read_bytes()==observation('child')
    assert manager.verify(lease)['valid']


@pytest.mark.parametrize('kind',['schema','extra','alias','link','unknown','lock_payload'])
def test_cache_recognition_never_swallows_unknown_evidence(published,kind):
    manager,lease,*_=published;work=Path(lease.path);evidence(work/'.gitgo');graph=DependencyGraph(work).to_dict();name=work/'.gitgo'/runtime.CACHES[0]
    if kind=='schema':graph['version']=100
    if kind=='extra':graph['audit_evidence']='must not be silently discarded'
    if kind=='unknown':name=work/'.gitgo/another-cache.json'
    if kind=='alias':name=work/'.gitgo/Dependency_Graph.v2.json'
    if kind=='link':name.symlink_to('/etc/passwd')
    else:name.write_text(json.dumps(graph))
    if kind=='lock_payload':
        import hashlib
        lock=work/'.gitgo/metadata-locks'/hashlib.sha256(runtime.CACHES[0].encode()).hexdigest()/'storage-maintenance.lock'
        lock.parent.mkdir(parents=True);lock.write_text('evidence')
    with pytest.raises((WorktreeError,OSError)):manager.seal(lease)
    assert not lease.macos_binding.get('runtime_update') and not lease.macos_binding.get('runtime_merged')


def test_business_drift_after_merge_retains_ack_but_never_seals(published,monkeypatch):
    manager,lease,*_=published;work=Path(lease.path);evidence(work/'.gitgo');merge=runtime.merge
    def changed(*a,**kw):
        value=merge(*a,**kw);(work/'a.bin').write_bytes(b'late');return value
    monkeypatch.setattr(runtime,'merge',changed)
    with pytest.raises(WorktreeError,match='changed after runtime merge'):manager.seal(lease)
    assert lease.state=='leased' and lease.macos_binding['runtime_merged'] and not lease.result_commit
    assert (manager.repo_root/'.gitgo'/runtime.NAMES[1]).read_bytes()==observation('child')


def test_disposable_cache_removal_preserves_canonical_root_copy(published):
    from backend.core import macos_worktree as life
    manager,lease,*_=published;root=manager.repo_root/'.gitgo';root.mkdir(exist_ok=True)
    _save_graph(DependencyGraph(manager.repo_root));before=(root/runtime.CACHES[0]).read_bytes()
    life.copy_runtime_inputs(manager,lease,{})
    (Path(lease.path)/'.gitgo'/runtime.CACHES[0]).unlink()
    manager.seal(lease)
    assert lease.macos_binding['seal']['derived_caches']=={runtime.CACHES[0]:None}
    assert (root/runtime.CACHES[0]).read_bytes()==before and manager.verify(lease)['valid']
