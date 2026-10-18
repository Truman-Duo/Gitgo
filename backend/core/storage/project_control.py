"""Project worker admission through the existing Host and runtime journals."""
from dataclasses import asdict,dataclass
import uuid

from .host_records import guid,require
from .journal_cells import canonical,decode,MAX_SEQUENCE
from .native_windows_host import WindowsHostOwner
from .native_windows_tree import DatabasePins,TreePurpose
from .native_windows_process import WindowsProcessLibrary,WindowsStorageProcess
from .project_records import ProjectRecord
from .project_profile import ProjectProfile
from .runtime_control import RuntimeControl
from .runtime_lifecycle import SelectedRuntime,RuntimeReference,claim,entry_plan,verify_target
from .runtime_manifest import digest
from .runtime_worker import require_input_execution


@dataclass(frozen=True)
class ProjectRequest:
    mode: str
    host: str
    slot: int
    project: str
    profile: str
    request: str
    launch: str
    runtime: RuntimeReference

    def __post_init__(self):
        require(self.mode in {"initialize","verify_initialize"},"closed project worker operation")
        digest(self.host);digest(self.profile)
        guid(self.project);guid(self.request);guid(self.launch)
        require(type(self.slot) is int and 0<=self.slot<11 and type(self.runtime) is RuntimeReference,
                "bounded project slot and exact selected runtime required")

    def to_bytes(self):return canonical(dict(version=1,**asdict(self)))

    @classmethod
    def from_bytes(cls,raw):
        value=decode(raw)
        require(type(value) is dict and set(value)=={"version","mode","host","slot","project","profile","request","launch","runtime"}
                and type(value["version"]) is int and value["version"]==1,"closed project request")
        reference=value["runtime"]
        require(type(reference) is dict and set(reference)=={"generation","manifest","epoch"},"closed runtime reference")
        result=cls(**{k:v for k,v in value.items() if k not in {"version","runtime"}},runtime=RuntimeReference(**reference))
        require(result.to_bytes()==raw,"canonical project request required")
        return result

    def event(self,context):
        return dict(phase="INTENT",launch=self.launch,pid=0,created=0,
            parent_pid=context.identity.pid,parent_created=context.identity.creation_time,
            profile=self.profile,mode=self.mode,request=self.request,generation=0,runtime=asdict(self.runtime))


