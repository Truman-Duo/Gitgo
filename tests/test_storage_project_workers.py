"""Actual selected-runtime initializer/verifier in disposable Host fixtures."""
from contextlib import contextmanager
from dataclasses import replace
import ctypes as C
import json,os,site,subprocess,sys,tempfile,time,uuid
from pathlib import Path

import pytest
from backend.core.storage import project_worker as worker
from backend.core.storage.project_control import ProjectControl
from backend.core.storage.project_profile import ProjectProfile,PROFILE_FILE
from backend.core.storage.project_launch import ProjectInitialization
from backend.core.storage.project_records import ProjectRecord
from backend.core.storage.runtime_control import RuntimeControl
from backend.core.storage.runtime_lifecycle import activate,select
from backend.core.storage.runtime_installation import PinnedRuntime
from backend.core.storage.runtime_launch import RuntimeInstallation
from backend.core.storage.runtime_manifest import with_runtime_copies
from backend.core.storage.envelope import StorageEnvelope
from backend.core.storage.host_records import HostLayout,HostAccount,HostInput,metadata_classes
from backend.core.storage.native_windows_host import WindowsHostRegistry
from backend.core.storage.native_windows_journal import WindowsJournalLibrary
from backend.core.storage.native_windows_process import WindowsProcessLibrary
from backend.core.storage.native_windows_scope import WindowsScopeLibrary
from tests.test_storage_runtime_control import reopen
from tests.test_storage_runtime_launch import fixture_entry,finish
from tests.test_storage_windows_tree import native,private_directory
from tests.test_storage_project_namespace import LIMITS,pristine
from scripts import build_storage_runtime as build

@contextmanager
def project_runtime_input():
    profile=ProjectProfile(__import__('sqlite3').sqlite_version,LIMITS,pristine())
    selected=os.environ.get('GITGO_STORAGE_NATIVE_TEST_LIBRARY')
    if sys.platform!='win32' or not selected:pytest.skip('Explicit native Windows runtime build required')
    binaries=Path(selected).resolve(strict=True).parent
    scope=WindowsScopeLibrary(C.CDLL(str(binaries/'gitgo-storage-scope.dll'))).open_scope()
    sid,_=scope.account();scope.close()
    packages=next(Path(path) for path in site.getsitepackages() if (Path(path)/'httpx').is_dir())
    with tempfile.TemporaryDirectory(prefix='gitgo_project_runtime_') as temporary:
        output=Path(temporary)/'input';private_directory(output,sid)
        descriptor=Path(temporary)/'profile.json';descriptor.write_bytes(profile.to_bytes())
        manifest=build.build(Path(__file__).resolve().parents[1],Path(sys.base_prefix),packages,binaries,
                             output,profile.sha256,profile_file=descriptor)
        yield manifest,profile


@pytest.fixture(scope="module")
def project_input():
    with project_runtime_input() as result:yield result

@contextmanager
def initialized_runtime(native,temporary,project_input,monkeypatch):
    scopes,trees,binaries=native;manifest,profile=project_input
    journals=WindowsJournalLibrary(C.CDLL(str(binaries/'gitgo-storage-journal.dll')))
    processes=WindowsProcessLibrary(C.CDLL(str(binaries/'gitgo-storage-process.dll')))
    scope=scopes.open_scope();sid,_=scope.account();scope.close()
    location=temporary/'profile';private_directory(location,sid);account=HostAccount(sid,str(location))
    source=PinnedRuntime(trees,manifest.to_bytes(),manifest.sha256,sid).pin()
    envelope=StorageEnvelope(1,4096,2,2*1024**3,metadata_classes(2),profile.file_classes())
    registry=WindowsHostRegistry(scopes,trees,journals,HostLayout(with_runtime_copies(envelope,manifest.content),manifest.content))
    host=registry.owner();host._attempted=True
    parent,identity=host._parent(account,create=True)
    host._bootstrap_at(account,parent,identity,HostInput.from_manifest(manifest))
    control=RuntimeControl(host);launch=RuntimeInstallation(control,control.reserve(source))
    try:
        case=registry,host,account,source,processes,launch
        with monkeypatch.context() as patch:
            fixture_entry(patch,case);launch.start(source,processes);assert finish(launch).phase=='READY'
        active=reopen(registry,account);activate(RuntimeControl(active),0);active.close()
        yield registry,account,processes
    finally:
        deadline=time.monotonic()+15
        for process in list(processes._owners.values()):
            while not process.retire():
                assert time.monotonic()<deadline;time.sleep(.01)
        for host in list(registry._owners.values()):assert host.close()
        source.close()

