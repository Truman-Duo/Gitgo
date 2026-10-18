"""Project construction facts in the existing Host slot journal.

This record is not initialization authority. The owning coordinator checks the
actual accepted worker, prepaid profile, native identities and independent
retirement evidence before updating it. No second project state file is used.
"""
from dataclasses import asdict, dataclass
from pathlib import PureWindowsPath

from backend.core.sandbox_read import ObjectIdentity
from .host_records import HostAnchor, guid, identity, require
from .runtime_manifest import canonical, digest
from .journal_cells import decode


# All initial names are closed. SQLite sidecar lifetimes and CAS generations
# are admitted later by their respective owned protocols, never added here.
DIRECTORIES = ("control", "data", "data/cas")
FILES = ("control/storage-maintenance.lock", "data/cas-lifecycle.lock",
         "data/observability.sqlite3", "data/state.sqlite3", "data/storage-maintenance.lock")
MEMBERS = tuple(sorted(DIRECTORIES+FILES))
PHASES = ("BUILDING", "STRUCTURED", "MATERIALIZED", "VERIFIED", "READY")


@dataclass(frozen=True)
class ProjectIdentity:
    store: str
    root: ObjectIdentity
    members: tuple[tuple[str,ObjectIdentity], ...] = ()

    def __post_init__(self):
        guid(self.store); identity(self.root)
        require(type(self.members) is tuple and all(type(pair) is tuple and len(pair)==2 for pair in self.members),
                "immutable project identities required")
        names = tuple(name for name,_ in self.members)
        require(names in ((), MEMBERS), "exact complete initial project members required")
        seen = {self.root.file_id}
        for _,value in self.members:
            identity(value)
            require(value.volume==self.root.volume and value.file_id not in seen,
                    "project alias or volume change")
            seen.add(value.file_id)

    def require_host(self, anchor):
        require(type(anchor) is HostAnchor,"exact Host anchor required")
        excluded = (anchor.parent_identity,anchor.root_identity,anchor.source.root_identity,
                    *(value for _,value in anchor.members))
        require(self.root.volume==anchor.root_identity.volume,
                "project creation crossed the Host volume")
        require(all(value not in excluded for value in (self.root,*(value for _,value in self.members))),
                "project identity aliases Host or runtime input")

    def value(self):
        return dict(store=self.store,root=asdict(self.root),members={name:asdict(value) for name,value in self.members})

    @classmethod
    def parse(cls, value):
        require(type(value) is dict and set(value)=={"store","root","members"},"closed project identity fields")
        def object_id(raw):
            require(type(raw) is dict and set(raw)=={"platform","volume","file_id"},"closed project object fields")
            return ObjectIdentity(**raw)
        require(type(value["members"]) is dict,"closed project member map")
        return cls(value["store"],object_id(value["root"]),
                   tuple((name,object_id(raw)) for name,raw in sorted(value["members"].items())))


@dataclass(frozen=True)
class ProjectRecord:
    phase: str
    profile: str
    identity: ProjectIdentity

    def __post_init__(self):
        require(type(self.phase) is str and self.phase in PHASES,"closed project construction phase")
        digest(self.profile)
        require(type(self.identity) is ProjectIdentity,"immutable project identity required")
        require(bool(self.identity.members)==(self.phase!="BUILDING"),"complete identity before database effects")

    def to_bytes(self):
        return canonical(dict(version=1,phase=self.phase,profile=self.profile,identity=self.identity.value()))

    @classmethod
    def from_bytes(cls, raw):
        value = decode(raw)
        require(type(value) is dict and set(value)=={"version","phase","profile","identity"}
                and type(value["version"]) is int and value["version"]==1,"closed project record")
        result = cls(value["phase"],value["profile"],ProjectIdentity.parse(value["identity"]))
        require(result.to_bytes()==raw,"canonical project record required")
        return result

    def database_binding(self, anchor, slot, project, role):
        """The exact native namespace is bound into both actual databases."""
        self.identity.require_host(anchor); guid(project)
        require(type(slot) is int and 0<=slot<anchor.layout.envelope.slots,"bounded project slot")
        require(role in ("state","observability") and type(role) is str,"closed database role")
        require(self.phase!="BUILDING","complete native identity before binding")
        require(self.profile==anchor.layout.content.storage_profile,"qualified storage profile changed")
        return canonical(dict(version=1,protocol="gitgo-storage-2",host=anchor.binding,
            slot=slot,project=project,profile=self.profile,role=role,identity=self.identity.value()))

    def root_path(self, anchor, project):
        self.identity.require_host(anchor); guid(project)
        return str(PureWindowsPath(anchor.account.root)/"candidates"/project)


def transition(old, new):
    """Record monotonicity; native authority remains the coordinator's duty."""
    require(type(new) is ProjectRecord,"exact project record required")
    if old is None:
        require(new.phase=="BUILDING","native root must be recorded first")
        return
    require(type(old) is ProjectRecord,"exact previous project record required")
    require(new.profile==old.profile and new.identity.store==old.identity.store
            and new.identity.root==old.identity.root,"project construction binding changed")
    if old==new: return
    require(PHASES.index(new.phase)==PHASES.index(old.phase)+1,"project phase must advance once")
    if old.phase!="BUILDING":
        require(new.identity==old.identity,"native members cannot change after structure is recorded")


def slot_transition(old, new, old_event, new_event, reservation):
    """Validate project facts alongside the *same* slot's worker transition."""
    if new is None:
        require(old is None,"project identity cannot be erased")
        return
    require(type(new) is ProjectRecord and new.profile==reservation["fingerprint"],
            "project record must consume its reserved profile")
    transition(old,new)
    if old==new: return
    require(type(new_event) is dict and new_event["generation"]==0
            and new_event["profile"]==new.profile,"project construction requires its exact worker profile")
    if old is None:
        require(new_event["request"]==new.identity.store,"fresh store identity must belong to initialization request")
    modes = ({"initialize"} if new.phase in {"BUILDING","STRUCTURED"} else
             {"initialize","verify_initialize"} if new.phase=="MATERIALIZED" else {"verify_initialize"})
    require(new_event["mode"] in modes,"closed project initialization or fact-verification worker required")
    require(new_event["phase"]==("RETIRED" if new.phase=="READY" else "ACCEPTED"),
            "project construction requires its accepted worker or confirmed retirement")
    if new.phase=="READY":
        require(type(old_event) is dict and old_event["phase"] in {"ACCEPTED","RETIRED"}
                and old_event["launch"]==new_event["launch"],"independent completion belongs to the original launch")
