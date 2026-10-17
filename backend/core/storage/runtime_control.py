"""Owned runtime admission in the existing canonical Host authority.

Immutable plans identify work. Only a confirmed bounded native journal update
can issue a live ticket; neither a plan nor a phase string approves execution.
"""
from dataclasses import dataclass
import hashlib
from pathlib import PureWindowsPath
import uuid

from . import runtime_records, journal_cells
from .host_records import HostAnchor, require, guid
from .native_windows_host import WindowsHostOwner
from .native_windows_tree import RuntimePins, RuntimeAbsence, TreePurpose
from backend.core.sandbox_read import ObjectIdentity
from .runtime_manifest import RuntimeManifest, canonical, digest


@dataclass(frozen=True)
class RuntimeInstallPlan:
    host_binding: str
    slot: int
    generation: str
    source_manifest: str
    content_contract: str

    def __post_init__(self):
        digest(self.host_binding);digest(self.source_manifest);digest(self.content_contract)
        guid(self.generation)
        require(type(self.slot) is int and self.slot in (0,1),"one prepaid runtime slot required")

    @classmethod
    def for_host(cls, anchor, slot, generation):
        require(type(anchor) is HostAnchor,"exact Host authority required")
        return cls(anchor.binding,slot,generation,anchor.source.manifest_digest,anchor.layout.content.contract)

    def require_host(self, anchor):
        require(type(anchor) is HostAnchor and self==self.for_host(anchor,self.slot,self.generation),
                "runtime installation plan belongs to a different Host or input")

    def to_bytes(self):
        return canonical(dict(version=1,host_binding=self.host_binding,slot=self.slot,generation=self.generation,
                              source_manifest=self.source_manifest,content_contract=self.content_contract))

    @property
    def sha256(self):
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def initial_entry(self):
        return dict(generation=self.generation,source_manifest=self.source_manifest,plan=self.sha256,
                    phase="RESERVED",root=None,manifest=None,worker=None)

    def require_entry(self, entry):
        require(type(entry) is dict and all(entry.get(name)==value for name,value in (
            ("generation",self.generation),("source_manifest",self.source_manifest),("plan",self.sha256))),
            "runtime generation or installation plan changed")

    def target_manifest(self, anchor, root_identity):
        self.require_host(anchor)
        result=RuntimeManifest(str(PureWindowsPath(anchor.account.root)/"runtimes"/self.generation),
                               root_identity,anchor.layout.content)
        require(root_identity.volume==anchor.root_identity.volume,"runtime target crossed the Host volume")
        require(root_identity not in (anchor.parent_identity,anchor.root_identity,anchor.source.root_identity,
                *[identity for _,identity in anchor.members]),"runtime target aliases a retained Host object")
        return result


def require_whole_tail(view):
    """Reserve headroom for installation, activation, failure and retirement."""
    require(view.head is not None and view.head.sequence<=journal_cells.MAX_SEQUENCE-32,
            "complete runtime lifecycle journal capacity required")
    sample=dict(generation="ffffffff-ffff-ffff-ffff-ffffffffffff",source_manifest="f"*64,plan="f"*64,
        phase="MATERIALIZED",root=dict(platform="win32",volume=2**64-1,file_id=2**128-1),manifest="f"*64,
        worker=dict(launch="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",mode="cleanup",phase="ACCEPTED",
                    pid=2**32-2,created=2**64-1,parent_pid=2**32-1,parent_created=2**64-1))
    other=dict(sample,generation="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    cases=[dict(binding="f"*64,epoch=2**63-1,active=None,entries=[sample,other]),
           dict(binding="f"*64,epoch=2**63-1,active=sample["generation"],entries=[
               dict(sample,phase="ACTIVE",worker=dict(sample["worker"],phase="RETIRED")),other])]
    for case in cases:
        encoded=canonical(case)
        require(len(encoded)<=journal_cells.PAYLOAD_BYTES,"complete runtime lifecycle payload capacity required")
        runtime_records.RuntimeRegistryState(encoded)


@dataclass(frozen=True)
class RuntimeReservation:
    plan: RuntimeInstallPlan


