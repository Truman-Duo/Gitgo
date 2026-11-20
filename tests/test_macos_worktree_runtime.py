"""Real native reads and canonical Host writes; privilege admission substituted."""
import copy
import hashlib
import json
import os
from pathlib import Path
import threading
import pytest
import sys

if sys.platform != 'darwin':
    pytest.skip('Darwin protected Git boundary', allow_module_level=True)

from backend.core import macos_worktree_runtime as runtime,macos_worktree as life
from backend.core.storage import metadata_files
from backend.core.loop.worktree import WorktreeLease,WorktreeError
from backend.core.loop.test_manifest import TestManifest,TestRecord,SeedResult
from tests.test_macos_worktree_seal import published,ready,staged,case,create,repository,commit_fixture


def observation(task):
    return json.dumps(dict(task_id=task,tool_name='read_file',files=['a.bin'],outcome='observed')).encode()+b'\n'


def evidence(path):
    path.mkdir(exist_ok=True)
    (path/'test_manifest.json').write_text(json.dumps(dict(schema_version=1,tests=[dict(test_id='unit:a',target='test_a.py',seeds=[1],results=[dict(seed=1,exit_code=0,duration_ms=1.5,output_tail='ok')])])) )
    (path/'dependency_observations.jsonl').write_bytes(observation('child'))
    (path/'dependency_feedback.json').write_text(json.dumps(dict(version=1,entries=[dict(dependent='a.py',dependency='b.py',confirmed=True)])))


def fresh(manager,lease):
    return WorktreeLease.from_dict(manager.storage.load_worktree_state(lease.process_id))


def test_three_channels_merge_once_then_seal_dispose(published):
    manager,lease,*_=published;child=Path(lease.path)/'.gitgo';evidence(child)
    root=manager.repo_root/'.gitgo';root.mkdir(exist_ok=True);(root/'dependency_observations.jsonl').write_bytes(observation('root'))
    manager.merge_runtime_outputs(lease);first={n:(root/n).read_bytes() for n in runtime.NAMES}
    manager.merge_runtime_outputs(lease);manager.recover_owned(lease)
    assert all((root/n).read_bytes()==v for n,v in first.items())
    assert first[runtime.NAMES[1]]==observation('root')+observation('child')
    assert TestManifest.load(manager.repo_root).records['unit:a'].passed
    assert json.loads(first[runtime.NAMES[2]])['entries'][0]['confirmed']
    manager.seal(lease);assert lease.state=='sealed' and manager.verify(lease)['valid']
    manager.dispose(lease);assert not child.exists() and all((root/n).read_bytes()==v for n,v in first.items())


@pytest.mark.parametrize('phase',['intent','first','last','ack'])
def test_crash_recovery_observes_without_writes_explicit_retry_finishes_once(published,monkeypatch,phase):
    manager,lease,*_=published;evidence(Path(lease.path)/'.gitgo');persist=manager._persist;write=metadata_files.write_metadata_bytes;seen=[]
    def fail_save(value):
        persist(value)
        if phase=='intent' and value.macos_binding.get('runtime_update'):raise OSError('crash')
        if phase=='ack' and value.macos_binding.get('runtime_merged'):raise OSError('crash')
    def fail_write(*a,**kw):
        value=write(*a,**kw);seen.append(a[1])
        if phase=='first' and len(seen)==1 or phase=='last' and len(seen)==3:raise OSError('crash')
        return value
    monkeypatch.setattr(manager,'_persist',fail_save);monkeypatch.setattr(metadata_files,'write_metadata_bytes',fail_write)
    with pytest.raises(OSError):manager.merge_runtime_outputs(lease)
    monkeypatch.setattr(manager,'_persist',persist);monkeypatch.setattr(metadata_files,'write_metadata_bytes',write)
    lease=fresh(manager,lease);root=manager.repo_root/'.gitgo';root.mkdir(exist_ok=True)
    before={n:(root/n).read_bytes() if (root/n).exists() else None for n in runtime.NAMES}
    manager.recover_owned(lease);manager.recover_owned(lease)
    assert all(((root/n).read_bytes() if (root/n).exists() else None)==v for n,v in before.items())
    pending=phase in ('intent','first');assert bool(lease.macos_binding.get('runtime_update'))==pending
    if pending:
        assert not manager.verify(lease)['valid']
        with pytest.raises(WorktreeError):manager.dispose(lease)
        with pytest.raises(WorktreeError):manager.stage_index(lease,['a.bin'])
        with pytest.raises(WorktreeError):manager.seal(lease)
    manager.merge_runtime_outputs(lease);assert not lease.macos_binding.get('runtime_update')
    assert (root/runtime.NAMES[1]).read_bytes()==observation('child')
    manager.merge_runtime_outputs(lease);manager.seal(lease)