PROJECT_FIXTURE='''import os,sys,ctypes,traceback
from pathlib import Path
from backend.core.storage import project_worker as worker
from backend.core.storage.project_control import ProjectControl,ProjectRequest
from backend.core.storage.host_records import HostAccount,HostLayout,HOST_LEAF
from backend.core.storage.envelope import StorageEnvelope
from backend.core.storage.native_windows_scope import WindowsScopeLibrary
from backend.core.storage.native_windows_tree import WindowsTreeLibrary
from backend.core.storage.native_windows_journal import WindowsJournalLibrary
from backend.core.storage.native_windows_process import WindowsProcessLibrary
from backend.core.storage.native_windows_host import WindowsHostRegistry
from backend.core.storage.runtime_manifest import RuntimeManifest
try:
 root=Path(sys.executable).parent
 def lib(name,kind):return kind(ctypes.CDLL(str(root/('gitgo-storage-'+name+'.dll')),winmode=0x100|0x800))
 scopes=lib('scope',WindowsScopeLibrary);trees=lib('tree',WindowsTreeLibrary)
 journals=lib('journal',WindowsJournalLibrary);processes=lib('process',WindowsProcessLibrary)
 probe=scopes.open_scope();sid,_=probe.account();account=HostAccount(sid,FIXTURE_PROFILE)
 request=ProjectRequest.from_bytes(REQUEST)
 rt=probe.root(str(root));raw=probe.read(probe.open('runtime-manifest.json',rt),65536)
 manifest=RuntimeManifest.from_bytes(raw,request.runtime.manifest)
 hr=probe.root(account.root);envelope=StorageEnvelope.from_bytes(probe.read(probe.open('envelope.json',hr),8192))
 registry=WindowsHostRegistry(scopes,trees,journals,HostLayout(envelope,manifest.content))
 host=registry.owner();host._attempted=True
 parent,pid=host._parent(account,create=False);host._verify(account,pid,host._scope.open(HOST_LEAF,parent,directory=True));probe.close()
 count=0;original=ProjectControl.update
 def update(self,*args):
  global count
  result=original(self,*args);count+=1
  if count==FAULT:os._exit(0)
  return result
 ProjectControl.update=update
 if FAULT==9:
  begin=worker.MetadataBootstrap.begin
  def lost_root(self,*args):
   begin(self,*args);os._exit(0)
  worker.MetadataBootstrap.begin=lost_root
 worker.execute(host,processes,request)
 assert sys.stdin.buffer.read(1)==b'R'
except BaseException:
 traceback.print_exc();os._exit(1)
os._exit(0)
'''

def project_entry(patch,launch,account,processes, *, fault=0):
    original=processes._calls['start'];observed=[]
    def start(cookie,executable,command,directory,environment):
        assert command.value==subprocess.list2cmdline((executable,'-I','-S','-B','-m','backend.core.storage.project_worker',launch.request.to_bytes().decode('ascii')))
        code=PROJECT_FIXTURE.replace('FIXTURE_PROFILE',repr(account.profile)).replace('REQUEST',repr(launch.request.to_bytes())).replace('FAULT',str(fault))
        actual=C.create_unicode_buffer(subprocess.list2cmdline((executable,'-I','-S','-B','-c',code)))
        observed.append(executable);return original(cookie,executable,actual,directory,environment)
    patch.setitem(processes._calls,'start',start)
    def observer():
        # The retained object is published before any native attach effect.
        launch._observer=launch.registry.owner();host=launch._observer;host._attempted=True
        parent,identity=host._parent(account,create=False)
        host._verify(account,identity,host._scope.open('storage-authority-v1',parent,directory=True));return host
    patch.setattr(launch,'_new_observer',observer)
    return observed

