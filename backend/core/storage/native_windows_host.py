"""Owned Windows Host bootstrap and prepaid project-slot admission.

Construction requires trusted loaded libraries and an already qualified layout.
The public locator uses the real account profile; state_home and environment
variables cannot create another budget authority. No default activation exists.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import threading
import uuid

from . import runtime_records, slot_records
from .host_records import (HOST_LEAF, HostAccount, HostAnchor, HostInput, HostLayout, bootstrap_intent,
                           guid, require)
from .native_windows_scope import WindowsScopeLibrary, WindowsStorageScope
from .native_windows_tree import (WindowsTreeLibrary, MetadataBootstrap, RuntimePins, TreePurpose)
from .native_windows_journal import WindowsJournalLibrary, WindowsStorageJournal
from .runtime_installation import PinnedRuntime
from .native_windows_process import WindowsStorageProcess
from .runtime_manifest import canonical, digest

_LIVE_HOSTS = {}


class WindowsHostRegistry:
    def __init__(self, scopes: WindowsScopeLibrary, trees: WindowsTreeLibrary,
                 journals: WindowsJournalLibrary, layout: HostLayout):
        require(type(scopes) is WindowsScopeLibrary and type(trees) is WindowsTreeLibrary
                and type(journals) is WindowsJournalLibrary and type(layout) is HostLayout,
                "explicit trusted native Host dependencies required")
        self.scopes, self.trees, self.journals, self.layout = scopes, trees, journals, layout
        self._owners = {}

    def owner(self):
        return WindowsHostOwner(self)


@dataclass(frozen=True)
class ProjectReservation:
    slot: int
    project: str
    fingerprint: str
    record: bytes


class WindowsHostOwner:
    def __init__(self, registry):
        require(type(registry) is WindowsHostRegistry and len(_LIVE_HOSTS)<4, "native Host owner capacity")
        self._pid, self._thread = os.getpid(),threading.get_ident()
        self._registry = registry
        self._resources = {}
        self._attempted = self._ready = self._closed = False
        self._anchor = None
        self._gate = None
        self._gate_held = False
        self._source = None
        self._tickets = {}
        registry._owners[id(self)] = self
        _LIVE_HOSTS[id(self)] = self
        self._scope = self._new(WindowsStorageScope, registry.scopes)

    def _require_identity(self):
        require(self._pid==os.getpid() and self._thread==threading.get_ident(),"Host owner process/thread changed")

    def _new(self, kind, library, *arguments):
        self._require_identity()
        require(not self._closed and len(self._resources)<16, "bounded native Host child resources required")
        # Keep the child object before its constructor can reserve a native slot.
        # A lost constructor return therefore cannot sever the parent's close order.
        child = object.__new__(kind)
        self._resources[id(child)] = (library,child)
        kind.__init__(child,library,*arguments)
        return child

    def _release(self, child):
        library,child = self._resources[id(child)]
        cookie = getattr(child,"_cookie",None)
        if type(child) is PinnedRuntime:
            child.close()
        elif id(child) in library._owners or cookie is not None and cookie.value:
            if type(child) is WindowsStorageProcess:
                if not child.retire(terminate=True):
                    return False
            else:
                child.close()
        # Before registration/zero native cookie means the constructor never
        # entered a native effect. A close failure never reaches this removal.
        self._resources.pop(id(child))
        return True

    def _pins(self, sid, parent, identity):
        pins = self._new(RuntimePins,self._registry.trees,TreePurpose.PIN,sid)
        pins.root(parent,identity)
        return pins

    @property
    def anchor(self):
        self._require_identity()
        require(self._ready and not self._closed,"live Host admission required")
        return self._anchor

    def _parent(self, account, *, create, exclude=None):
        profile = self._scope.root(account.profile,exclude=exclude)
        self._scope.volume(profile)
        proof = self._new(MetadataBootstrap,self._registry.trees,TreePurpose.BOOTSTRAP,account.sid)
        proof.check_parent(profile,self._scope.identity(profile)[0])
        self._release(proof)
        try:
            parent = self._scope.open(".gitgo",profile,directory=True)
        except FileNotFoundError:
            require(create,"canonical Host parent is absent")
            creator = self._new(MetadataBootstrap,self._registry.trees,TreePurpose.BOOTSTRAP,account.sid)
            creator.begin(profile,self._scope.identity(profile)[0],".gitgo")
            expected = creator.identity(0)
            creator.finish()
            self._release(creator)
            parent = self._scope.open(".gitgo",profile,directory=True)
            require(self._scope.identity(parent)[0]==expected,"canonical Host parent changed after creation")
        parent_id = self._scope.identity(parent)[0]
        require(exclude is None or parent_id!=exclude,"input copy aliases the Host parent")
        self._scope.volume(parent)
        proof = self._pins(account.sid,parent,parent_id)
        self._release(proof)
        return parent,parent_id

    def bootstrap(self, source: PinnedRuntime):
        self._require_identity()
        require(not self._attempted and not self._closed,"one canonical Host bootstrap attempt")
        self._attempted = True
        require(type(source) is PinnedRuntime and source.ready and source.manifest.content==self._registry.layout.content,
                "independently approved and completely pinned input runtime required")
        self._source = source
        account = HostAccount(*self._scope.account())
        descriptor = HostInput.from_manifest(source.manifest)
        descriptor.require_disjoint(account.root)
        parent,parent_id = self._parent(account,create=True,exclude=descriptor.root_identity)
        return self._bootstrap_at(account,parent,parent_id,descriptor)

    def _bootstrap_at(self, account, parent, parent_id, source):
        """Internal object-relative installation; the public entry fixes location."""
        layout = self._registry.layout
        require(type(source) is HostInput,"exact charged input copy required")
        source.require_content(layout.content)
        source.require_disjoint(account.root)
        creator = self._new(MetadataBootstrap,self._registry.trees,TreePurpose.BOOTSTRAP,account.sid)
        creator.begin(parent,parent_id,HOST_LEAF)
        root_id = creator.identity(0)
        registry_id = str(uuid.uuid4())
        indices = {}
        for member in layout.members:
            index = creator.add(0,member.name,directory=member.directory,ceiling=member.ceiling)
            indices[member.name] = index
            if member.name == "bootstrap.intent":
                creator.write(index,bootstrap_intent(registry_id,account,layout,parent_id,root_id,source))
            if member.name == "storage-maintenance.lock":
                creator.write(index,b"")
                creator.lock_gate(index)
        anchor = HostAnchor(registry_id,account,parent_id,root_id,layout,source,
            tuple(sorted((name,creator.identity(index)) for name,index in indices.items())))
        runtime = runtime_records.RuntimeRegistryState(canonical(dict(binding=anchor.binding,epoch=0,active=None,entries=[None,None])))
        frame = runtime_records.FORMAT.next_update(runtime_records.FORMAT.read(bytes(8192)),runtime)
        encoded = bytearray(8192)
        encoded[frame.target*4096:(frame.target+1)*4096] = frame.encoded
        for member in layout.members:
            if member.directory or member.name in {"bootstrap.intent","storage-maintenance.lock","authority.json"}:
                continue
            if member.name == "envelope.json": raw = layout.envelope.to_bytes()
            elif member.name == "slot-0063.json": raw = bytes(encoded)
            elif member.name.endswith(".json"): raw = bytes(8192)
            else: raw = b""
            creator.write(indices[member.name],raw)
        creator.write(indices["authority.json"],anchor.to_bytes())
        creator.finish()
        self._release(creator)
        root = self._scope.open(HOST_LEAF,parent,directory=True)
        self._verify(account,parent_id,root,expected=anchor)
        return self.anchor

    def attach(self):
        self._require_identity()
        require(not self._attempted and not self._closed,"one canonical Host attach attempt")
        self._attempted = True
        account = HostAccount(*self._scope.account())
        parent,parent_id = self._parent(account,create=False)
        root = self._scope.open(HOST_LEAF,parent,directory=True)
        self._verify(account,parent_id,root)
        return self.anchor

    def _verify(self, account, parent_id, root, *, expected=None):
        self._root, self._root_id = root,self._scope.identity(root)[0]
        self._scope.volume(root)
        pins = self._pins(account.sid,root,self._root_id)
        gate = self._scope.open("storage-maintenance.lock",root)
        self._scope.lock(gate,self._scope.identity(gate)[0])
        self._gate,self._gate_held = gate,True
        authority = self._scope.open("authority.json",root)
        raw = self._scope.read(authority,65536)
        anchor = HostAnchor.from_bytes(raw,account,self._registry.layout)
        require(anchor.root_identity==self._root_id and anchor.parent_identity==parent_id,"Host root/parent identity changed")
        require(expected is None or anchor==expected,"fresh Host authority readback changed")
        self._anchor = anchor
        self._members = {}
        identities = dict(anchor.members)
        mutable = self._pins(account.sid,root,self._root_id)
        for member in anchor.layout.members:
            handle = self._scope.open(member.name,root,directory=member.directory)
            require(self._scope.identity(handle)[0]==identities[member.name],"Host member was replaced")
            self._members[member.name] = handle
            if not member.mutable:
                contents = self._scope.read(handle,member.ceiling)
                wanted = {"authority.json":raw,"bootstrap.intent":anchor.intent(),
                          "envelope.json":anchor.layout.envelope.to_bytes()}[member.name]
                require(contents==wanted,"immutable Host metadata changed")
                index = pins.add(0,member.name,length=len(contents))
                require(pins.digest(index)==hashlib.sha256(wanted).hexdigest(),"Host metadata changed during pinning")
            else:
                mutable.add(0,member.name,directory=member.directory,length=member.ceiling)
        self._release(mutable)
        runtime = self._read_slot(63)
        require(runtime.head is not None and runtime.head.state.value()["binding"]==anchor.binding,
                "runtime registry binding is incomplete")
        projects = set()
        for slot in range(anchor.layout.envelope.slots):
            view = self._read_slot(slot)
            if view.head is not None:
                reservation = self._reservation(view,slot)
                require(reservation["project"] not in projects,"duplicate project reservation")
                projects.add(reservation["project"])
        inventory = pins.inventory(0)
        observed = set(inventory)
        require(len(inventory)==len(observed),"duplicate Host directory entries")
        fixed = {member.name for member in anchor.layout.members}
        require(observed==fixed,"unqualified Host registry member; project binding required")
        self._ready = True

    def _require_gate(self):
        self._require_identity()
        require(self._gate_held and not self._closed,"exclusive Host gate required")

    def handoff_gate(self):
        """End admission before releasing the lock; unknown release cannot resume."""
        self._require_gate()
        require(self._ready,"complete Host verification required for handoff")
        self._ready = self._gate_held = False
        self._scope.close_handle(self._gate)

    def _journal(self, index):
        self._require_gate()
        record_format = runtime_records.FORMAT if index==63 else slot_records.FORMAT
        journal = self._new(WindowsStorageJournal,self._registry.journals,record_format)
        journal.open(self._root,index,self._root_id,dict(self._anchor.members)[f"slot-{index:04d}.json"])
        return journal

    def _read_slot(self, index):
        journal = self._journal(index)
        view = journal.read()
        self._release(journal)
        return view

    def _reservation(self, view, index):
        value = view.head.state.value()["reservation"]["body"]
        require(value["registry_id"]==self._anchor.registry_id and value["envelope"]==self._anchor.layout.envelope.digest
                and value["slot"]==index,"project reservation belongs to a different Host envelope")
        return value

    def require_input(self, source):
        """Accept only the previously charged copy, with complete live native pins."""
        self._require_identity()
        require(self._ready and not self._closed,"live locked Host admission required")
        require(type(source) is PinnedRuntime and source.ready
                and HostInput.from_manifest(source.manifest)==self._anchor.source,
                "runtime input changed or is not completely pinned; no second input allowance")

    def reserve_project(self, project, fingerprint):
        self._require_identity()
        require(self._ready and not self._closed,"live locked Host admission required")
        guid(project);digest(fingerprint)
        self._ready = False
        chosen = None
        for slot in range(self._anchor.layout.envelope.slots):
            view = self._read_slot(slot)
            if view.head is None:
                if chosen is None: chosen = slot,view
            else:
                prior = self._reservation(view,slot)
                require(prior["project"]!=project,"existing project debt requires recovery; no replay")
        require(chosen is not None,"Host project capacity exhausted")
        slot,view = chosen
        body = dict(version=1,registry_id=self._anchor.registry_id,envelope=self._anchor.layout.envelope.digest,
                    slot=slot,project=project,fingerprint=fingerprint)
        raw = canonical(dict(body=body,checksum=hashlib.sha256(canonical(body)).hexdigest()))
        state = slot_records.WorkerSlotState(raw)
        journal = self._journal(slot)
        committed = journal.update(view,state)
        require(committed.head is not None and committed.head.state==state,"project reservation commit is unconfirmed")
        self._release(journal)
        ticket = ProjectReservation(slot,project,fingerprint,raw)
        self._tickets[id(ticket)] = ticket
        self._ready = True
        return ticket

    def require_ticket(self, ticket):
        self._require_identity()
        require(self._ready and not self._closed and type(ticket) is ProjectReservation
                and self._tickets.get(id(ticket)) is ticket,"exact live Host capacity ticket required")

    def close(self):
        self._require_identity()
        self._ready = False
        if not self._closed:
            # Reverse creation order keeps the gate/namespace scope last.
            for _,child in tuple(reversed(tuple(self._resources.values()))):
                if not self._release(child):
                    return False
            self._closed = True
            self._gate_held = False
        self._registry._owners.pop(id(self),None)
        _LIVE_HOSTS.pop(id(self),None)
        return True

