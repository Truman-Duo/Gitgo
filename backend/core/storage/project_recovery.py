"""Evidenced project worker retirement after its original parent has ended.

Only native process facts settle a lost coordinator. Missing child identity is
unknown. Recovery never reruns initialization or completes partial migrations.
"""
from dataclasses import replace

from .host_records import require
from .native_windows_process import WindowsProcessLibrary,WindowsStorageProcess,StorageProcessIdentity
from .project_control import ProjectControl
from .project_records import ProjectRecord
from .project_profile import ProjectProfile
from .project_namespace import ProjectNamespace
from .runtime_control import RuntimeControl
from .runtime_lifecycle import claim,entry_plan,verify_target


def recover(control,slot,processes):
    require(type(control) is ProjectControl and not control._used and type(processes) is WindowsProcessLibrary,
            "fresh project control and trusted native process observations required")
    control._used=True;host=control.host
    prior=control.read(slot);state=prior.head.state;value=state.value();event=value["event"]
    require(event is not None and event["phase"] in {"CREATED","ACCEPTED"} and event["pid"]>0,
            "unknown native creation cannot be replayed or inferred from a project directory")
    require(event["mode"] in {"initialize","verify_initialize"} and event["generation"]==0,
            "closed project initialization recovery required")
    runtime_control=RuntimeControl(host)
    _,runtime=claim(runtime_control,exclusive=False)
    control._lease(slot)
    observers=[]
    for pid,created in ((event["parent_pid"],event["parent_created"]),(event["pid"],event["created"])):
        observer=host._new(WindowsStorageProcess,processes);observers.append(observer)
        require(observer.ended(StorageProcessIdentity(pid,created)),"exact project owner process is still alive")
    for observer in observers:
        require(observer.retire(terminate=False),"project recovery observation closure is incomplete")
        require(host._release(observer),"project recovery observer remains retained")
    require(all(entry is None or entry["worker"] is None or entry["worker"]["phase"]=="RETIRED" for entry in runtime["entries"]),
            "runtime worker retains debt during project recovery")
    runtime_control._namespace(runtime)
    index=next((i for i,e in enumerate(runtime["entries"]) if e is not None and e["generation"]==runtime["active"]),None)
    require(index is not None,"registered selected runtime required for project recovery")
    entry,plan=entry_plan(runtime_control,runtime,index)
    require(entry["phase"]=="ACTIVE" and event["runtime"]==dict(generation=entry["generation"],manifest=entry["manifest"],epoch=runtime["epoch"]),
            "project recovery runtime identity changed")
    pins=verify_target(runtime_control,entry,plan)
    profile=ProjectProfile.from_pins(pins);profile.require_host(control.anchor)
    require(event["profile"]==profile.sha256 and value["reservation"]["body"]["fingerprint"]==profile.sha256,
            "project recovery profile changed")
    record=None if state.project_bytes is None else ProjectRecord.from_bytes(state.project_bytes)
    after=state.with_event(dict(event,phase="RETIRED"))
    if record is not None and record.phase=="VERIFIED":
        require(event["mode"]=="verify_initialize" and event["phase"]=="ACCEPTED",
                "independent committed verification fact required")
        control.namespace()
        host._ready=True
        try:
            namespace=ProjectNamespace(host,record,value["reservation"]["body"]["project"],profile.limits).pin()
            namespace.require_empty_cas();namespace.verify()
        finally:host._ready=False
        record=replace(record,phase="READY");after=after.with_project(record)
    # A missing root identity still remains absent from this record even when
    # both processes are known dead. It cannot become a later creation ticket.
    control.update(prior,after);host._ready=True
    return "UNKNOWN" if record is None else record.phase
