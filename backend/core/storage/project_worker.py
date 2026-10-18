"""Selected-runtime project initialization and independent committed-fact audit."""
import ctypes,os,sys
from dataclasses import replace
from pathlib import Path,PurePosixPath,PureWindowsPath

from .envelope import StorageEnvelope
from .host_records import HostAccount,HostLayout,require
from .native_windows_scope import WindowsScopeLibrary
from .native_windows_tree import WindowsTreeLibrary,MetadataBootstrap,TreePurpose
from .native_windows_journal import WindowsJournalLibrary
from .native_windows_process import WindowsProcessLibrary
from .native_windows_host import WindowsHostRegistry
from .native_windows_sqlite import WindowsSqliteLibrary
from .project_control import ProjectControl,ProjectRequest
from .project_records import ProjectIdentity,ProjectRecord,DIRECTORIES,FILES
from .project_namespace import ProjectNamespace
from .project_schema import ProjectDatabases
from .runtime_manifest import MANIFEST_NAME,RuntimeManifest


def attach_selected(request):
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.GetModuleFileNameW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_uint32]
    kernel.GetModuleFileNameW.restype=ctypes.c_uint32
    image=ctypes.create_unicode_buffer(32768)
    require(0<kernel.GetModuleFileNameW(None,image,len(image))<len(image),"actual worker image required")
    root=Path(image.value).parent
    def library(name,kind):return kind(ctypes.CDLL(str(root/("gitgo-storage-"+name+".dll")),winmode=0x100|0x800))
    scopes=library("scope",WindowsScopeLibrary);trees=library("tree",WindowsTreeLibrary)
    journals=library("journal",WindowsJournalLibrary);processes=library("process",WindowsProcessLibrary)
    preliminary=scopes.open_scope()
    try:
        account=HostAccount(*preliminary.account())
        input_root=preliminary.root(str(root));preliminary.volume(input_root)
        raw=preliminary.read(preliminary.open(MANIFEST_NAME,input_root),65536)
        manifest=RuntimeManifest.from_bytes(raw,request.runtime.manifest)
        require(PureWindowsPath(manifest.root)==PureWindowsPath(root)
                and manifest.root_identity==preliminary.identity(input_root)[0],"actual installed executable root changed")
        host_root=preliminary.root(account.root)
        envelope=StorageEnvelope.from_bytes(preliminary.read(preliminary.open("envelope.json",host_root),8192))
        registry=WindowsHostRegistry(scopes,trees,journals,HostLayout(envelope,manifest.content))
        host=registry.owner();anchor=host.attach()
        require(anchor.binding==request.host,"canonical Host request changed")
    finally:preliminary.close()
    return host,processes


def sqlite_library(selected):
    manifest=selected.require_live();root=Path(manifest.root)
    candidates={PurePosixPath(item.path).name:item.path for item in manifest.content.files
                if PurePosixPath(item.path).name in {"_sqlite3.pyd","sqlite3.dll"}}
    require(set(candidates)=={"_sqlite3.pyd","sqlite3.dll"}
            and sum(PurePosixPath(item.path).name in candidates for item in manifest.content.files)==2,
            "one exact pinned CPython SQLite extension and SQLite module required")
    load=lambda path:ctypes.CDLL(str(root/path),winmode=0x100|0x800)
    return WindowsSqliteLibrary(load("gitgo-storage-sqlite.dll"),load(candidates["_sqlite3.pyd"]),load(candidates["sqlite3.dll"]))


def execute(host,processes,request):
    control=ProjectControl(host)
    control.accept(request,processes)
    profile=control.profile;anchor=control.anchor
    if request.mode=="initialize":
        creator=host._new(MetadataBootstrap,host._registry.trees,TreePurpose.BOOTSTRAP,anchor.account.sid)
        creator.begin(host._members["candidates"],dict(anchor.members)["candidates"],request.project)
        record=ProjectRecord("BUILDING",profile.sha256,ProjectIdentity(request.request,creator.identity(0)))
        control.record(record)
        indices={"":0}
        for name in DIRECTORIES+FILES:
            path=PurePosixPath(name);parent="" if str(path.parent)=="." else str(path.parent)
            index=creator.add(indices[parent],path.name,directory=name in DIRECTORIES);indices[name]=index
            if name in FILES:creator.write(index,b"")
        creator.finish()
        record=replace(record,phase="STRUCTURED",identity=replace(record.identity,members=
            tuple(sorted((name,creator.identity(index)) for name,index in indices.items() if name))))
        control.record(record)
        require(host._release(creator),"initial namespace owner closure is incomplete")
    else:
        record=ProjectRecord.from_bytes(control.read(request.slot).head.state.project_bytes)
    namespace=ProjectNamespace(host,record,request.project,profile.limits).pin()
    namespace.require_empty_cas()
    databases=ProjectDatabases(namespace,sqlite_library(control.selected),request.slot,profile.schemas)
    if request.mode=="initialize":databases.initialize()
    else:databases.verify_initialized()
    for owner in reversed(tuple(databases.owners.values())):
        require(host._release(owner),"database owner closure is incomplete")
    namespace.verify()
    if record.phase=="STRUCTURED":
        record=replace(record,phase="MATERIALIZED");control.record(record)
    if request.mode=="verify_initialize":
        control.record(replace(record,phase="VERIFIED"))
    return control


def main():
    try:
        require(len(sys.argv)==2 and getattr(getattr(sys.modules["__main__"],"__spec__",None),"name",None)
                =="backend.core.storage.project_worker","closed project worker entry required")
        request=ProjectRequest.from_bytes(sys.argv[1].encode("ascii"))
        host,processes=attach_selected(request)
        execute(host,processes,request)
        os.write(1,b'{"phase":"WORKER_FINISHED","authority":"diagnostic"}\n')
        require(sys.stdin.buffer.read(1)==b"R","project worker release frame changed")
    except BaseException:
        try:os.write(2,b"Storage project worker stopped; committed facts and native retirement require reconciliation.\n")
        finally:os._exit(1)
    os._exit(0)


if __name__=="__main__":main()
