"""Whole-schema inventory for the closed serial cleanup page bound.

The settled-tree bound includes a source-derived transient DELETE reserve.
Its stated SQLite, schema and operation premises are mandatory; the result
never issues admission, executes cleanup or refunds native capacity.
The schema catalogue must come from a trusted pristine build, never the target.
"""
from dataclasses import dataclass
import hashlib
import json


class Incomplete(RuntimeError): pass


def require(value,reason):
    if not value: raise Incomplete(reason)


def quote(name):
    require(type(name) is str and 0<len(name)<=256,'identifier')
    return '"'+name.replace('"','""')+'"'


def catalogue(db):
    rows=tuple(tuple(row) for row in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name LIMIT 257'))
    require(len(rows)<=256,'schema_count')
    require(all((kind in {'table','index'} or kind=='view' and name=='schema_migrations') and (sql is None or len(sql.encode('utf8'))<=65536)
                for kind,name,table,sql in rows),'schema_kind_or_size')
    columns=[]
    for kind,name,table,sql in rows:
        if kind=='table':
            require(sql and 'VIRTUAL TABLE' not in sql.upper() and 'WITHOUT ROWID' not in sql.upper(),'rowid_table_only')
            fields=tuple(tuple(row) for row in db.execute('PRAGMA table_xinfo('+quote(name)+')'))
            require(0<len(fields)<=32 and all(row[6]==0 for row in fields),'ordinary_columns')
            columns.append((name,tuple(row[1] for row in fields)))
        elif kind=='index':
            fields=tuple(tuple(row) for row in db.execute('PRAGMA index_xinfo('+quote(name)+')'))
            require(0<len(fields)<=33 and all(row[1]>=-1 for row in fields),'ordinary_index_columns')
            require(len({row[1] for row in fields})==len(fields),'distinct_index_columns')
    return rows,tuple(columns)


@dataclass(frozen=True)
class TableInventory:
    name: str
    rows: int
    indexes: int
    payload_bytes: int
    largest_record: int


@dataclass(frozen=True)
class CleanupInventory:
    schema_digest: str
    page_size: int
    pages: int
    free_pages: int
    roots: int
    tables: tuple[TableInventory,...]

    @property
    def row_cells(self):return sum(table.rows for table in self.tables)
    @property
    def index_cells(self):return sum(table.rows*table.indexes for table in self.tables)
    @property
    def payload_records(self):return self.row_cells+self.index_cells
    @property
    def payload_bytes(self):
        return sum(table.payload_bytes*(1+table.indexes)+18*table.rows*table.indexes
                   for table in self.tables)
    @property
    def settled_page_bound(self):
        # A non-empty non-root B-tree page has a cell. A rowid table has at most
        # N-1 interior separator cells in addition to its N leaf records.
        # Each index entry is one cell. Count one root even for empty trees.
        tree_pages=self.roots+2*self.row_cells+self.index_cells
        # Every overflow chain contributes at most ceil(payload/(usable-4)).
        # Sum of ceilings <= floor(total/(usable-4)) + number of records.
        # The file-format reserved-byte field is one byte. Until an exact owned
        # header qualifies zero reserved bytes, use the format maximum of 255.
        overflow_pages=self.payload_bytes//(self.page_size-255-4)+self.payload_records
        return tree_pages+overflow_pages


def capture(db,expected,*,max_rows=1024,max_field_bytes=8192,max_record_bytes=65536,
            max_payload_bytes=8*1024*1024):
    require(db.in_transaction,'owning_snapshot_required')
    require(all(type(value) is int and 0<value<=8*1024*1024 for value in (
        max_rows,max_field_bytes,max_record_bytes,max_payload_bytes)),'inventory_limits')
    require(tuple(catalogue(db))==tuple(expected),'schema_mismatch')
    require(db.execute('PRAGMA page_size').fetchone()[0]==4096,'page_size')
    require(db.execute('PRAGMA encoding').fetchone()[0]=='UTF-8','encoding')
    require(db.execute('PRAGMA auto_vacuum').fetchone()[0]==0,'auto_vacuum')
    page_count=db.execute('PRAGMA page_count').fetchone()[0]
    free=db.execute('PRAGMA freelist_count').fetchone()[0]
    require(0<=free<=page_count<262144,'page_or_lock_byte_boundary')
    schema,columns=expected
    result=[]
    remaining=max_rows
    payload_remaining=max_payload_bytes
    for table,fields in columns:
        count=0;total=0;largest=0
        expression=','.join('COALESCE(length(CAST('+quote(field)+' AS BLOB)),0)' for field in fields)
        cursor=db.execute('SELECT '+expression+' FROM '+quote(table)+' LIMIT ?',(remaining+1,))
        try:
            for row in cursor:
                require(count<remaining,'global_rows')
                require(all(type(size) is int and 0<=size<=max_field_bytes for size in row),'field_size')
                # Eight bytes covers every stored numeric; nine per header field
                # also covers all serial-type/header varints in this profile.
                payload=9*(len(fields)+1)+sum(max(8,size) for size in row)
                require(payload<=max_record_bytes,'record_size')
                count+=1;total+=payload;largest=max(largest,payload)
                require(total<=payload_remaining,'global_payload')
        finally:cursor.close()
        remaining-=count;payload_remaining-=total
        indexes=sum(kind=='index' and owner==table for kind,name,owner,sql in schema)
        result.append(TableInventory(table,count,indexes,total,largest))
    # sqlite_schema itself is a real rowid B-tree whose SQL payload must count.
    schema_payload=sum(9*6+sum(max(8,len(str(value).encode('utf8'))) for value in row if value is not None)+8
                       for row in schema)
    result.append(TableInventory('sqlite_schema',len(schema),0,schema_payload,0))
    digest=hashlib.sha256(json.dumps(expected,separators=(',',':')).encode()).hexdigest()
    return CleanupInventory(digest,4096,page_count,free,1+len(schema),tuple(result))


# Conditional bound for inspected SQLite 3.53.2/3.53.4 btree.c. The
# operational profile restricts those versions; the pinned manifest binds the
# actual native build. Cleanup deletes only ordinary B-tree rows (no schema,
# virtual tables, triggers, VACUUM, or implicit insertion of arbitrary values).
# sqlite3BtreeDelete can call balance twice; each can traverse at most the
# 20-level cursor stack plus a new root level, with at most NB+2 (5) pages.
# Each pass also allows one root deepening page. Existing cell overflow chains
# move intact in DELETE; this bound is not valid for arbitrary INSERT/UPDATE.
# Source: sqlite-amalgamation-3530200/sqlite3.c, BTCURSOR_MAX_DEPTH,
# sqlite3BtreeDelete, balance, balance_nonroot and balance_deeper.
# Format premises: https://www.sqlite.org/fileformat.html
DELETE_TRANSIENT_PAGES = 2 * ((20 + 1) * 5 + 1)


def cleanup_page_bound(found, max_pages, future_payload_bytes):
    require(type(found) is CleanupInventory, 'complete_inventory')
    require(type(max_pages) is int and 1 <= max_pages < 262144, 'before_lock_byte_page')
    require(type(future_payload_bytes) is int and 0 <= future_payload_bytes <= 1024*1024,
            'closed_future_payload')
    # Page 20's reserved-byte count is bounded by its one-byte format. Free-list
    # trunks retain six otherwise usable leaf slots for historical-format
    # compatibility; reserve all possible trunk pages rather than treating every
    # free page as immediately available to an allocation.
    usable=found.page_size-255
    trunk_capacity=(usable//4)-2-6
    trunks=(max_pages+trunk_capacity-1)//trunk_capacity
    future=(future_payload_bytes+usable-5)//(usable-4)
    return found.settled_page_bound+DELETE_TRANSIENT_PAGES+trunks+future


def require_cleanup_capacity(db,expected,max_pages,*,future_payload_bytes=0):
    found=capture(db,expected,max_rows=512,max_field_bytes=65536,
                  max_record_bytes=131072,max_payload_bytes=2*1024*1024)
    require(found.pages<=max_pages,'actual_page_ceiling')
    needed=cleanup_page_bound(found,max_pages,future_payload_bytes)
    require(needed<=max_pages,'delete_cleanup_headroom')
    return found,needed
