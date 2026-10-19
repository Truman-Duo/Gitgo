"""Existing public facade methods using real selected children and private Host."""
from contextlib import contextmanager
from pathlib import Path
import ctypes,subprocess,uuid
import pytest
from tests.test_storage_operational_profile import operational_input
from tests.test_storage_project_workers import initialized_runtime,PROJECT_FIXTURE
from tests.test_storage_windows_tree import native
from backend.core.storage import StorageRuntime
from backend.core.storage.native_windows_host import WindowsHostOwner
from backend.core.storage.project_control import ProjectRequest
from backend.core.storage.models import StorageBlocked

@contextmanager
def fixture_route(patch,account,processes):
    def attach(self):
        self._attempted=True
        parent,identity=self._parent(account,create=False)
        self._verify(account,identity,self._scope.open('storage-authority-v1',parent,directory=True))
        return self.anchor
    patch.setattr(WindowsHostOwner,'attach',attach)
    original_start=processes._calls['start'];requests=[]
    from backend.core.storage.native_windows_process import WindowsStorageProcess
    original_read=WindowsStorageProcess.read
    def read(self,*,stderr=False):
        data=original_read(self,stderr=stderr)
        if stderr and data:diagnostics.append(data)
        return data
    diagnostics=[];patch.setattr(WindowsStorageProcess,'read',read)
    # Command-line parsing in this test only: launch arguments are already the
    # exact production ProjectRequest encoding and the selected image is real.
    shell=ctypes.WinDLL('shell32');shell.CommandLineToArgvW.argtypes=[ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype=ctypes.POINTER(ctypes.c_wchar_p)
    kernel=ctypes.WinDLL('kernel32');kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
    def start(cookie,executable,command,directory,environment):
        count=ctypes.c_int();values=shell.CommandLineToArgvW(command.value,ctypes.byref(count))
        try:argv=[values[index] for index in range(count.value)]
        finally:kernel.LocalFree(values)
        assert argv[:6]==[executable,'-I','-S','-B','-m','backend.core.storage.project_worker']
        request=ProjectRequest.from_bytes(argv[6].encode('ascii'));requests.append(request)
        code=PROJECT_FIXTURE
        if request.payload is not None:
            code=code.replace(' worker.execute(host,processes,request)',
                " result=worker.execute(host,processes,request)\n from backend.core.storage.journal_cells import canonical\n os.write(1,canonical(dict(version=1,request=request.request,launch=request.launch,mode=request.mode,result=result))+b'\\n')")
        code=code.replace('FIXTURE_PROFILE',repr(account.profile)).replace('REQUEST',repr(request.to_bytes())).replace('FAULT','0')
        actual=ctypes.create_unicode_buffer(subprocess.list2cmdline((executable,'-I','-S','-B','-c',code)))
        return original_start(cookie,executable,actual,directory,environment)
    patch.setitem(processes._calls,'start',start)
    yield requests,diagnostics

def test_public_storage_facade_creates_saves_reopens_and_reads(native,tmp_path_factory,operational_input,monkeypatch):
    with initialized_runtime(native,tmp_path_factory,operational_input,monkeypatch) as (registry,account,processes):
        with monkeypatch.context() as patch,fixture_route(patch,account,processes) as (requests,diagnostics):
            store=StorageRuntime.create_protected_project(registry,processes,workspace_hint='workspace metadata')
            project,slot=store.project,store.slot
            try:
                ref=store.save_agent_checkpoint(dict(process_id='p',session_id='s',messages=[],expected_revision=0,session_metadata={'marker':'kept'}))
                assert str(ref).startswith('sqlite:session/p/') and ref.revision==1
                count=len(requests)
                with pytest.raises(StorageBlocked,match='UNSUPPORTED'):store.maintain_cas()
                assert len(requests)==count
                with pytest.raises(StorageBlocked,match='observed revision'):
                    store.save_agent_checkpoint(dict(process_id='p',session_id='s',messages=[]))
                assert len(requests)==count
            except Exception:
                if diagnostics:print(b''.join(diagnostics).decode('utf8',errors='replace'))
                raise
            finally:store.close()
            with StorageRuntime.open_protected_project(registry,processes,project=project,slot=slot,workspace_hint='workspace metadata') as reopened:
                assert reopened.load_agent_session_state('p')==dict(session_id='s',messages=[],provider_state={},marker='kept')
                second=reopened.save_agent_checkpoint(dict(process_id='p',session_id='s',messages=[],expected_revision=1,session_metadata={'marker':'updated'}))
                assert second.revision==2
                assert reopened.load_agent_session_state('p')['marker']=='updated'
            assert not registry._owners and not processes.retained_processes


def test_lost_host_attach_retains_original_owner_until_close(native,tmp_path_factory,operational_input,monkeypatch):
    with initialized_runtime(native,tmp_path_factory,operational_input,monkeypatch) as (registry,account,processes):
        with monkeypatch.context() as patch,fixture_route(patch,account,processes) as (requests,diagnostics):
            store=StorageRuntime.create_protected_project(registry,processes,workspace_hint='workspace metadata')
            original=WindowsHostOwner.attach;count=[]
            def lost(self):
                original(self);count.append(True);raise OSError('lost Host attach acknowledgement')
            with monkeypatch.context() as fault:
                fault.setattr(WindowsHostOwner,'attach',lost)
                with pytest.raises(OSError,match='lost Host attach'):store.load_agent_session_state('p')
                assert len(store._hosts)==1
                with pytest.raises(StorageBlocked,match='previous native operation'):store.load_agent_session_state('p')
                assert len(count)==1 and len(store._hosts)==1
            store.close()
            assert not registry._owners and not processes.retained_processes


def test_refused_revision_or_capacity_preserves_last_committed_checkpoint(native,tmp_path_factory,operational_input,monkeypatch):
    with initialized_runtime(native,tmp_path_factory,operational_input,monkeypatch) as (registry,account,processes):
        with monkeypatch.context() as patch,fixture_route(patch,account,processes):
            with StorageRuntime.create_protected_project(registry,processes,workspace_hint='workspace metadata') as store:
                first=dict(process_id='p',session_id='s',messages=[],expected_revision=0,session_metadata={'marker':'kept'})
                assert store.save_agent_checkpoint(first).revision==1
                for bad in (first,dict(first,expected_revision=1,messages=[{'role':'user','content':str(index)} for index in range(9)])):
                    with pytest.raises(StorageBlocked,match='NOT_PUBLISHED'):store.save_agent_checkpoint(bad)
                    assert store.load_agent_session_state('p')['marker']=='kept'
                assert store.save_agent_checkpoint(dict(first,expected_revision=1,session_metadata={'marker':'next'})).revision==2
                assert store.load_agent_session_state('p')['marker']=='next'


def test_nonempty_conversation_provider_state_and_metadata_roundtrip(native,tmp_path_factory,operational_input,monkeypatch):
    messages=[{'role':'user','content':'保存后重新打开：hello'},
              {'role':'assistant','content':'已记录','provider_state_id':'state-1'}]
    providers={'state-1':{'reasoning':'retained state','version':1}}
    with initialized_runtime(native,tmp_path_factory,operational_input,monkeypatch) as (registry,account,processes):
        with monkeypatch.context() as patch,fixture_route(patch,account,processes):
            with StorageRuntime.create_protected_project(registry,processes,workspace_hint='content acceptance') as store:
                project,slot=store.project,store.slot
                data=dict(process_id='p',session_id='s',messages=messages,provider_state=providers,
                          session_metadata={'marker':'content'},expected_revision=0)
                assert store.save_agent_checkpoint(data).revision==1
            with StorageRuntime.open_protected_project(registry,processes,project=project,slot=slot,workspace_hint='content acceptance') as store:
                observed=store.load_agent_session_state('p')
                assert observed==dict(session_id='s',messages=messages,provider_state=providers,marker='content')
                messages[1]['content']='已更新'
                assert store.save_agent_checkpoint(dict(data,expected_revision=1)).revision==2
                assert store.load_agent_session_state('p')['messages']==messages
            assert not registry._owners and not processes.retained_processes