def prepare(registry,account,project,slot=None):
    host=reopen(registry,account);selected=select(RuntimeControl(host))
    control=ProjectControl(host);control.reserve(selected,project,verify_slot=slot)
    return ProjectInitialization(control)

def run(launch,account,processes,monkeypatch, *, fault=0):
    with monkeypatch.context() as patch:
        seen=project_entry(patch,launch,account,processes,fault=fault)
        launch.start(processes)
        result=finish(launch)
        assert len(seen)==1 and 'runtimes' in Path(seen[0]).parts
        return result

def test_exact_selected_worker_and_independent_verifier_reach_ready(native,tmp_path_factory,project_input,monkeypatch):
    with initialized_runtime(native,tmp_path_factory,project_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4());initialize=prepare(registry,account,project)
        first=run(initialize,account,processes,monkeypatch)
        assert first.phase=='MATERIALIZED'
        verify=prepare(registry,account,project,first.slot)
        second=run(verify,account,processes,monkeypatch)
        assert second.phase=='READY' and first.record.identity==second.record.identity
        assert initialize._identity!=verify._identity and initialize.request.launch!=verify.request.launch
        host=reopen(registry,account)
        state=host._read_slot(first.slot).head.state
        assert state.value()['event']['mode']=='verify_initialize' and state.value()['event']['phase']=='RETIRED'
        assert ProjectRecord.from_bytes(state.project_bytes).phase=='READY'
        host.close()


@pytest.mark.parametrize('fault,expected',[(1,'UNKNOWN'),(2,'BUILDING'),(3,'STRUCTURED'),(4,'MATERIALIZED'),(9,'UNKNOWN')])
def test_initializer_crashes_preserve_exact_debt_and_never_claim_readiness(native,tmp_path_factory,project_input,monkeypatch,fault,expected):
    from backend.core.storage.models import StorageBlocked
    with initialized_runtime(native,tmp_path_factory,project_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4());launch=prepare(registry,account,project)
        result=run(launch,account,processes,monkeypatch,fault=fault)
        assert result.phase==expected
        observer=reopen(registry,account);state=observer._read_slot(result.slot).head.state
        assert state.value()['event']['phase']=='RETIRED';observer.close()
        if fault==4:
            verified=run(prepare(registry,account,project,result.slot),account,processes,monkeypatch)
            assert verified.phase=='READY'
        elif fault==9:
            # A present root without its birth ID is not adopted into the slot.
            assert state.project_bytes is None and (Path(account.root)/'candidates'/project).is_dir()
            with pytest.raises(StorageBlocked,match='lacks recorded'):
                prepare(registry,account,str(uuid.uuid4()))


@pytest.mark.parametrize('fault,expected',[(1,'MATERIALIZED'),(2,'READY')])
def test_independent_verifier_requires_its_own_committed_fact(native,tmp_path_factory,project_input,monkeypatch,fault,expected):
    with initialized_runtime(native,tmp_path_factory,project_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4());first=run(prepare(registry,account,project),account,processes,monkeypatch)
        verify=prepare(registry,account,project,first.slot)
        result=run(verify,account,processes,monkeypatch,fault=fault)
        assert result.phase==expected
        if fault==1:
            later=run(prepare(registry,account,project,first.slot),account,processes,monkeypatch)
            assert later.phase=='READY'