class RuntimeControl:
    def __init__(self, host):
        require(type(host) is WindowsHostOwner,"owned canonical Host admission required")
        self._anchor=host.anchor
        require(getattr(host,"_runtime_control",None) is None,"one runtime coordinator per Host owner")
        self._host=host
        self._used=False
        self._ticket=None
        host._runtime_control=self

    def _read(self):
        view=self._host._read_slot(63)
        require(view.head is not None and view.head.state.value()["binding"]==self._anchor.binding,
                "runtime journal belongs to this exact Host authority")
        return view

    def _idle(self, value, *, except_slot=None):
        for slot,entry in enumerate(value["entries"]):
            if slot!=except_slot and entry is not None and entry["worker"] is not None:
                require(entry["worker"]["phase"]=="RETIRED","another runtime worker retains debt")
        for slot in range(self._anchor.layout.envelope.slots):
            view=self._host._read_slot(slot)
            if view.head is not None:
                event=view.head.state.value()["event"]
                require(event is None or event["phase"]=="RETIRED","project worker must retire before runtime installation")

    def _namespace(self, value):
        host=self._host
        parent=host._members["runtimes"]
        expected=dict(self._anchor.members)["runtimes"]
        pins=host._new(RuntimePins,host._registry.trees,TreePurpose.PIN,self._anchor.account.sid)
        pins.root(parent,expected)
        names=pins.inventory(0)
        entries={entry["generation"]:entry for entry in value["entries"] if entry is not None}
        require(len(names)==len(set(names)) and set(names)<=set(entries),
                "unregistered runtime members block admission")
        for name,entry in entries.items():
            present=name in names
            if entry["phase"]=="ABSENT":
                require(not present,"retired runtime name is present again")
            elif entry["root"] is not None and entry["phase"]!="RETIRING":
                require(present,"registered runtime root is missing")
            if present:
                worker=entry["worker"]
                require(entry["root"] is not None or worker is not None and worker["phase"] in {"ACCEPTED","RETIRED"},
                        "runtime root appeared before accepted creation intent")
                handle=host._scope.open(name,parent,directory=True)
                actual=host._scope.identity(handle)[0]
                require(entry["root"] is None or actual==ObjectIdentity(**entry["root"]),"runtime root was replaced")
                # Validation must not pin the old target against its separately
                # admitted cleanup worker. Later operations rebind exact IDs.
                host._scope.close_handle(handle)
        host._release(pins)

    def _require_absence(self, generation):
        host=self._host
        absence=host._new(RuntimeAbsence,host._registry.trees,TreePurpose.ABSENCE,self._anchor.account.sid)
        absent=absence.observe(host._members["runtimes"],dict(self._anchor.members)["runtimes"],generation)
        require(absent,"fresh runtime generation already exists")
        host._release(absence)

    def _update(self, prior, value):
        state=runtime_records.RuntimeRegistryState(canonical(value))
        journal=self._host._journal(63)
        result=journal.update(prior,state)
        require(result.head is not None and result.head.state==state,"runtime admission commit is unconfirmed")
        self._host._release(journal)
        return result

    def reserve(self, source):
        host=self._host
        require(not self._used,"runtime reservation attempt is already consumed")
        host.require_input(source)
        self._used=True
        host._ready=False
        prior=self._read()
        require_whole_tail(prior)
        value=prior.head.state.value()
        self._idle(value)
        self._namespace(value)
        slot=next((index for index,entry in enumerate(value["entries"]) if entry is None),None)
        require(slot is not None,"both prepaid runtime copies remain charged")
        plan=RuntimeInstallPlan.for_host(self._anchor,slot,str(uuid.uuid4()))
        require(all(entry is None or entry["generation"]!=plan.generation for entry in value["entries"]),
                "runtime generation collision")
        self._require_absence(plan.generation)
        value["entries"][slot]=plan.initial_entry()
        self._update(prior,value)
        ticket=RuntimeReservation(plan)
        self._ticket=ticket
        host._ready=True
        return ticket

    def require_ticket(self, ticket):
        require(self._host.anchor==self._anchor and type(ticket) is RuntimeReservation and self._ticket is ticket,
                "exact live runtime capacity ticket required")
        ticket.plan.require_host(self._anchor)

