"""Selected child operation joining in temporary canonical-object fixtures."""
import time,uuid
import pytest
from tests.test_storage_operational_profile import operational_input
from tests.test_storage_project_workers import initialized_runtime,prepare,run,project_entry
from tests import test_storage_project_workers as fixtures
from tests.test_storage_windows_tree import native
from tests.test_storage_runtime_control import reopen
from backend.core.storage.runtime_control import RuntimeControl
from backend.core.storage.runtime_lifecycle import select
from backend.core.storage.project_control import ProjectControl,ProjectRequest
from backend.core.storage.operational_launch import ProjectOperation
from backend.core.storage.native_windows_process import WindowsStorageProcess
from backend.core.storage.cycle_ledger import CycleLedger
from backend.core.storage.models import StorageBlocked

def operation(registry,account,processes,monkeypatch,project,slot,mode,payload,*,fault=None,expect_output=True,lost_output=False):
    host=reopen(registry,account);selected=select(RuntimeControl(host))
    control=ProjectControl(host);request=control.reserve_operation(selected,project,slot,mode,payload)
    assert ProjectRequest.from_bytes(request.to_bytes())==request
    launch=ProjectOperation(control);diagnostics=[]
    template=fixtures.PROJECT_FIXTURE.replace(' worker.execute(host,processes,request)',
        " result=worker.execute(host,processes,request)\n from backend.core.storage.journal_cells import canonical\n os.write(1,canonical(dict(version=1,request=request.request,launch=request.launch,mode=request.mode,result=result))+b'\\n')")
    if fault is not None:template=template.replace(' result=worker.execute(host,processes,request)',FAULTS[fault]+'\n result=worker.execute(host,processes,request)')
    with monkeypatch.context() as patch:
        patch.setattr(fixtures,'PROJECT_FIXTURE',template)
        seen=project_entry(patch,launch,account,processes)
        original=WindowsStorageProcess.read
        output_fault=[]
        def read(self,*,stderr=False):
            data=original(self,stderr=stderr)
            if stderr and data:diagnostics.append(data)
            if lost_output and not stderr and data and not output_fault:
                output_fault.append(True);raise OSError('lost native output acknowledgement')
            return data
        patch.setattr(WindowsStorageProcess,'read',read)
        launch.start(processes);deadline=time.monotonic()+70
        while True:
            try:result=launch.poll()
            except OSError as error:
                assert lost_output and str(error)=='lost native output acknowledgement'
                assert launch._output_bad
                continue
            if result is not None:break
            assert time.monotonic()<deadline
            time.sleep(.01)
        assert len(seen)==1
        if expect_output:assert result.observed is not None,b''.join(diagnostics).decode('utf8',errors='replace')
        else:assert result.observed is None
        return result

FAULTS={
 'tampered_input':''' from dataclasses import replace
 from backend.core.storage.operation_payload import parse_payload,payload_bytes
 payload=parse_payload(request.payload);payload['workspace_hint']='changed after native creation'
 request=replace(request,payload=payload_bytes(request.mode,payload))''',
 'after_prepare':''' from backend.core.storage import native_checkpoint as nc
 original_birth=nc.WindowsCasWriter.create
 def interrupted_create(*args):os._exit(87)
 nc.WindowsCasWriter.create=interrupted_create''',
 'unknown_birth':''' from backend.core.storage import native_checkpoint as nc
 original_birth=nc.WindowsCasWriter.create
 def interrupted_create(*args):
  original_birth(*args);os._exit(87)
 nc.WindowsCasWriter.create=interrupted_create''',
 'after_birth':''' from backend.core.storage import native_checkpoint as nc
 def interrupted_materialize(*args):os._exit(87)
 nc.WindowsCasWriter.materialize=interrupted_materialize''',
 'after_publish':''' from backend.core.storage import native_checkpoint as nc
 original_publish=nc.NativeCheckpoint.publish
 def interrupted_publish(*args):
  original_publish(*args);os._exit(87)
 nc.NativeCheckpoint.publish=interrupted_publish''',
 'after_retire_intent':''' from backend.core.storage import operational_worker as op
 original_retire=op.native_read
 def interrupted_retire(*args,**kwargs):os._exit(87)
 op.native_read=interrupted_retire''',
 'after_retire_finish':''' from backend.core.storage import operational_worker as op
 original_retire=op.retire_checkpoint
 def interrupted_retire(*args):
  original_retire(*args);os._exit(87)
 op.retire_checkpoint=interrupted_retire''',
 'after_delete':''' from backend.core.storage import operational_worker as op
 original_delete=op.WindowsCasGeneration.delete
 def interrupted_delete(*args):
  original_delete(*args);os._exit(87)
 op.WindowsCasGeneration.delete=interrupted_delete''',
 'after_reset_verified':''' from backend.core.storage import operational_worker as op
 original_execute=op.execute
 def interrupted_execute(*args):
  original_execute(*args);os._exit(87)
 op.execute=interrupted_execute''',
}