def test_lost_parent_terminal_acknowledgement_confirms_durability_without_relaunch(native,tmp_path_factory,project_input,monkeypatch):
    with initialized_runtime(native,tmp_path_factory,project_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4());launch=prepare(registry,account,project)
        with monkeypatch.context() as patch:
            seen=project_entry(patch,launch,account,processes)
            launch.start(processes)
            original=registry.journals._calls['update']
            def lost(*args):
                assert original(*args)==0
                raise RuntimeError('lost terminal acknowledgement')
            with monkeypatch.context() as fault:
                fault.setitem(registry.journals._calls,'update',lost)
                with pytest.raises(RuntimeError,match='lost terminal'):finish(launch)
            result=finish(launch)
            assert result.phase=='MATERIALIZED' and len(seen)==1
            assert finish(launch) is result


PARENT_FIXTURE='''import os,sys,ctypes,subprocess,time,json
from pathlib import Path
from backend.core.storage.host_records import HostAccount,HostLayout,HOST_LEAF
from backend.core.storage.envelope import StorageEnvelope
from backend.core.storage.native_windows_scope import WindowsScopeLibrary
from backend.core.storage.native_windows_tree import WindowsTreeLibrary
from backend.core.storage.native_windows_journal import WindowsJournalLibrary
from backend.core.storage.native_windows_process import WindowsProcessLibrary
from backend.core.storage.native_windows_host import WindowsHostRegistry
from backend.core.storage.runtime_manifest import RuntimeManifest
from backend.core.storage.runtime_control import RuntimeControl
from backend.core.storage.runtime_lifecycle import select
from backend.core.storage.project_control import ProjectControl
from backend.core.storage.project_launch import ProjectInitialization
root=Path(sys.executable).parent
def lib(name,kind):return kind(ctypes.CDLL(str(root/('gitgo-storage-'+name+'.dll')),winmode=0x100|0x800))
scopes=lib('scope',WindowsScopeLibrary);trees=lib('tree',WindowsTreeLibrary)
journals=lib('journal',WindowsJournalLibrary);processes=lib('process',WindowsProcessLibrary)
probe=scopes.open_scope();sid,_=probe.account();account=HostAccount(sid,PARENT_PROFILE)
rt=probe.root(str(root));raw=probe.read(probe.open('runtime-manifest.json',rt),65536)
manifest=RuntimeManifest.from_bytes(raw,PARENT_MANIFEST)
hr=probe.root(account.root);envelope=StorageEnvelope.from_bytes(probe.read(probe.open('envelope.json',hr),8192))
registry=WindowsHostRegistry(scopes,trees,journals,HostLayout(envelope,manifest.content))
host=registry.owner();host._attempted=True
parent,pid=host._parent(account,create=False);host._verify(account,pid,host._scope.open(HOST_LEAF,parent,directory=True));probe.close()
selected=select(RuntimeControl(host));control=ProjectControl(host);control.reserve(selected,PARENT_PROJECT)
launch=ProjectInitialization(control);original=processes._calls['start']
def report():
 print(json.dumps(dict(project=PARENT_PROJECT,slot=launch.request.slot,pid=os.getpid())),flush=True)
def start(cookie,executable,command,directory,environment):
 code=CHILD_TEMPLATE.replace('FIXTURE_PROFILE',repr(account.profile)).replace('REQUEST',repr(launch.request.to_bytes())).replace('FAULT',str(CHILD_FAULT))
 actual=ctypes.create_unicode_buffer(subprocess.list2cmdline((executable,'-I','-S','-B','-c',code)))
 result=original(cookie,executable,actual,directory,environment)
 if PARENT_MODE=='unknown_identity':report();os._exit(89)
 return result
processes._calls['start']=start
launch.start(processes)
deadline=time.monotonic()+30
while not launch._worker.retire(terminate=False):
 assert time.monotonic()<deadline;time.sleep(.01)
report()
if PARENT_MODE=='alive_parent':assert sys.stdin.read(1)=='R'
os._exit(89)
'''

def parent_command(registry,account,project,mode):
    host=reopen(registry,account);selected=select(RuntimeControl(host));manifest=selected.require_live();host.close()
    code=PARENT_FIXTURE.replace('PARENT_PROFILE',repr(account.profile)).replace('PARENT_MANIFEST',repr(manifest.sha256))
    code=code.replace('PARENT_PROJECT',repr(project)).replace('PARENT_MODE',repr(mode))
    code=code.replace('CHILD_TEMPLATE',repr(PROJECT_FIXTURE)).replace('CHILD_FAULT',str({'partial':3,'root_gap':9}.get(mode,0)))
    return [str(Path(manifest.root)/'python.exe'),'-I','-S','-B','-c',code]


