"""Real parent death; no self-reported process exit substitutes for OS facts."""
import json,subprocess,uuid
from pathlib import Path
import pytest
from tests.test_storage_operational_profile import operational_input
from tests.test_storage_operational_workers import operation
from tests.test_storage_project_workers import initialized_runtime,prepare,run,PARENT_FIXTURE,PROJECT_FIXTURE
from tests.test_storage_windows_tree import native
from tests.test_storage_runtime_control import reopen
from backend.core.storage.runtime_control import RuntimeControl
from backend.core.storage.runtime_lifecycle import select
from backend.core.storage.project_control import ProjectControl
from backend.core.storage.project_recovery import recover
from backend.core.storage.models import StorageBlocked

def parent_command(registry,account,project,slot,mode):
    host=reopen(registry,account);selected=select(RuntimeControl(host));manifest=selected.require_live();host.close()
    code=PARENT_FIXTURE.replace('control.reserve(selected,PARENT_PROJECT)',
        "control.reserve_operation(selected,PARENT_PROJECT,PARENT_SLOT,'publish_checkpoint',PARENT_PAYLOAD)")
    code=code.replace('launch=ProjectInitialization(control)',
        'from backend.core.storage.operational_launch import ProjectOperation\nlaunch=ProjectOperation(control)')
    payload=dict(snapshot=dict(process_id='p',session_id='s',messages=[],expected_revision=0),workspace_hint='metadata',created_at='time')
    code=code.replace('PARENT_SLOT',str(slot)).replace('PARENT_PAYLOAD',repr(payload))
    code=code.replace('PARENT_PROFILE',repr(account.profile)).replace('PARENT_MANIFEST',repr(manifest.sha256))
    code=code.replace('PARENT_PROJECT',repr(project)).replace('PARENT_MODE',repr(mode))
    code=code.replace('CHILD_TEMPLATE',repr(PROJECT_FIXTURE)).replace('CHILD_FAULT','0')
    return [str(Path(manifest.root)/'python.exe'),'-I','-S','-B','-c',code]

@pytest.mark.parametrize('mode',['complete','unknown_identity','alive_parent'])
def test_original_parent_lifetime_controls_operational_recovery(native,tmp_path_factory,operational_input,monkeypatch,mode):
    with initialized_runtime(native,tmp_path_factory,operational_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4());first=run(prepare(registry,account,project),account,processes,monkeypatch)
        ready=run(prepare(registry,account,project,first.slot),account,processes,monkeypatch)
        parent=subprocess.Popen(parent_command(registry,account,project,ready.slot,mode),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            report=json.loads(parent.stdout.readline());assert report['slot']==ready.slot
            if mode=='alive_parent':
                observer=reopen(registry,account);before=observer._read_slot(ready.slot).raw
                with pytest.raises(StorageBlocked,match='process is still alive'):recover(ProjectControl(observer),ready.slot,processes)
                assert observer._read_slot(ready.slot).raw==before;observer.close()
                parent.stdin.write('R');parent.stdin.flush()
            assert parent.wait(timeout=30)==89,parent.stderr.read()
            observer=reopen(registry,account);before=observer._read_slot(ready.slot).raw
            if mode=='unknown_identity':
                with pytest.raises(StorageBlocked,match='unknown native creation'):recover(ProjectControl(observer),ready.slot,processes)
                assert observer._read_slot(ready.slot).raw==before;observer.close();return
            assert recover(ProjectControl(observer),ready.slot,processes)=='OPEN'
            assert observer._read_slot(ready.slot).head.state.value()['event']['phase']=='RETIRED';observer.close()
            execute=lambda mode,payload={}:operation(registry,account,processes,monkeypatch,project,ready.slot,mode,payload)
            assert execute('verify_checkpoint').observed['revision']==1
            execute('retire_checkpoint');execute('collect_obsolete');assert execute('reset_wal').settled
            assert execute('read_checkpoint',{'process_id':'p'}).observed['session']['session_id']=='s'
        finally:
            if parent.poll() is None:parent.kill();parent.wait(timeout=20)
            for stream in (parent.stdin,parent.stdout,parent.stderr):stream.close()
