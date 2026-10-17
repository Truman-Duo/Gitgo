"""Trusted runtime materialization after persisted native launch admission.

This worker does not select another Host authority or approve an input package.
The canonical Host record and exact native process identity supply admission.
Diagnostic output is never an installation or cleanup receipt.
"""
from dataclasses import dataclass, asdict
import ctypes
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import struct
import sys

from .host_records import HostAccount, HostLayout, require, guid
from .envelope import StorageEnvelope
from .journal_cells import decode
from .native_windows_host import WindowsHostOwner, WindowsHostRegistry
from .native_windows_scope import WindowsScopeLibrary
from .native_windows_journal import WindowsJournalLibrary
from .native_windows_process import WindowsProcessLibrary, WindowsStorageProcess
from .native_windows_tree import RuntimeInstaller, TreePurpose, WindowsTreeLibrary
from .runtime_control import RuntimeControl, RuntimeInstallPlan
from .runtime_installation import PinnedRuntime
from .runtime_manifest import MANIFEST_NAME, RuntimeManifest, canonical, digest


@dataclass(frozen=True)
class RuntimeInstallRequest:
    host: str
    slot: int
    generation: str
    source: str
    plan: str
    launch: str

    def __post_init__(self):
        digest(self.host);digest(self.source);digest(self.plan)
        guid(self.generation);guid(self.launch)
        require(type(self.slot) is int and self.slot in (0,1),"one runtime slot required")

    def to_bytes(self):
        return canonical(dict(version=1,mode="install",**asdict(self)))

    @classmethod
    def from_bytes(cls, raw):
        value=decode(raw)
        require(type(value) is dict and set(value)=={"version","mode","host","slot","generation","source","plan","launch"},
                "closed runtime worker request required")
        require(type(value["version"]) is int and value["version"]==1 and value["mode"]=="install",
                "unknown runtime worker protocol or mode")
        return cls(**{key:item for key,item in value.items() if key not in {"version","mode"}})


