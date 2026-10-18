"""Closed initialization-only descriptor, loaded from the pinned runtime.

This initialization-only contract does not qualify checkpoint publication,
cleanup, platform support or a production storage budget by itself.
"""
from dataclasses import asdict,dataclass
import hashlib,sqlite3

from .envelope import FileEnvelope
from .host_records import require
from .journal_cells import decode
from .native_windows_sqlite import SqliteFileLimits
from .runtime_manifest import canonical,digest
from .project_schema import MIGRATIONS,GUARD

PROFILE_FILE="storage-profile.json"


@dataclass(frozen=True)
class ProjectProfile:
    sqlite_version: str
    limits: tuple[SqliteFileLimits,SqliteFileLimits]
    schemas: tuple[str,str]

    def __post_init__(self):
        require(type(self.sqlite_version) is str and self.sqlite_version==sqlite3.sqlite_version,
                "actual selected SQLite version must match profile")
        require(type(self.limits) is tuple and len(self.limits)==2 and all(type(x) is SqliteFileLimits for x in self.limits),
                "complete immutable database family limits required")
        require(sum(x.main+x.wal+x.journal+x.shm for x in self.limits)<=2*1024**3,
                "complete metadata inspection budget required")
        require(type(self.schemas) is tuple and len(self.schemas)==2,"two pristine repository schema digests required")
        for value in self.schemas:digest(value)

    def to_bytes(self):
        return canonical(dict(version=1,operations=["initialize","verify_initialize"],sqlite=self.sqlite_version,
            limits=[asdict(x) for x in self.limits],schemas=self.schemas,
            migrations={role:[(m.version,m.name,m.checksum) for m in values] for role,values in MIGRATIONS.items()},
            guard=GUARD,page_size=4096,cache_kib=16384,spill=False))

    @property
    def sha256(self):return hashlib.sha256(self.to_bytes()).hexdigest()

    @classmethod
    def from_bytes(cls,raw):
        value=decode(raw)
        require(type(value) is dict and set(value)=={"version","operations","sqlite","limits","schemas","migrations","guard","page_size","cache_kib","spill"},
                "closed initialization profile required")
        require(type(value["limits"]) is list and len(value["limits"])==2
                and all(type(x) is dict and set(x)=={"main","wal","journal","shm"} for x in value["limits"])
                and type(value["schemas"]) is list and len(value["schemas"])==2,"complete profile families required")
        result=cls(value["sqlite"],tuple(SqliteFileLimits(**x) for x in value["limits"]),tuple(value["schemas"]))
        require(result.to_bytes()==raw,"canonical supported profile required")
        return result

    def file_classes(self):
        return tuple(sorted((FileEnvelope("project_gates",4096,3),*(
            FileEnvelope(role+"_"+name,getattr(limit,name),1)
            for role,limit in zip(("state","observability"),self.limits)
            for name in ("main","wal","journal","shm"))),key=lambda x:x.name))

    def require_host(self,anchor):
        require(anchor.layout.content.storage_profile==self.sha256
                and anchor.layout.envelope.files==self.file_classes(),
                "profile, runtime and complete prepaid project family differ")

    @classmethod
    def from_pins(cls,pins):
        require(pins.ready,"complete live runtime pins required")
        member=next((m for m in pins.manifest.content.files if m.path==PROFILE_FILE),None)
        require(member is not None and 0<member.length<=8192,"bounded pinned storage profile missing")
        profile=cls.from_bytes(pins.read(PROFILE_FILE,0,member.length))
        require(profile.sha256==pins.manifest.content.storage_profile,"selected runtime storage profile changed")
        return profile