@pytest.mark.parametrize('case',['tampered_input','lost_output'])
def test_native_request_and_output_binding_never_turn_unknown_into_publication_proof(native,tmp_path_factory,operational_input,monkeypatch,case):
    with initialized_runtime(native,tmp_path_factory,operational_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4());first=run(prepare(registry,account,project),account,processes,monkeypatch)
        ready=run(prepare(registry,account,project,first.slot),account,processes,monkeypatch)
        execute=lambda mode,payload={},**options:operation(registry,account,processes,monkeypatch,project,ready.slot,mode,payload,**options)
        payload=dict(snapshot=dict(process_id='p',session_id='s',messages=[],expected_revision=0),workspace_hint='metadata',created_at='time')
        options=dict(fault='tampered_input') if case=='tampered_input' else dict(lost_output=True)
        execute('publish_checkpoint',payload,expect_output=False,**options)
        observed=execute('verify_checkpoint').observed
        assert observed['status']==('NO_RETAINED_BATCH' if case=='tampered_input' else 'PUBLISHED')
        if case=='lost_output':assert observed['revision']==1
        execute('retire_checkpoint');execute('collect_obsolete');assert execute('reset_wal').settled

@pytest.mark.parametrize('fault,published',[('after_prepare',False),('after_birth',False),('after_publish',True)])
def test_worker_crash_is_observed_without_replaying_publication(native,tmp_path_factory,operational_input,monkeypatch,fault,published):
    with initialized_runtime(native,tmp_path_factory,operational_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4())
        first=run(prepare(registry,account,project),account,processes,monkeypatch)
        ready=run(prepare(registry,account,project,first.slot),account,processes,monkeypatch)
        execute=lambda mode,payload={},**options:operation(registry,account,processes,monkeypatch,project,ready.slot,mode,payload,**options)
        payload=dict(snapshot=dict(process_id='p',session_id='s',task_id='t',status='running',messages=[],expected_revision=0),workspace_hint='metadata',created_at='time')
        execute('publish_checkpoint',payload,fault=fault,expect_output=False)
        result=execute('verify_checkpoint').observed
        assert result['status']==('PUBLISHED' if published else 'NOT_PUBLISHED')
        if published:assert result['revision']==1
        execute('retire_checkpoint');execute('collect_obsolete');assert execute('reset_wal').settled
        session=execute('read_checkpoint',{'process_id':'p'}).observed['session']
        assert (session is not None)==published

@pytest.mark.parametrize('fault',['after_retire_intent','after_retire_finish'])
def test_cleanup_crash_consumes_separate_finite_reconciliation_range(native,tmp_path_factory,operational_input,monkeypatch,fault):
    with initialized_runtime(native,tmp_path_factory,operational_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4());first=run(prepare(registry,account,project),account,processes,monkeypatch)
        ready=run(prepare(registry,account,project,first.slot),account,processes,monkeypatch)
        execute=lambda mode,payload={},**options:operation(registry,account,processes,monkeypatch,project,ready.slot,mode,payload,**options)
        execute('publish_checkpoint',dict(snapshot=dict(process_id='p',session_id='s',messages=[],expected_revision=0),workspace_hint='metadata',created_at='time'))
        assert execute('verify_checkpoint').observed['status']=='PUBLISHED'
        execute('retire_checkpoint',fault=fault,expect_output=False)
        execute('reconcile_preparation');execute('collect_obsolete')
        assert execute('reset_wal',fault='after_reset_verified',expect_output=False).settled
        assert execute('read_checkpoint',{'process_id':'p'}).observed['session']['session_id']=='s'
        observer=reopen(registry,account)
        assert CycleLedger.from_bytes(observer._read_slot(ready.slot).head.state.cycle_bytes).attempts==25
        observer.close()

def test_unknown_file_birth_blocks_replay_and_preserves_the_file(native,tmp_path_factory,operational_input,monkeypatch):
    from pathlib import Path
    with initialized_runtime(native,tmp_path_factory,operational_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4());first=run(prepare(registry,account,project),account,processes,monkeypatch)
        ready=run(prepare(registry,account,project,first.slot),account,processes,monkeypatch)
        execute=lambda mode,payload={},**options:operation(registry,account,processes,monkeypatch,project,ready.slot,mode,payload,**options)
        payload=dict(snapshot=dict(process_id='p',session_id='s',messages=[],expected_revision=0),workspace_hint='metadata',created_at='time')
        execute('publish_checkpoint',payload,fault='unknown_birth',expect_output=False)
        files=tuple((Path(account.root)/'candidates'/project/'data/cas').iterdir())
        assert len(files)==1 and files[0].stat().st_size==0
        assert execute('verify_checkpoint').observed['status']=='NOT_PUBLISHED'
        execute('retire_checkpoint',expect_output=False)
        assert files[0].is_file()
        observer=reopen(registry,account);before=observer._read_slot(ready.slot).raw
        selected=select(RuntimeControl(observer));control=ProjectControl(observer)
        with pytest.raises(StorageBlocked,match='unsettled'):
            control.reserve_operation(selected,project,ready.slot,'publish_checkpoint',payload)
        assert observer._read_slot(ready.slot).raw==before
        observer.close()
        assert files[0].is_file()

