"""Exact project metadata and leases beneath a canonical, gated Host owner.

This component neither admits a worker nor opens SQLite. The coordinator must
first consume its exact persisted native launch, runtime and capacity ticket.
All native children join the existing Host owner before construction effects.
"""
from pathlib import Path, PurePosixPath

from .host_records import require, guid
from .native_windows_host import WindowsHostOwner
from .native_windows_scope import WindowsStorageScope
from .native_windows_tree import DatabasePins, TreePurpose
from .native_windows_sqlite import SqliteFileLimits
from .project_records import ProjectRecord, DIRECTORIES, FILES

ROLES=("state","observability")
SIDECARS=tuple(role+".sqlite3"+suffix for role in ROLES for suffix in ("-wal","-shm","-journal"))


class ProjectNamespace:
    def __init__(self, host, record, project, limits):
        require(type(host) is WindowsHostOwner and type(record) is ProjectRecord,
                "owned Host and immutable recorded project required")
        self.anchor=host.anchor
        record.identity.require_host(self.anchor);guid(project)
        require(record.phase!="BUILDING","complete project identities required before database access")
        require(type(limits) is tuple and len(limits)==2 and all(type(x) is SqliteFileLimits for x in limits),
                "both immutable database file families required")
        require(sum(v.main+v.wal+v.journal+v.shm for v in limits)<=2*1024**3,
                "complete database metadata inspection capacity exceeded")
        self.host,self.record,self.project,self.limits=host,record,project,dict(zip(ROLES,limits))
        self.path=Path(record.root_path(self.anchor,project))
        self._attempted=self._ready=False
        self.scope=self.pins=None
        self.handles={};self.indices={"":0}

    def pin(self):
        self.host._require_gate()
        require(not self._attempted,"one project namespace acquisition attempt required")
        self._attempted=True
        host=self.host;members=dict(self.record.identity.members)
        self.scope=host._new(WindowsStorageScope,host._registry.scopes)
        parent=self.scope.import_parent(host._members["candidates"],dict(self.anchor.members)["candidates"])
        root=self.scope.open(self.project,parent,directory=True)
        require(self.scope.identity(root)[0]==self.record.identity.root,"project root was replaced")
        self.pins=host._new(DatabasePins,host._registry.trees,TreePurpose.DATA,self.anchor.account.sid)
        self.pins.root(root,self.record.identity.root)
        self.handles[""]=root
        for name in DIRECTORIES+FILES:
            path=PurePosixPath(name);parent_name="" if str(path.parent)=="." else str(path.parent)
            directory=name in DIRECTORIES
            ceiling=0 if directory or not name.endswith(".sqlite3") else self.limits[path.stem].main
            self.indices[name]=self.pins.add(self.indices[parent_name],path.name,members[name],
                                            directory=directory,ceiling=ceiling)
            # These scope handles belong to the same native objects just pinned
            # against deletion. The pins outlive every SQL/VFS child.
            handle=self.scope.open(path.name,self.handles[parent_name],directory=directory)
            require(self.scope.identity(handle)[0]==members[name],"project member changed while opening lease")
            self.handles[name]=handle
        for name in ("control/storage-maintenance.lock","data/storage-maintenance.lock","data/cas-lifecycle.lock"):
            self.scope.lock(self.handles[name],members[name],exclusive=True)
        self._verify_layout()
        for role in ROLES:
            for suffix in ("wal","shm","journal"):
                index=self.pins.inspect_sidecar(self.indices["data"],role+".sqlite3-"+suffix,
                                                 getattr(self.limits[role],suffix))
                # SQLite owns sidecar removal. The protected parent and all
                # exclusive leases remain retained after this known closure.
                if index is not None:self.pins.release(index)
        self._ready=True
        return self

    def _verify_layout(self):
        for name,index in self.indices.items():
            expected=self.record.identity.root if not name else dict(self.record.identity.members)[name]
            require(self.pins.identity(index)==expected,"project native identity changed")
        fixed={"": {"control","data"}, "control":{"storage-maintenance.lock"},
               "data":{"cas","state.sqlite3","observability.sqlite3","storage-maintenance.lock","cas-lifecycle.lock"}}
        for name,wanted in fixed.items():
            names=self.pins.inventory(self.indices[name]);actual=set(names)
            require(len(names)==len(actual),"duplicate project member names")
            require(actual==wanted if name!="data" else wanted<=actual<=wanted|set(SIDECARS),
                    "unqualified project namespace member")

    def require_live(self):
        self.host._require_identity()
        require(self._ready and not self.host._closed and not self.scope._sealed,
                "live project namespace and exclusive leases required")
        self.pins._require()
        return self

    def require_empty_cas(self):
        self.require_live()
        require(self.pins.inventory(self.indices["data/cas"])==(),"fresh initialization cannot adopt CAS files")

    def verify(self):
        self.require_live();self._verify_layout()

    def database_path(self,role):
        self.require_live()
        require(type(role) is str and role in ROLES,"closed database role required")
        return self.path/"data"/(role+".sqlite3")
