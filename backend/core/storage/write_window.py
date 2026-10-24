"""Native enforcement of a prepaid write window; not durable admission.

The Host ledger must assign this complete finite range before worker effects.
This owner only consumes it; it never reconstructs, extends, retries or refunds
the range. The admitted cycle supplies the range; unknown outcomes retain its debt.
"""
from contextlib import contextmanager
from dataclasses import dataclass

from backend.core.storage.host_records import require
from backend.core.storage.native_windows_sqlite import WindowsSqliteVfs,_OwnedStorageConnection
from backend.core.storage.transactions import authoritative_transaction
from backend.core.storage.wal_capacity import WalFrameBound


@dataclass(frozen=True)
class WriteRange:
    bound:WalFrameBound
    before:int
    attempts:int

    def __post_init__(self):
        require(type(self.bound) is WalFrameBound and self.bound.page_size==4096,
                'qualified native page bound required')
        require(type(self.before) is int and 0<=self.before<64
                and type(self.attempts) is int and 0<self.attempts<=64-self.before,
                'finite complete prepaid write range required')

    def ceiling(self,used):
        require(type(used) is int and 0<used<=self.attempts,'one spent attempt required')
        return 32+(self.before+used)*self.bound.transaction_bytes


class WriteWindow:
    def __init__(self,db,owner,grant):
        require(type(db) is _OwnedStorageConnection and type(owner) is WindowsSqliteVfs
                and db._native_owner is owner and type(grant) is WriteRange,
                'one actual owned native connection and exact write range required')
        db.require_available()
        require(not db.in_transaction and owner.limits.main==grant.bound.main_bytes
                and grant.ceiling(grant.attempts)<=owner.limits.wal,
                'write range exceeds the complete native family')
        for pragma,expected in (('page_size',4096),('auto_vacuum',0),('journal_mode','wal'),
                                ('cache_spill',0),('cache_size',-16384),
                                ('temp_store',2),('synchronous',2),('wal_autocheckpoint',0)):
            cursor=db.execute('PRAGMA '+pragma)
            try:row=cursor.fetchone()
            finally:cursor.close()
            require(row is not None and row[0]==expected,'write window configuration changed: '+pragma)
        cursor=db.execute('PRAGMA mmap_size')
        try:row=cursor.fetchone()
        finally:cursor.close()
        # This exact native VFS always returns NULL from xFetch and does not
        # forward mmap file-control. SQLite may therefore return no PRAGMA row;
        # absence here is the supported VFS contract, not a WAL-size estimate.
        require(row is None or row[0]==0,'native mmap configuration changed')
        require(getattr(owner,'_write_window',None) is None,'native owner already consumed a write window')
        self.db,self.owner,self.grant=db,owner,grant
        self.spent=0;owner._write_window=self

    @contextmanager
    def attempt(self):
        self.db.require_available()
        require(not self.db.in_transaction,'write window cannot nest')
        require(self.spent<self.grant.attempts,'prepaid write attempts exhausted')
        self.spent+=1  # Consume before native limit or BEGIN can have an effect.
        ceiling=self.grant.ceiling(self.spent)
        try:
            self.owner.limit('wal',ceiling)
            yield self.db
            require(not self.db.in_transaction,'operation left an open transaction')
            extent=self.owner.extent('wal')
            require(type(extent) is int and 0<=extent<=ceiling,
                    'owned WAL extent is unknown or exceeds the prepaid range')
        except BaseException:
            # Never restore a native limit, retry a lost acknowledgement, or
            # replace the original exception with a second call on a sealed VFS.
            self.db.quarantine()
            raise

    @contextmanager
    def transaction(self):
        with self.attempt():
            with authoritative_transaction(self.db):yield self.db