@pytest.mark.parametrize('kind',['invalid_json','duplicate_key','manifest_shape','duplicate_id','nan','feedback_shape','observation_shape','partial_line','unknown','read_only','deleted','hardlink','symlink','fifo','directory'])
def test_bad_output_retained_without_root_writes(published,kind):
    manager,lease,*_=published;root=manager.repo_root/'.gitgo';root.mkdir(exist_ok=True)
    (root/'context_objects/blobs').mkdir(parents=True)
    readonly='context_objects/blobs/'+hashlib.sha256(b'{}').hexdigest()+'.json'
    (root/readonly).write_bytes(b'{}');life.copy_runtime_inputs(manager,lease,{'rules':{'pinned':'context:rules@sha256:'+hashlib.sha256(b'{}').hexdigest()}})
    child=Path(lease.path)/'.gitgo';evidence(child);name=child/runtime.NAMES[0]
    if kind=='invalid_json':name.write_bytes(b'{')
    if kind=='duplicate_key':name.write_bytes(b'{"tests":[],"tests":[]}')
    if kind=='manifest_shape':name.write_bytes(b'[]')
    if kind=='duplicate_id':name.write_text(json.dumps({'tests':[{'test_id':'x','target':'a'},{'test_id':'x','target':'b'}]}))
    if kind=='nan':name.write_bytes(b'{"tests":[],"x":NaN}')
    if kind=='feedback_shape':(child/runtime.NAMES[2]).write_bytes(b'{"entries":[{}]}')
    if kind=='observation_shape':(child/runtime.NAMES[1]).write_bytes(b'{}\n')
    if kind=='partial_line':(child/runtime.NAMES[1]).write_bytes(observation('child').rstrip())
    if kind=='unknown':(child/'other.json').write_bytes(b'{}')
    if kind=='read_only':(child/readonly).write_bytes(b'{"edited":true}')
    if kind=='deleted':(child/readonly).unlink()
    if kind in ('hardlink','symlink','fifo','directory'):
        name.unlink()
        if kind=='hardlink':os.link(child/runtime.NAMES[1],name)
        if kind=='symlink':name.symlink_to(root/readonly)
        if kind=='fifo':os.mkfifo(name)
        if kind=='directory':name.mkdir()
    before={n:p.read_bytes() for n in runtime.NAMES if (p:=root/n).exists()}
    with pytest.raises((WorktreeError,OSError,ValueError)):manager.merge_runtime_outputs(lease)
    assert not lease.macos_binding.get('runtime_update') and child.exists()
    assert before=={n:p.read_bytes() for n in runtime.NAMES if (p:=root/n).exists()}


def test_modified_observation_prefix_is_not_mistaken_for_append(published):
    manager,lease,*_=published;root=manager.repo_root/'.gitgo';root.mkdir(exist_ok=True);name=runtime.NAMES[1]
    (root/name).write_bytes(observation('before'));life.copy_runtime_inputs(manager,lease,{})
    (Path(lease.path)/'.gitgo'/name).write_bytes(observation('edited')+observation('after'))
    with pytest.raises(WorktreeError,match='prefix'):manager.merge_runtime_outputs(lease)
    assert (root/name).read_bytes()==observation('before')


def test_prefix_append_and_root_newer_observations_both_preserved(published):
    manager,lease,*_=published;root=manager.repo_root/'.gitgo';root.mkdir(exist_ok=True);name=runtime.NAMES[1]
    (root/name).write_bytes(observation('before'));life.copy_runtime_inputs(manager,lease,{})
    (Path(lease.path)/'.gitgo'/name).write_bytes(observation('before')+observation('child'))
    (root/name).write_bytes(observation('before')+observation('newer'))
    manager.merge_runtime_outputs(lease)
    assert (root/name).read_bytes()==observation('before')+observation('newer')+observation('child')


@pytest.mark.parametrize('failure',['foreign','destination','child','cancel','debt'])
def test_changed_or_unknown_outcome_retains_debt(published,monkeypatch,failure):
    manager,lease,*_=published;child=Path(lease.path)/'.gitgo';evidence(child)
    persist=manager._persist
    def crash(value):
        persist(value)
        if value.macos_binding.get('runtime_update'):raise OSError('before writes')
    monkeypatch.setattr(manager,'_persist',crash)
    with pytest.raises(OSError):manager.merge_runtime_outputs(lease)
    monkeypatch.setattr(manager,'_persist',persist);lease=fresh(manager,lease);root=manager.repo_root/'.gitgo';root.mkdir(exist_ok=True);stop=threading.Event()
    if failure=='foreign':(root/runtime.NAMES[1]).write_bytes(observation('foreign'))
    if failure=='destination':root.rename(root.with_name('.gitgo-old'));root.mkdir()
    if failure=='child':(child/runtime.NAMES[1]).write_bytes(observation('changed'))
    if failure=='cancel':stop.set()
    if failure=='debt':monkeypatch.setattr('backend.core.storage.native_invocations.pending_native_invocations',lambda *a,**k:[{}])
    with pytest.raises((WorktreeError,OSError)):manager.merge_runtime_outputs(lease,cancellation=stop)
    assert lease.macos_binding['runtime_update'] and child.exists()
    if failure=='foreign':assert (root/runtime.NAMES[1]).read_bytes()==observation('foreign')