class ProjectControl:
    def __init__(self,host):
        require(type(host) is WindowsHostOwner,"canonical owned Host required")
        self.anchor=host.anchor;self.host=host
        require(getattr(host,"_project_control",None) is None,"one project coordinator per Host owner")
        host._project_control=self
        self.selected=self.profile=self.request=self.ticket=None
        self._used=self._accepted=False

    def read(self,slot):
        require(type(slot) is int and 0<=slot<self.anchor.layout.envelope.slots,"exact prepaid project slot")
        view=self.host._read_slot(slot)
        require(view.head is not None,"persisted project reservation required")
        self.host._reservation(view,slot)
        return view

    def update(self,prior,state):
        slot=state.value()["reservation"]["body"]["slot"]
        journal=self.host._journal(slot)
        result=journal.update(prior,state)
        require(result.head is not None and result.head.state==state,"project journal commit is unconfirmed")
        self.host._release(journal)
        return result

    def namespace(self):
        host=self.host;anchor=self.anchor
        pins=host._new(DatabasePins,host._registry.trees,TreePurpose.DATA,anchor.account.sid)
        pins.root(host._members["candidates"],dict(anchor.members)["candidates"])
        names=pins.inventory(0);remaining=set(names)
        require(len(names)==len(remaining),"duplicate project directory names")
        for slot in range(anchor.layout.envelope.slots):
            view=host._read_slot(slot)
            if view.head is None:continue
            value=view.head.state.value();reservation=host._reservation(view,slot);name=reservation["project"]
            raw=view.head.state.project_bytes
            if raw is None:
                require(name not in remaining,"present project root lacks recorded native creation identity")
                continue
            record=ProjectRecord.from_bytes(raw);record.identity.require_host(anchor)
            require(record.profile==reservation["fingerprint"],"project record and reservation profile differ")
            require(name in remaining,"recorded project root is missing")
            pins.add(0,name,record.identity.root,directory=True)
            remaining.remove(name)
        require(not remaining,"unregistered project roots block admission")
        require(host._release(pins),"project namespace observation closure is incomplete")

    def _lease(self,slot):
        name=f"slot-{slot:04d}.lease"
        self.host._scope.lock(self.host._members[name],dict(self.anchor.members)[name],exclusive=True)

    def reserve(self,selected,project, *, verify_slot=None):
        require(not self._used and type(selected) is SelectedRuntime and selected._control._host is self.host,
                "one project operation under the same selected runtime owner required")
        selected.require_live();guid(project)
        self._used=True
        profile=ProjectProfile.from_pins(selected._pins);profile.require_host(self.anchor)
        self.namespace()
        if verify_slot is None:
            ticket=self.host.reserve_project(project,profile.sha256)
            prior=self.read(ticket.slot)
            require(prior.head.state.event_bytes is None and prior.head.state.project_bytes is None,"fresh project reservation required")
            slot=ticket.slot;mode="initialize";self.ticket=ticket
        else:
            prior=self.read(verify_slot);value=prior.head.state.value()
            require(value["reservation"]["body"]["project"]==project and value["event"] is not None
                    and value["event"]["phase"]=="RETIRED","original initializer must retire before independent verification")
            record=ProjectRecord.from_bytes(prior.head.state.project_bytes)
            require(record.profile==profile.sha256 and record.phase in {"STRUCTURED","MATERIALIZED"},
                    "only recorded complete structures may undergo committed-fact verification")
            slot=verify_slot;mode="verify_initialize"
        # Includes intent, identity, acceptance, all construction facts, retired
        # observations and the separate verifier. No sequence reset on retry.
        require(prior.head.sequence<=MAX_SEQUENCE-24,"complete initialization journal tail required")
        self._lease(slot)
        self.selected,self.profile=selected,profile
        self.request=ProjectRequest(mode,self.anchor.binding,slot,project,profile.sha256,
                                    str(uuid.uuid4()),str(uuid.uuid4()),selected.reference)
        return self.request

    def accept(self,request,processes):
        require(not self._used and type(request) is ProjectRequest and type(processes) is WindowsProcessLibrary,
                "one exact native project acceptance required")
        self._used=True
        observer=self.host._new(WindowsStorageProcess,processes)
        context=observer.current()
        require(request.host==self.anchor.binding,"project worker Host changed")
        prior=self.read(request.slot);value=prior.head.state.value();event=value["event"]
        body=value["reservation"]["body"]
        require(body["project"]==request.project and body["fingerprint"]==request.profile,
                "project worker reservation changed")
        require(event is not None and event["phase"]=="CREATED" and all(event[key]==value for key,value in
                (("launch",request.launch),("profile",request.profile),("mode",request.mode),("request",request.request),
                 ("generation",0),("runtime",asdict(request.runtime)),("pid",context.identity.pid),("created",context.identity.creation_time))),
                "worker lacks its exact persisted native process and request")
        if request.mode=="initialize":require(prior.head.state.project_bytes is None,"initializer may not replay existing construction")
        else:
            record=ProjectRecord.from_bytes(prior.head.state.project_bytes)
            require(record.phase in {"STRUCTURED","MATERIALIZED"},"closed independent verification input required")
        control=RuntimeControl(self.host)
        runtime_view,runtime=claim(control,exclusive=False)
        for entry in runtime["entries"]:
            require(entry is None or entry["worker"] is None or entry["worker"]["phase"]=="RETIRED",
                    "runtime worker retains debt")
        for slot in range(self.anchor.layout.envelope.slots):
            if slot==request.slot:continue
            other=self.host._read_slot(slot)
            if other.head is not None:
                worker=other.head.state.value()["event"]
                require(worker is None or worker["phase"]=="RETIRED","another project worker retains debt")
        control._namespace(runtime)
        index=next((i for i,e in enumerate(runtime["entries"]) if e is not None and e["generation"]==runtime["active"]),None)
        require(index is not None,"active selected runtime required")
        entry,plan=entry_plan(control,runtime,index)
        require(entry["phase"]=="ACTIVE" and request.runtime==RuntimeReference(entry["generation"],entry["manifest"],runtime["epoch"]),
                "project worker runtime epoch or generation changed")
        pins=verify_target(control,entry,plan)
        require_input_execution(pins,context)
        profile=ProjectProfile.from_pins(pins);profile.require_host(self.anchor)
        require(profile.sha256==request.profile,"project worker storage profile changed")
        self.namespace();self._lease(request.slot)
        self.host._scope.flush_bound(self.host._root,"slot-0063.json",dict(self.anchor.members)["slot-0063.json"],8192)
        require(control._read().raw==runtime_view.raw,"selected runtime authority changed")
        selected=SelectedRuntime(control,pins,request.runtime);control._selected=selected
        self.selected,self.profile,self.request=selected,profile,request
        self.update(prior,prior.head.state.with_event(dict(event,phase="ACCEPTED")))
        self._accepted=True;self.host._ready=True

    def record(self,record):
        require(self._accepted and type(record) is ProjectRecord,"actual accepted worker required for construction fact")
        self.selected.require_live(self.request.runtime)
        prior=self.read(self.request.slot);event=prior.head.state.value()["event"]
        require(event["phase"]=="ACCEPTED" and event["launch"]==self.request.launch,"project worker changed")
        self.update(prior,prior.head.state.with_project(record))
