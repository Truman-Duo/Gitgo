"""SQL half of durable preparation and atomic ownership handoff.

All methods require the admitted owner's existing State transaction. No method
opens a file, commits a transaction, issues a ticket, reconstructs executable
SQL, or refunds an OS/native lifetime. Execution admission belongs to the enclosing selected worker.
"""
from dataclasses import dataclass

from backend.core.storage.checkpoint_plan import recheck_basis
from backend.core.storage.native_windows_cas import CasFileRecord
from backend.core.storage.host_records import require
from backend.core.storage.models import StorageBlocked

from .batch_records import (
    BatchContext, BatchPreparation, MAX_FACT_BYTES, MAX_MEMBERS, MAX_GENERATIONS,
    MAX_PREPARATIONS, facts_from_bytes, identity_bytes, identity_from_bytes,
)

SCHEMA = (
    """CREATE TABLE storage_batches (
        request TEXT PRIMARY KEY,
        context BLOB NOT NULL CHECK(typeof(context)='blob' AND length(context)<=4096),
        publication TEXT NOT NULL CHECK(length(publication)=64),
        facts BLOB NOT NULL CHECK(typeof(facts)='blob' AND length(facts)<=65536),
        member_count INTEGER NOT NULL CHECK(member_count BETWEEN 1 AND 8),
        phase TEXT NOT NULL CHECK(phase IN ('PREPARED','PUBLISHED','RETIRING')))""",
    """CREATE TABLE storage_batch_members (
        request TEXT NOT NULL REFERENCES storage_batches(request),
        ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 7),
        generation TEXT NOT NULL UNIQUE,
        digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_length INTEGER NOT NULL CHECK(byte_length BETWEEN 0 AND 65536),
        planned BLOB NOT NULL CHECK(typeof(planned)='blob' AND length(planned)=byte_length),
        birth BLOB UNIQUE CHECK(birth IS NULL OR typeof(birth)='blob' AND length(birth)<=256),
        phase TEXT NOT NULL CHECK(phase IN ('PLANNED','CREATED','SEALED','PUBLISHED')),
        PRIMARY KEY(request,ordinal),
        CHECK((phase='PLANNED')=(birth IS NULL)))""",
    """CREATE TABLE storage_cas_generations (
        generation TEXT PRIMARY KEY,
        digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_length INTEGER NOT NULL CHECK(byte_length BETWEEN 0 AND 65536),
        birth BLOB UNIQUE NOT NULL CHECK(typeof(birth)='blob' AND length(birth)<=256),
        phase TEXT NOT NULL CHECK(phase IN ('LIVE','RETIRING','ABSENT')))""",
    """CREATE TABLE storage_cas_current (
        digest TEXT PRIMARY KEY,
        generation TEXT UNIQUE NOT NULL REFERENCES storage_cas_generations(generation))""",
)


def transaction(db):
    db.require_available()
    require(db.in_transaction,"owning State transaction required")


def fetchone(db,sql,values=()):
    cursor=db.execute(sql,values)
    try:return cursor.fetchone()
    finally:cursor.close()


def fetchall(db,sql,values=()):
    cursor=db.execute(sql,values)
    try:return cursor.fetchall()
    finally:cursor.close()


def exact_write(db,sql,values,expected):
    cursor=db.execute(sql,values)
    try:require(cursor.rowcount==expected,"exact batch transition required")
    finally:cursor.close()


def head(db,context,publication=None):
    transaction(db)
    row=fetchone(db,"SELECT context,publication,facts,member_count,phase FROM storage_batches WHERE request=?",
                 (context.request,))
    require(row is not None and row[0]==context.to_bytes(),"exact retained batch owner required")
    if publication is not None:
        require(row[1]==publication.digest
                and facts_from_bytes(row[2])==publication.facts
                and row[3]==len(publication.objects.members),"complete retained publication changed")
    return row


def prepare(db,plan):
    transaction(db)
    require(type(plan) is BatchPreparation,"repository preparation required")
    context=plan.context
    require(fetchone(db,"SELECT 1 FROM storage_batches WHERE request=?",(context.request,)) is None,
            "retained preparation never issues a fresh ticket")
    require(len(fetchall(db,"SELECT request FROM storage_batches LIMIT ?",(MAX_PREPARATIONS+1,)))<MAX_PREPARATIONS,
            "complete preparation slot capacity")
    committed=fetchall(db,"SELECT generation FROM storage_cas_generations LIMIT ?",(MAX_GENERATIONS+1,))
    pending=fetchall(db,"SELECT generation FROM storage_batch_members WHERE phase!='PUBLISHED' LIMIT ?",
                     (MAX_GENERATIONS+1,))
    require(len(committed)+len(pending)+len(plan.intents)<=MAX_GENERATIONS,
            "all live and unretired CAS copies must remain charged")
    existing={row[0] for row in committed+pending}
    require(not any(intent.generation in existing for intent in plan.intents),"generation already has an owner")
    recheck_basis(db,plan.publication.process_id,plan.publication.parent_process_id,plan.publication.basis)
    exact_write(db,"INSERT INTO storage_batches VALUES(?,?,?,?,?,'PREPARED')",
                (context.request,context.to_bytes(),plan.publication.digest,plan.fact_bytes,len(plan.intents)),1)
    for ordinal,(intent,obj) in enumerate(zip(plan.intents,plan.publication.objects.members)):
        exact_write(db,"INSERT INTO storage_batch_members VALUES(?,?,?,?,?,?,NULL,'PLANNED')",
                    (context.request,ordinal,intent.generation,intent.digest,intent.length,obj.data),1)
    # No return value can be mistaken for post-COMMIT permission. The owner
    # creates its in-memory capability only after authoritative_transaction
    # reports a known commit; recovery only reads the retained facts.