def require_input_execution(source, context):
    """Validate the already loaded input role before any materialization effect."""
    root=PureWindowsPath(source.manifest.root)
    require(PureWindowsPath(context.image)==root/"python.exe" and PureWindowsPath(sys.executable)==root/"python.exe",
            "runtime installer must execute the exact pinned input interpreter")
    require(source.manifest.content.python_abi=="cpython-314-win_amd64" and sys.platform=="win32"
            and sys.version_info[:2]==(3,14) and struct.calcsize("P")==8,"qualified input Python ABI required")
    require(sys.flags.isolated==1 and sys.flags.no_site==1 and sys.dont_write_bytecode,
            "closed input interpreter flags required")
    require(PureWindowsPath(sys.prefix)==root and [PureWindowsPath(value) for value in sys.path]==[
        root/"python314.zip",root/"DLLs",root/"app.zip",root],"closed input module path required")
    require(set(os.environ)=={"PATH","SYSTEMROOT","WINDIR"}
            and PureWindowsPath(os.environ["PATH"])==root
            and all(PureWindowsPath(os.environ[key])==PureWindowsPath(context.system_root) for key in ("SYSTEMROOT","WINDIR")),
            "closed input environment required")
    for module in tuple(sys.modules.values()):
        origin=getattr(getattr(module,"__spec__",None),"origin",None)
        if origin and origin not in {"built-in","frozen"}:
            require(PureWindowsPath(origin).is_relative_to(root),"Python module loaded outside the input runtime")
    # Pseudo-process handle and a bounded module snapshot; no second open
    # process handle or Python cleanup callback is introduced by this audit.
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.GetCurrentProcess.restype=ctypes.c_void_p
    kernel.K32EnumProcessModules.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint32,ctypes.POINTER(ctypes.c_uint32)]
    kernel.K32EnumProcessModules.restype=ctypes.c_int
    kernel.GetModuleFileNameW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_uint32]
    kernel.GetModuleFileNameW.restype=ctypes.c_uint32
    modules=(ctypes.c_void_p*512)();needed=ctypes.c_uint32()
    require(bool(kernel.K32EnumProcessModules(kernel.GetCurrentProcess(),modules,ctypes.sizeof(modules),ctypes.byref(needed))),
            "native input module enumeration failed")
    require(0<needed.value<ctypes.sizeof(modules) and needed.value%ctypes.sizeof(ctypes.c_void_p)==0,
            "native input module enumeration is incomplete")
    system=PureWindowsPath(context.system_root)/"System32"
    packaged={PurePosixPath(item.path).name.casefold() for item in source.manifest.content.files
              if item.path.lower().endswith((".dll",".pyd"))}
    for handle in modules[:needed.value//ctypes.sizeof(ctypes.c_void_p)]:
        name=ctypes.create_unicode_buffer(32768)
        count=kernel.GetModuleFileNameW(handle,name,len(name))
        require(0<count<len(name),"native input module name is incomplete")
        path=PureWindowsPath(name.value)
        require(path.is_relative_to(root) or path.is_relative_to(system) and path.name.casefold() not in packaged,
                "native module loaded outside its qualified runtime or system role")


def materialize(host, source, processes, request):
    """Consume persisted CREATED admission; keep every owner until worker exit.

    The production entry supplies a canonically attached Host. A test may use
    the internal object-relative fixture, without adding a locator override to
    the production protocol.
    """
    require(type(host) is WindowsHostOwner and type(source) is PinnedRuntime
            and type(processes) is WindowsProcessLibrary and type(request) is RuntimeInstallRequest,
            "exact owned runtime worker inputs required")
    host.require_input(source)
    require(any(child is source for _,child in host._resources.values()),"input pins must belong to this Host owner")
    anchor=host.anchor
    control=RuntimeControl(host)
    control._used=True
    host._ready=False
    observer=host._new(WindowsStorageProcess,processes)
    context=observer.current()
    require_input_execution(source,context)
    plan=RuntimeInstallPlan.for_host(anchor,request.slot,request.generation)
    require((request.host,request.source,request.plan)==(anchor.binding,plan.source_manifest,plan.sha256),
            "runtime worker request changed its Host or input")
    prior=control._read();value=prior.head.state.value();entry=value["entries"][plan.slot]
    plan.require_entry(entry)
    worker=entry["worker"]
    require(entry["phase"]=="RESERVED" and worker is not None and worker["phase"]=="CREATED"
            and worker["mode"]=="install" and worker["launch"]==request.launch
            and (worker["pid"],worker["created"])==(context.identity.pid,context.identity.creation_time),
            "runtime worker lacks its exact persisted native launch")
    control._idle(value,except_slot=plan.slot)
    control._namespace(value)
    entry["worker"]=dict(worker,phase="ACCEPTED")
    control._update(prior,value)
    installer=host._new(RuntimeInstaller,host._registry.trees,TreePurpose.INSTALL,anchor.account.sid)
    installer.begin(host._members["runtimes"],dict(anchor.members)["runtimes"],plan.generation)
    root_identity=installer.identity(0)
    prior=control._read();value=prior.head.state.value();entry=value["entries"][plan.slot]
    entry.update(phase="BUILDING",root=asdict(root_identity));control._update(prior,value)
    positions={"":0}
    for name in source.manifest.content.directories:
        path=PurePosixPath(name);parent=str(path.parent)
        positions[name]=installer.add(positions["" if parent=="." else parent],path.name,directory=True)
    for member in source.manifest.content.files:
        path=PurePosixPath(member.path);parent=str(path.parent)
        index=installer.add(positions["" if parent=="." else parent],path.name,length=member.length)
        for offset in range(0,member.length,65536):
            installer.write(index,source.read(member.path,offset,min(65536,member.length-offset)))
        installer.seal(index)
        require(installer.digest(index)==member.sha256,"installed runtime member digest changed")
    target=plan.target_manifest(anchor,root_identity)
    raw=target.to_bytes();index=installer.add(0,MANIFEST_NAME,length=len(raw))
    installer.write(index,raw);installer.seal(index)
    require(installer.digest(index)==target.sha256,"installed runtime manifest digest changed")
    installer.finish()
    prior=control._read();value=prior.head.state.value();entry=value["entries"][plan.slot]
    entry.update(phase="MATERIALIZED",manifest=target.sha256);control._update(prior,value)
    return target


def attach_input(request):
    """Resolve only the real native account and the actual input executable."""
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.GetModuleFileNameW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_uint32]
    kernel.GetModuleFileNameW.restype=ctypes.c_uint32
    image=ctypes.create_unicode_buffer(32768)
    count=kernel.GetModuleFileNameW(None,image,len(image))
    require(0<count<len(image),"actual worker image is unavailable")
    root=Path(image.value).parent
    def library(name, kind):
        # Resolve dependencies only beside the selected absolute DLL or in the
        # system directory. No PATH lookup selects a native storage module.
        return kind(ctypes.CDLL(str(root/("gitgo-storage-"+name+".dll")),winmode=0x100|0x800))
    scopes=library("scope",WindowsScopeLibrary)
    trees=library("tree",WindowsTreeLibrary)
    journals=library("journal",WindowsJournalLibrary)
    processes=library("process",WindowsProcessLibrary)
    preliminary=scopes.open_scope()
    try:
        account=HostAccount(*preliminary.account())
        input_root=preliminary.root(str(root));preliminary.volume(input_root)
        manifest_file=preliminary.open(MANIFEST_NAME,input_root)
        raw=preliminary.read(manifest_file,65536)
        manifest=RuntimeManifest.from_bytes(raw,request.source)
        require(PureWindowsPath(manifest.root)==PureWindowsPath(root)
                and manifest.root_identity==preliminary.identity(input_root)[0],"input executable root changed")
        host_root=preliminary.root(account.root)
        envelope_file=preliminary.open("envelope.json",host_root)
        envelope=StorageEnvelope.from_bytes(preliminary.read(envelope_file,8192))
        registry=WindowsHostRegistry(scopes,trees,journals,HostLayout(envelope,manifest.content))
        host=registry.owner()
        anchor=host.attach()
        require(anchor.binding==request.host and anchor.source.manifest_digest==manifest.sha256,
                "input request is not approved by the canonical Host authority")
    finally:
        preliminary.close()
    source=host._new(PinnedRuntime,trees,raw,anchor.source.manifest_digest,account.sid)
    source.pin()
    host.require_input(source)
    return host,source,processes


def main():
    # Every error remains diagnostic. The parent uses the journal, actual Job
    # retirement and independent file verification, never this process's text.
    try:
        require(len(sys.argv)==2 and getattr(getattr(sys.modules["__main__"],"__spec__",None),"name",None)
                =="backend.core.storage.runtime_worker","closed runtime worker entry required")
        request=RuntimeInstallRequest.from_bytes(sys.argv[1].encode("ascii"))
        host,source,processes=attach_input(request)
        materialize(host,source,processes,request)
        os.write(1,b'{"phase":"MATERIALIZED","authority":"diagnostic"}\n')
        require(sys.stdin.buffer.read(1)==b"R","runtime worker release frame is invalid")
    except BaseException:
        try: os.write(2,b"Runtime installer stopped; retained journal debt requires reconciliation.\n")
        finally: os._exit(1)
    os._exit(0)


if __name__=="__main__":
    main()