def test_collection_crash_after_native_delete_reconciles_exact_absence(native,tmp_path_factory,operational_input,monkeypatch):
    with initialized_runtime(native,tmp_path_factory,operational_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4());first=run(prepare(registry,account,project),account,processes,monkeypatch)
        ready=run(prepare(registry,account,project,first.slot),account,processes,monkeypatch)
        execute=lambda mode,payload={},**options:operation(registry,account,processes,monkeypatch,project,ready.slot,mode,payload,**options)
        for revision in (1,2):
            execute('publish_checkpoint',dict(snapshot=dict(process_id='p',session_id='s',messages=[],expected_revision=revision-1),workspace_hint='metadata',created_at='time'))
            assert execute('verify_checkpoint').observed['revision']==revision
            execute('retire_checkpoint')
            if revision==1:execute('collect_obsolete')
            else:
                execute('collect_obsolete',fault='after_delete',expect_output=False)
                assert execute('reconcile_collection').observed['count']>0
            assert execute('reset_wal').settled
        assert execute('read_checkpoint',{'process_id':'p'}).observed['session']['session_id']=='s'

def test_two_real_worker_cycles_save_read_and_release_only_after_reset(native,tmp_path_factory,operational_input,monkeypatch):
    with initialized_runtime(native,tmp_path_factory,operational_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4())
        first=run(prepare(registry,account,project),account,processes,monkeypatch)
        ready=run(prepare(registry,account,project,first.slot),account,processes,monkeypatch)
        assert ready.phase=='READY'
        execute=lambda mode,payload={}:operation(registry,account,processes,monkeypatch,project,ready.slot,mode,payload)
        assert execute('read_checkpoint',{'process_id':'p'}).observed==dict(status='READ',session=None)
        for revision in (1,2):
            payload=dict(snapshot=dict(process_id='p',session_id='s',task_id='t',status='running',messages=[],
                expected_revision=revision-1,session_metadata={'marker':str(revision)}),workspace_hint='metadata only',created_at='fixture-time')
            published=execute('publish_checkpoint',payload)
            assert published.observed==dict(status='PUBLICATION_ATTEMPT_FINISHED')
            verified=execute('verify_checkpoint')
            assert verified.observed['status']=='PUBLISHED' and verified.observed['revision']==revision
            assert execute('retire_checkpoint').observed['status']=='RETIRED'
            collected=execute('collect_obsolete')
            assert collected.observed['status']=='COLLECTED'
            if revision==2:assert collected.observed['count']>0
            reset=execute('reset_wal');assert reset.settled
            actual=execute('read_checkpoint',{'process_id':'p'})
            assert actual.observed['session']==dict(session_id='s',messages=[],provider_state={},marker=str(revision))
            observer=reopen(registry,account)
            ledger=CycleLedger.from_bytes(observer._read_slot(ready.slot).head.state.cycle_bytes)
            assert ledger.phase=='SETTLED' and ledger.epoch==revision and ledger.attempts==23
            observer.close()

def test_unsettled_cycle_keeps_runtime_epoch_then_allows_rotation_after_settlement(native,tmp_path_factory,operational_input,monkeypatch):
    from tests.test_storage_runtime_lifecycle import new_work,run as run_runtime,promote
    with initialized_runtime(native,tmp_path_factory,operational_input,monkeypatch) as (registry,account,processes):
        candidate=new_work(registry,account,operational_input[0],processes)
        assert run_runtime(monkeypatch,candidate).phase=='READY'
        project=str(uuid.uuid4());first=run(prepare(registry,account,project),account,processes,monkeypatch)
        ready=run(prepare(registry,account,project,first.slot),account,processes,monkeypatch)
        execute=lambda mode,payload={}:operation(registry,account,processes,monkeypatch,project,ready.slot,mode,payload)
        execute('publish_checkpoint',dict(snapshot=dict(process_id='p',session_id='s',messages=[],expected_revision=0),workspace_hint='metadata',created_at='time'))
        observer=reopen(registry,account);before=observer._read_slot(63).raw;observer.close()
        with pytest.raises(StorageBlocked,match='cycle retains its runtime lease'):promote(registry,account,1)
        observer=reopen(registry,account)
        assert observer._read_slot(63).raw==before;observer.close()
        assert execute('verify_checkpoint').observed['revision']==1
        execute('retire_checkpoint');execute('collect_obsolete');assert execute('reset_wal').settled
        assert promote(registry,account,1).epoch==2
        assert execute('read_checkpoint',{'process_id':'p'}).observed['session']['session_id']=='s'