def birth(db,plan,ordinal,record):
    transaction(db)
    require(type(plan) is BatchPreparation and type(ordinal) is int
            and 0<=ordinal<len(plan.intents) and type(record) is CasFileRecord,
            "exact planned native birth")
    require(record.intent==plan.intents[ordinal]
            and record.identity.volume==plan.context.parent.volume
            and record.identity!=plan.context.parent,"native birth binding changed")
    require(head(db,plan.context,plan.publication)[4]=='PREPARED',"unpublished preparation required")
    encoded=identity_bytes(record.identity)
    require(fetchone(db,"SELECT 1 FROM storage_cas_generations WHERE birth=?",(encoded,)) is None,
            "native birth aliases an existing generation")
    exact_write(db,"UPDATE storage_batch_members SET birth=?,phase='CREATED' "
                "WHERE request=? AND ordinal=? AND generation=? AND digest=? AND byte_length=? AND phase='PLANNED' AND birth IS NULL",
                (encoded,plan.context.request,ordinal,record.intent.generation,
                 record.intent.digest,record.intent.length),1)


def seal(db,plan,ordinal,record):
    transaction(db)
    require(type(plan) is BatchPreparation and type(ordinal) is int
            and 0<=ordinal<len(plan.intents) and type(record) is CasFileRecord
            and record.intent==plan.intents[ordinal],"exact verified native generation")
    require(head(db,plan.context,plan.publication)[4]=='PREPARED',"unpublished preparation required")
    exact_write(db,"UPDATE storage_batch_members SET phase='SEALED' "
                "WHERE request=? AND ordinal=? AND generation=? AND birth=? AND phase='CREATED'",
                (plan.context.request,ordinal,record.intent.generation,identity_bytes(record.identity)),1)


def members(db,plan,records):
    require(type(records) is tuple and len(records)==len(plan.intents)
            and all(type(record) is CasFileRecord for record in records),"complete immutable native handoff")
    actual=fetchall(db,"SELECT ordinal,generation,digest,byte_length,planned,birth,phase "
                    "FROM storage_batch_members WHERE request=? ORDER BY ordinal LIMIT ?",
                    (plan.context.request,MAX_MEMBERS+1))
    expected=[]
    for ordinal,(intent,obj,record) in enumerate(zip(plan.intents,plan.publication.objects.members,records)):
        require(record.intent==intent,"native record does not belong to planned object")
        expected.append((ordinal,intent.generation,intent.digest,intent.length,obj.data,
                         identity_bytes(record.identity),'SEALED'))
    require([tuple(row) for row in actual]==expected,"complete sealed membership changed")


def publish(db,plan,records):
    transaction(db)
    require(type(plan) is BatchPreparation,"repository preparation required")
    require(head(db,plan.context,plan.publication)[4]=='PREPARED',"fresh prepared batch required")
    members(db,plan,records)
    # The existing repository plan performs State, outbox and logical-root
    # writes. Physical generation ownership joins that SAME transaction.
    plan.publication.apply(db)
    for intent,record in zip(plan.intents,records):
        exact_write(db,"INSERT INTO storage_cas_generations VALUES(?,?,?,?,'LIVE')",
                    (intent.generation,intent.digest,intent.length,identity_bytes(record.identity)),1)
        exact_write(db,"INSERT INTO storage_cas_current VALUES(?,?) ON CONFLICT(digest) "
                    "DO UPDATE SET generation=excluded.generation",(intent.digest,intent.generation),1)
    exact_write(db,"UPDATE storage_batch_members SET phase='PUBLISHED' WHERE request=? AND phase='SEALED'",
                (plan.context.request,),len(plan.intents))
    exact_write(db,"UPDATE storage_batches SET phase='PUBLISHED' WHERE request=? AND phase='PREPARED'",
                (plan.context.request,),1)
    # Preparation payloads are deliberately retained after handoff. A separate
    # native worker retirement plus committed-fact verification must precede
    # their deletion and any capacity release.


def require_published(db,plan,records):
    transaction(db)
    require(type(plan) is BatchPreparation,"exact retained publication")
    require(head(db,plan.context,plan.publication)[4]=='PUBLISHED',"publication not committed")
    require(type(records) is tuple and len(records)==len(plan.intents)
            and all(type(record) is CasFileRecord for record in records),"complete retained native generations")
    actual=fetchall(db,"SELECT ordinal,generation,digest,byte_length,planned,birth,phase "
                    "FROM storage_batch_members WHERE request=? ORDER BY ordinal LIMIT ?",
                    (plan.context.request,MAX_MEMBERS+1))
    expected=[]
    for ordinal,(intent,obj,record) in enumerate(zip(plan.intents,plan.publication.objects.members,records)):
        require(record.intent==intent,"retained generation differs")
        encoded=identity_bytes(record.identity)
        expected.append((ordinal,intent.generation,intent.digest,intent.length,obj.data,encoded,'PUBLISHED'))
        generation=fetchone(db,"SELECT digest,byte_length,birth,phase FROM storage_cas_generations WHERE generation=?",
                            (intent.generation,))
        require(generation is not None and tuple(generation)==(intent.digest,intent.length,encoded,'LIVE'),
                "complete generation handoff evidence required")
    require([tuple(row) for row in actual]==expected,"retained published membership changed")
    plan.publication.require_retained_facts(db)
