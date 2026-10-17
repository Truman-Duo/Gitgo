"""One canonical Host storage authority and its closed bootstrap description.

These immutable values do not create an installation approval, grant, lease or
capacity ticket. Runtime/native proofs are supplied by the owning coordinator.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import PureWindowsPath
import re
import uuid

from backend.core.sandbox_read import ObjectIdentity
from .envelope import FileEnvelope, StorageEnvelope
from .runtime_manifest import RuntimeContent, RuntimeManifest, canonical, local_path, charge, digest, runtime_copy_classes
from .models import StorageBlocked

HOST_LEAF = "storage-authority-v1"
MAX_HOST_BYTES = 65536
MAX_PROJECT_SLOTS = 11


class HostAuthorityInvalid(StorageBlocked):
    code = "STORAGE_HOST_AUTHORITY"


def require(condition, reason):
    if not condition:
        raise HostAuthorityInvalid(reason)


def identity(value):
    require(type(value) is ObjectIdentity and value.platform == "win32"
            and type(value.volume) is int and 0 <= value.volume < 2**64
            and type(value.file_id) is int and 0 <= value.file_id < 2**128, "exact Host object identity required")


def guid(value):
    try:
        valid = type(value) is str and str(uuid.UUID(value)) == value
    except ValueError:
        valid = False
    require(valid, "canonical Host registry identity required")


@dataclass(frozen=True)
class HostAccount:
    sid: str
    profile: str

    def __post_init__(self):
        require(type(self.sid) is str and re.fullmatch(r"S-1-5(?:-(?:0|[1-9][0-9]{0,9})){1,15}",self.sid),
                "canonical native Host account required")
        require(all(int(part) < 2**32 for part in self.sid.split("-")[3:]), "bounded Host SID subauthority required")
        local_path(self.profile)

    @property
    def parent(self):
        return str(PureWindowsPath(self.profile)/".gitgo")

    @property
    def root(self):
        return str(PureWindowsPath(self.parent)/HOST_LEAF)


@dataclass(frozen=True)
class HostInput:
    """The one charged input copy; this description is not installation approval."""
    root: str
    root_identity: ObjectIdentity
    manifest_digest: str
    content_contract: str

    def __post_init__(self):
        local_path(self.root);identity(self.root_identity)
        digest(self.manifest_digest);digest(self.content_contract)

    @classmethod
    def from_manifest(cls, manifest):
        require(type(manifest) is RuntimeManifest,"exact input manifest required")
        return cls(manifest.root,manifest.root_identity,manifest.sha256,manifest.content.contract)

    def require_content(self, content):
        require(type(content) is RuntimeContent and content.contract==self.content_contract
                and RuntimeManifest(self.root,self.root_identity,content).sha256==self.manifest_digest,
                "input copy does not match the complete content and identity contract")

    def require_disjoint(self, host_root):
        local_path(host_root)
        source,target=PureWindowsPath(self.root),PureWindowsPath(host_root)
        require(not source.is_relative_to(target) and not target.is_relative_to(source),
                "input runtime and Host authority must have disjoint roots")

    @classmethod
    def from_value(cls, value):
        require(type(value) is dict and set(value)=={"root","root_identity","manifest_digest","content_contract"},
                "closed Host input fields required")
        root=value["root_identity"]
        require(type(root) is dict and set(root)=={"platform","volume","file_id"},"closed input root identity required")
        return cls(value["root"],ObjectIdentity(**root),value["manifest_digest"],value["content_contract"])


@dataclass(frozen=True)
class HostMember:
    name: str
    directory: bool
    ceiling: int
    mutable: bool


def metadata_classes(slots):
    require(type(slots) is int and 1 <= slots <= MAX_PROJECT_SLOTS, "bounded Host project slot count required")
    counts = {
        "host_authority": (65536,1), "host_bootstrap": (4096,1), "host_envelope": (8192,1),
        "host_gate": (4096,1), "host_runtime_control": (8192,1), "host_runtime_lease": (4096,1),
        "host_slot_control": (8192,slots), "host_slot_lease": (4096,slots),
        "host_project_ref": (65536,slots),
    }
    return tuple(FileEnvelope(name,*values) for name,values in sorted(counts.items()))


@dataclass(frozen=True)
class HostLayout:
    envelope: StorageEnvelope
    content: RuntimeContent

    def __post_init__(self):
        require(type(self.envelope) is StorageEnvelope and self.envelope.allocation_unit == 4096,
                "qualified immutable Host envelope required")
        require(type(self.content) is RuntimeContent, "closed approved runtime content required")
        actual = {item.name:item for item in self.envelope.registry_files}
        for item in metadata_classes(self.envelope.slots):
            require(actual.get(item.name) == item, "Host metadata and reference capacity mismatch")
        expected = {item.name:item for item in runtime_copy_classes(self.content)}
        require({name:item for name,item in actual.items() if name.startswith(("input_","runtime_"))} == expected,
                "input and installed runtime copies must share the exact Host envelope")
        require(1+len(self.members) <= 32, "native Host bootstrap node capacity exceeded")
        require(sum(charge(member.ceiling) for member in self.members if not member.directory) <= 256*1024,
                "native Host bootstrap byte capacity exceeded")

    @property
    def members(self):
        values = [HostMember("bootstrap.intent",False,4096,False),
            HostMember("storage-maintenance.lock",False,0,True),
            HostMember("candidates",True,0,True),HostMember("runtimes",True,0,True),
            HostMember("authority.json",False,65536,False),HostMember("envelope.json",False,8192,False),
            HostMember("runtime.lease",False,0,True),HostMember("slot-0063.json",False,8192,True)]
        for slot in range(self.envelope.slots):
            values.extend((HostMember(f"slot-{slot:04d}.json",False,8192,True),
                           HostMember(f"slot-{slot:04d}.lease",False,0,True)))
        return tuple(values)

    @property
    def contract(self):
        return hashlib.sha256(canonical(dict(version=1,envelope=self.envelope.digest,
            content=self.content.contract,storage_profile=self.content.storage_profile))).hexdigest()


@dataclass(frozen=True)
class HostAnchor:
    registry_id: str
    account: HostAccount
    parent_identity: ObjectIdentity
    root_identity: ObjectIdentity
    layout: HostLayout
    source: HostInput
    members: tuple[tuple[str,ObjectIdentity], ...]

    def __post_init__(self):
        guid(self.registry_id)
        require(type(self.account) is HostAccount and type(self.layout) is HostLayout, "exact Host installation inputs required")
        require(type(self.source) is HostInput,"exact charged input copy required")
        self.source.require_content(self.layout.content)
        self.source.require_disjoint(self.account.root)
        identity(self.parent_identity);identity(self.root_identity)
        require(self.parent_identity.volume == self.root_identity.volume, "Host authority crossed a volume")
        require(type(self.members) is tuple and all(type(item) is tuple and len(item)==2 for item in self.members),
                "immutable Host member identities required")
        require([name for name,_ in self.members] == sorted(member.name for member in self.layout.members),
                "complete unique Host namespace identities required")
        found = {(self.root_identity.volume,self.root_identity.file_id),
                 (self.parent_identity.volume,self.parent_identity.file_id)}
        require(len(found)==2,"Host root aliases parent")
        for _,value in self.members:
            identity(value)
            pair = (value.volume,value.file_id)
            require(value.volume == self.root_identity.volume and pair not in found,"Host member alias or volume change")
            found.add(pair)
        require((self.source.root_identity.volume,self.source.root_identity.file_id) not in found,
                "input copy aliases the Host namespace")
        require(len(self.to_bytes()) <= MAX_HOST_BYTES, "Host authority encoding exceeded its reserved file")

    def to_bytes(self):
        return canonical(dict(version=1,protocol="gitgo-host-storage-1",registry_id=self.registry_id,
            account=asdict(self.account),parent=asdict(self.parent_identity),root=asdict(self.root_identity),
            layout=self.layout.contract,envelope=self.layout.envelope.digest,source=asdict(self.source),
            members={name:asdict(value) for name,value in self.members}))

    @property
    def binding(self):
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def intent(self):
        raw = bootstrap_intent(self.registry_id,self.account,self.layout,self.parent_identity,self.root_identity,self.source)
        require(len(raw)<=4096,"Host bootstrap intent exceeds reserved size")
        return raw

    @classmethod
    def from_bytes(cls, raw, account, layout):
        require(type(raw) is bytes and 0<len(raw)<=MAX_HOST_BYTES,"bounded Host authority bytes required")
        def pairs(items):
            result={}
            for key,value in items:
                require(key not in result,"duplicate Host authority field");result[key]=value
            return result
        def nonfinite(_):
            raise HostAuthorityInvalid("nonfinite Host authority field")
        try:
            value=json.loads(raw,object_pairs_hook=pairs,parse_constant=nonfinite)
        except (ValueError,RecursionError) as error:
            raise HostAuthorityInvalid("invalid Host authority encoding") from error
        require(type(value) is dict and set(value)=={"version","protocol","registry_id","account","parent","root","layout","envelope","members","source"},
                "closed Host authority fields required")
        require(type(value["version"]) is int and value["version"]==1 and value["protocol"]=="gitgo-host-storage-1",
                "unknown Host authority protocol")
        require(type(account) is HostAccount and value["account"]==asdict(account),"Host account/location mismatch")
        require(type(layout) is HostLayout and value["layout"]==layout.contract and value["envelope"]==layout.envelope.digest,
                "Host content/envelope contract changed; migration required")
        def parse(item):
            require(type(item) is dict and set(item)=={"platform","volume","file_id"},"closed Host native identity required")
            return ObjectIdentity(**item)
        require(type(value["members"]) is dict and len(value["members"])==len(layout.members),"complete Host members required")
        result=cls(value["registry_id"],account,parse(value["parent"]),parse(value["root"]),layout,HostInput.from_value(value["source"]),
                   tuple((name,parse(item)) for name,item in sorted(value["members"].items())))
        require(result.to_bytes()==raw,"noncanonical Host authority encoding")
        return result


def bootstrap_intent(registry_id,account,layout,parent,root,source):
    guid(registry_id);identity(parent);identity(root)
    require(type(source) is HostInput,"exact charged input copy required")
    source.require_content(layout.content)
    source.require_disjoint(account.root)
    return canonical(dict(version=1,protocol="gitgo-host-bootstrap-1",registry_id=registry_id,
        account=asdict(account),layout=layout.contract,parent=asdict(parent),root=asdict(root),source=asdict(source)))