def test_native_manifest_writer_uses_same_cas_and_canonical_seed_merge(published):
    manager,lease,*_=published
    a=TestManifest.load(manager.repo_root);b=TestManifest.load(manager.repo_root)
    a.records['x']=TestRecord('x','a',seeds=[1],results=[SeedResult(1,0,1,'ok')]);a.save()
    b.records['y']=TestRecord('y','b')
    with pytest.raises(metadata_files.MetadataFileChanged):b.save()
    a.register(TestRecord('x','a',seeds=[2],results=[SeedResult(2,0,1,'ok')]))
    assert TestManifest.load(manager.repo_root).records['x'].seeds==[1,2]


def test_known_empty_writer_locks_are_not_dropped_evidence(published):
    manager,lease,*_=published;path=Path(lease.path);evidence(path/'.gitgo')
    metadata_files.write_metadata_bytes(path,runtime.NAMES[1],observation('child'))
    manager.merge_runtime_outputs(lease);manager.seal(lease);assert manager.verify(lease)['valid']


def test_root_cas_conflict_cannot_overwrite_newer_evidence(published,monkeypatch):
    manager,lease,*_=published;evidence(Path(lease.path)/'.gitgo');write=metadata_files.write_metadata_bytes
    def conflict(workspace,name,data,**kw):
        (workspace/'.gitgo'/name).write_bytes(b'{"schema_version":1,"tests":[]}')
        return write(workspace,name,data,**kw)
    monkeypatch.setattr(metadata_files,'write_metadata_bytes',conflict)
    with pytest.raises(metadata_files.MetadataFileChanged):manager.merge_runtime_outputs(lease)
    assert lease.macos_binding['runtime_update'] and not manager.verify(lease)['valid']
    assert (manager.repo_root/'.gitgo/test_manifest.json').read_bytes()==b'{"schema_version":1,"tests":[]}'
    with pytest.raises(WorktreeError,match='unknown image'):manager.recover_owned(lease)


@pytest.mark.parametrize('name',runtime.NAMES)
def test_malformed_root_evidence_is_never_loaded_as_empty(published,name):
    manager,lease,*_=published;root=manager.repo_root/'.gitgo';root.mkdir(exist_ok=True)
    (root/name).write_bytes(b'broken');evidence(Path(lease.path)/'.gitgo')
    with pytest.raises(WorktreeError):manager.merge_runtime_outputs(lease)
    assert (root/name).read_bytes()==b'broken' and not lease.macos_binding.get('runtime_update')


def test_merge_retains_canonical_seed_and_feedback_semantics(published):
    manager,lease,*_=published;root=manager.repo_root/'.gitgo';root.mkdir(exist_ok=True);child=Path(lease.path)/'.gitgo';evidence(child)
    manifest=TestManifest.load(manager.repo_root)
    manifest.register(TestRecord('unit:a','test_a.py',seeds=[2],results=[SeedResult(2,1,2,'failed')]))
    (root/'dependency_feedback.json').write_text(json.dumps(dict(version=1,entries=[dict(dependent='a.py',dependency='b.py',confirmed=False),dict(dependent='old.py',dependency='b.py',confirmed=True)])))
    manager.merge_runtime_outputs(lease)
    record=TestManifest.load(manager.repo_root).records['unit:a']
    assert record.seeds==[2,1] and [r.exit_code for r in record.results]==[1,0] and not record.passed
    assert json.loads((root/'dependency_feedback.json').read_text())['entries']==[dict(dependent='a.py',dependency='b.py',confirmed=True),dict(dependent='old.py',dependency='b.py',confirmed=True)]


def test_output_changed_after_ack_is_not_replayed(published):
    manager,lease,*_=published;child=Path(lease.path)/'.gitgo';evidence(child);manager.merge_runtime_outputs(lease)
    before=(manager.repo_root/'.gitgo'/runtime.NAMES[1]).read_bytes()
    (child/runtime.NAMES[1]).write_bytes(before+observation('later'))
    with pytest.raises(WorktreeError):manager.merge_runtime_outputs(lease)
    with pytest.raises(WorktreeError):manager.seal(lease)
    assert (manager.repo_root/'.gitgo'/runtime.NAMES[1]).read_bytes()==before


@pytest.mark.parametrize('name',['../outside','/etc/passwd','.git/config','.gitgo/config','a/../b','./a'])
@pytest.mark.parametrize('channel',['feedback','observations'])
def test_dependency_paths_obey_canonical_business_boundary(published,name,channel):
    manager,lease,*_=published;child=Path(lease.path)/'.gitgo';evidence(child)
    if channel=='feedback':(child/runtime.NAMES[2]).write_text(json.dumps(dict(entries=[dict(dependent=name,dependency='good.py',confirmed=True)])))
    else:(child/runtime.NAMES[1]).write_bytes(json.dumps(dict(task_id='x',tool_name='read_file',files=[name])).encode()+b'\n')
    with pytest.raises((WorktreeError,ValueError)):manager.merge_runtime_outputs(lease)
    assert not lease.macos_binding.get('runtime_update')
