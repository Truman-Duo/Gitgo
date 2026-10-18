"""Once-only actual repository schemas and committed project identity binding.

Call only inside an accepted qualified worker with the exact namespace held.
Verification reads committed facts and never completes a partial migration.
This inactive component is not the production storage router.
"""
import hashlib
import sqlite3

from .host_records import require
from .migrations import STATE_MIGRATIONS, OBSERVABILITY_MIGRATIONS
from .native_windows_sqlite import WindowsSqliteLibrary, WindowsSqliteVfs
from .project_namespace import ProjectNamespace, ROLES
from .runtime import StorageRuntime
from .runtime_manifest import canonical,digest
from .transactions import authoritative_transaction

MIGRATIONS={"state":STATE_MIGRATIONS,"observability":OBSERVABILITY_MIGRATIONS}
# New code reads the retained append-only history directly. The old public
# name cannot be evaluated by a legacy runtime, even one predating the format
# admission check. No compatible/no-op UDF is registered by this implementation.
GUARD=(
    "ALTER TABLE schema_migrations RENAME TO schema_migration_records",
    "CREATE VIEW schema_migrations AS SELECT version,name,checksum,applied_at FROM schema_migration_records WHERE gitgo_storage_legacy_refused()=1",
    "CREATE TABLE storage_protocol(id INTEGER PRIMARY KEY CHECK(id=1),binding BLOB NOT NULL)",
)


def schema_digest(connection):
    rows=tuple(tuple(row) for row in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name LIMIT 257"))
    require(len(rows)<=256,"qualified schema object capacity exceeded")
    require(all(type(row[0]) is str and row[0] in {"table","index","view"}
                and all(value is None or type(value) is str and len(value.encode('utf8'))<=65536 for value in row)
                for row in rows),"bounded ordinary schema objects required")
    return hashlib.sha256(canonical(rows)).hexdigest()


class ProjectDatabases:
    def __init__(self, namespace, library, slot, expected_schema):
        require(type(namespace) is ProjectNamespace and type(library) is WindowsSqliteLibrary,
                "owned exact project namespace and selected SQLite modules required")
        require(type(slot) is int and 0<=slot<namespace.anchor.layout.envelope.slots,"exact Host slot required")
        require(type(expected_schema) is tuple and len(expected_schema)==2
                and all(type(value) is str and len(value)==64 for value in expected_schema),
                "both pristine build schema digests required")
        for value in expected_schema:digest(value)
        self.namespace,self.library,self.slot=namespace,library,slot
        self.expected=dict(zip(ROLES,expected_schema));self.owners={};self.databases={}
        self._sealed=False;self._attempted=set()

    def _open(self,role):
        self.namespace.require_live()
        require(not self._sealed and type(role) is str and role in ROLES and role not in self._attempted,
                "one database role attempt per worker required")
        self._attempted.add(role)
        host=self.namespace.host
        owner=host._new(WindowsSqliteVfs,self.library,self.namespace.database_path(role),self.namespace.limits[role])
        self.owners[role]=owner
        db=owner.connect().database
        self.databases[role]=db
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA cache_spill=OFF")
        db.execute("PRAGMA cache_size=-16384")
        db.execute("PRAGMA trusted_schema=OFF")
        return db

    def _binding(self,role):
        ns=self.namespace
        return ns.record.database_binding(ns.anchor,self.slot,ns.project,role)

    def _stop(self):
        self._sealed=True
        for owner in self.owners.values():owner._sealed=True

    def initialize(self):
        self.namespace.require_empty_cas()
        require(self.namespace.record.phase=="STRUCTURED","once-only structure phase before migrations required")
        try:
            for role in ROLES:
                db=self._open(role)
                require(db.execute("PRAGMA page_count").fetchone()[0]==0
                        and not db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone(),
                        "initialization cannot adopt or resume an existing database")
                require(db.execute("PRAGMA journal_mode=WAL").fetchone()[0]=="wal","qualified WAL mode required")
                StorageRuntime._apply_migrations(db,MIGRATIONS[role])
                with authoritative_transaction(db):
                    for sql in GUARD:db.execute(sql)
                    db.execute("INSERT INTO storage_protocol VALUES(1,?)",(self._binding(role),))
                self._verify(role,db)
            self.namespace.verify()
        except BaseException:
            self._stop();raise

    def _verify(self,role,db):
        require(db.execute("PRAGMA page_size").fetchone()[0]==4096
                and db.execute("PRAGMA encoding").fetchone()[0]=="UTF-8"
                and db.execute("PRAGMA auto_vacuum").fetchone()[0]==0,"database physical layout profile changed")
        require(schema_digest(db)==self.expected[role],"stored schema differs from the pristine qualified build")
        rows=db.execute("SELECT id,binding FROM storage_protocol LIMIT 2").fetchall()
        require([tuple(row) for row in rows]==[(1,self._binding(role))],"database belongs to another project or native generation")
        history=db.execute("SELECT version,name,checksum FROM schema_migration_records ORDER BY version LIMIT 257").fetchall()
        require([tuple(row) for row in history]==[(m.version,m.name,m.checksum) for m in MIGRATIONS[role]],
                "committed repository migrations are incomplete or changed")
        # The initializer has no business-write capability. A matching schema
        # and binding cannot excuse unrelated rows appearing before readiness.
        tables=db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT IN ('storage_protocol','schema_migration_records') LIMIT 257").fetchall()
        require(len(tables)<=256,"bounded initialization table inventory required")
        for row in tables:
            name=row[0]
            require(type(name) is str and len(name)<=128,"bounded qualified table name required")
            quoted='"'+name.replace('"','""')+'"'
            require(db.execute("SELECT 1 FROM "+quoted+" LIMIT 1").fetchone() is None,
                    "unexpected business rows before initialization readiness")
        require(db.execute("PRAGMA journal_mode").fetchone()[0]=="wal","database journal profile changed")
        require([tuple(row) for row in db.execute("PRAGMA quick_check").fetchall()]==[("ok",)],
                "database structural verification failed")

    def verify_initialized(self):
        try:
            for role in ROLES:
                db=self._open(role)
                # This owner is used solely to inspect committed initialization
                # facts. Unknown creation/migration outcomes are never replayed.
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                try:self._verify(role,db)
                finally:db.execute("ROLLBACK")
            self.namespace.verify()
        except BaseException:
            self._stop();raise
