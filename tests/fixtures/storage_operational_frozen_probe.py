"""Owned temporary-account acceptance probe; never a product entry point."""
import ctypes,json,os,subprocess,sys
from pathlib import Path
import backend.core.storage.runtime as runtime_module
from backend.core.storage import StorageRuntime
from backend.core.storage.host_records import HostAccount,HostLayout
from backend.core.storage.envelope import StorageEnvelope
from backend.core.storage.runtime_manifest import RuntimeManifest
from backend.core.storage.native_windows_scope import WindowsScopeLibrary
from backend.core.storage.native_windows_tree import WindowsTreeLibrary
from backend.core.storage.native_windows_journal import WindowsJournalLibrary
from backend.core.storage.native_windows_process import WindowsProcessLibrary,WindowsStorageProcess
from backend.core.storage.native_windows_host import WindowsHostRegistry,WindowsHostOwner
from backend.core.storage.project_control import ProjectRequest
assert getattr(sys,'frozen',False)
assert Path(runtime_module.__file__).is_relative_to(Path(sys._MEIPASS))
fixture=json.loads(Path(sys.argv[1]).read_text(encoding='utf8'))
manifest=RuntimeManifest.from_bytes(fixture['manifest'].encode('ascii'),fixture['approved'])
root=Path(manifest.root)
def library(name,kind):return kind(ctypes.CDLL(str(root/('gitgo-storage-'+name+'.dll')),winmode=0x100|0x800))
scopes=library('scope',WindowsScopeLibrary);trees=library('tree',WindowsTreeLibrary)
journals=library('journal',WindowsJournalLibrary);processes=library('process',WindowsProcessLibrary)
probe=scopes.open_scope();sid,_=probe.account();account=HostAccount(sid,fixture['profile'])
host_root=probe.root(account.root)
envelope=StorageEnvelope.from_bytes(probe.read(probe.open('envelope.json',host_root),8192));probe.close()
registry=WindowsHostRegistry(scopes,trees,journals,HostLayout(envelope,manifest.content))
def attach(self):
    self._attempted=True
    parent,identity=self._parent(account,create=False)
    self._verify(account,identity,self._scope.open('storage-authority-v1',parent,directory=True))
    return self.anchor
WindowsHostOwner.attach=attach
shell=ctypes.WinDLL('shell32');shell.CommandLineToArgvW.argtypes=[ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_int)]
shell.CommandLineToArgvW.restype=ctypes.POINTER(ctypes.c_wchar_p)
kernel=ctypes.WinDLL('kernel32');kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
original_start=processes._calls['start'];launches=[]
def start(cookie,executable,command,directory,environment):
    count=ctypes.c_int();values=shell.CommandLineToArgvW(command.value,ctypes.byref(count))
    try:argv=[values[index] for index in range(count.value)]
    finally:kernel.LocalFree(values)
    assert argv[:6]==[executable,'-I','-S','-B','-m','backend.core.storage.project_worker']
    request=ProjectRequest.from_bytes(argv[6].encode('ascii'));launches.append(request.mode)
    code=fixture['worker']
    code=code.replace(' worker.execute(host,processes,request)',
        " result=worker.execute(host,processes,request)\n from backend.core.storage.journal_cells import canonical\n os.write(1,canonical(dict(version=1,request=request.request,launch=request.launch,mode=request.mode,result=result))+b'\\n')")
    code=code.replace('FIXTURE_PROFILE',repr(account.profile)).replace('REQUEST',repr(request.to_bytes())).replace('FAULT','0')
    actual=ctypes.create_unicode_buffer(subprocess.list2cmdline((executable,'-I','-S','-B','-c',code)))
    return original_start(cookie,executable,actual,directory,environment)
processes._calls['start']=start
original_read=WindowsStorageProcess.read;diagnostics=[]
def read(self,*,stderr=False):
    data=original_read(self,stderr=stderr)
    if stderr and data:diagnostics.append(data)
    return data
WindowsStorageProcess.read=read
try:
    with StorageRuntime.open_protected_project(registry,processes,project=fixture['project'],slot=fixture['slot'],workspace_hint='workspace metadata') as store:
        assert store.load_agent_session_state('p')['marker']=='source'
        reference=store.save_agent_checkpoint(dict(process_id='p',session_id='s',messages=[],expected_revision=1,session_metadata={'marker':'frozen'}))
        assert reference.revision==2
    with StorageRuntime.open_protected_project(registry,processes,project=fixture['project'],slot=fixture['slot'],workspace_hint='workspace metadata') as store:
        assert store.load_agent_session_state('p')['marker']=='frozen'
    assert not registry._owners and not processes.retained_processes
    print(json.dumps(dict(frozen=True,revision=2,launches=launches)))
except BaseException:
    if diagnostics:sys.stderr.write(b''.join(diagnostics).decode('utf8',errors='replace'))
    raise