@pytest.mark.parametrize('mode,expected',[('complete','MATERIALIZED'),('partial','STRUCTURED'),('root_gap','UNKNOWN'),('unknown_identity',None)])
def test_real_parent_exit_recovers_only_native_proven_project_lifetimes(native,tmp_path_factory,project_input,monkeypatch,mode,expected):
    from backend.core.storage.project_recovery import recover
    from backend.core.storage.models import StorageBlocked
    with initialized_runtime(native,tmp_path_factory,project_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4())
        done=subprocess.run(parent_command(registry,account,project,mode),capture_output=True,text=True,timeout=45)
        assert done.returncode==89,done.stderr
        report=json.loads(done.stdout);host=reopen(registry,account)
        before=host._read_slot(report['slot']).raw
        if expected is None:
            with pytest.raises(StorageBlocked,match='unknown native creation'):recover(ProjectControl(host),report['slot'],processes)
            assert host._read_slot(report['slot']).raw==before
        else:
            assert recover(ProjectControl(host),report['slot'],processes)==expected
            state=host._read_slot(report['slot']).head.state
            assert state.value()['event']['phase']=='RETIRED'
            if mode=='root_gap':assert state.project_bytes is None
        assert host.close()
        if mode=='complete':
            assert run(prepare(registry,account,project,report['slot']),account,processes,monkeypatch).phase=='READY'


def test_living_original_parent_blocks_recovery_after_child_exit(native,tmp_path_factory,project_input,monkeypatch):
    from backend.core.storage.project_recovery import recover
    from backend.core.storage.models import StorageBlocked
    with initialized_runtime(native,tmp_path_factory,project_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4())
        parent=subprocess.Popen(parent_command(registry,account,project,'alive_parent'),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            report=json.loads(parent.stdout.readline());host=reopen(registry,account)
            before=host._read_slot(report['slot']).raw
            with pytest.raises(StorageBlocked,match='process is still alive'):recover(ProjectControl(host),report['slot'],processes)
            assert host._read_slot(report['slot']).raw==before and host.close()
            parent.stdin.write('R');parent.stdin.flush();assert parent.wait(timeout=20)==89
            host=reopen(registry,account);assert recover(ProjectControl(host),report['slot'],processes)=='MATERIALIZED';host.close()
        finally:
            if parent.poll() is None:parent.kill();parent.wait(timeout=20)
            for stream in (parent.stdin,parent.stdout,parent.stderr):stream.close()


@pytest.mark.parametrize('operation',['ended','retire'])
def test_unknown_recovery_observation_retains_original_slot(native,tmp_path_factory,project_input,monkeypatch,operation):
    from backend.core.storage.project_recovery import recover
    with initialized_runtime(native,tmp_path_factory,project_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4())
        done=subprocess.run(parent_command(registry,account,project,'complete'),capture_output=True,text=True,timeout=45)
        assert done.returncode==89,done.stderr
        report=json.loads(done.stdout);host=reopen(registry,account);before=host._read_slot(report['slot']).raw
        original=processes._calls[operation]
        def lost(*args):
            assert original(*args)==0
            raise RuntimeError('lost recovery native response')
        with monkeypatch.context() as patch:
            patch.setitem(processes._calls,operation,lost)
            with pytest.raises(RuntimeError,match='lost recovery'):recover(ProjectControl(host),report['slot'],processes)
        assert not host._ready and processes.retained_processes
        assert host._read_slot(report['slot']).raw==before
        assert host.close() and not processes.retained_processes
        observer=reopen(registry,account)
        assert recover(ProjectControl(observer),report['slot'],processes)=='MATERIALIZED'
        assert observer.close()
