"""Retained-fact preparation retirement, without file or worker authority.

Only the admitted coordinator may call finish after original-worker retirement
and the required exact native absence observations. These SQL routines neither
claim those proofs nor issue/reconstruct a creation or deletion capability.
"""
from dataclasses import dataclass

from backend.core.storage.host_records import guid,require
from backend.core.storage.models import StorageBlocked
from backend.core.storage.native_windows_cas import CasFileIntent,CasFileRecord
from backend.core.storage.runtime_manifest import digest
from .batch_records import BatchContext,facts_from_bytes,identity_from_bytes,MAX_MEMBERS
from .batch_sql import transaction,fetchone,fetchall,exact_write


@dataclass(frozen=True)
class RetainedMember:
    ordinal:int
    intent:CasFileIntent
    planned:bytes
    record:CasFileRecord|None
    phase:str


@dataclass(frozen=True)
class RetainedBatch:
    context:BatchContext
    publication:str
    facts:tuple
    members:tuple[RetainedMember,...]
    phase:str

    @property
    def published(self):return all(member.phase=='PUBLISHED' for member in self.members)


def observe(db,request):
    try:
        return _observe(db,request)
    except (ValueError,TypeError,UnicodeError) as error:
        raise StorageBlocked('retained_batch_invalid') from error


def _observe(db,request):
    transaction(db);guid(request)
    row=fetchone(db,'SELECT context,publication,facts,member_count,phase FROM storage_batches WHERE request=?',(request,))
    require(row is not None,'retained batch is absent')
    context=BatchContext.from_bytes(row[0]);digest(row[1]);facts=facts_from_bytes(row[2])
    require(context.request==request and type(row[3]) is int and 1<=row[3]<=MAX_MEMBERS
            and row[4] in {'PREPARED','PUBLISHED','RETIRING'},'retained batch header changed')
    require(all(fact.project_id==context.project for fact in facts),'retained fact project differs')
    values=fetchall(db,'SELECT ordinal,generation,digest,byte_length,planned,birth,phase '
                    'FROM storage_batch_members WHERE request=? ORDER BY ordinal LIMIT ?',
                    (request,MAX_MEMBERS+1))
    require(len(values)==row[3],'retained batch membership is incomplete')
    members=[];births=set()
    for ordinal,member in enumerate(values):
        require(type(member[0]) is int and member[0]==ordinal,'retained ordinal changed')
        intent=CasFileIntent(member[2],member[1],member[3]);intent.require_bytes(member[4])
        require(member[6] in {'PLANNED','CREATED','SEALED','PUBLISHED'}
                and (member[5] is None)==(member[6]=='PLANNED'),'retained birth phase changed')
        record=None
        if member[5] is not None:
            native=identity_from_bytes(member[5])
            require(native.volume==context.parent.volume and native!=context.parent
                    and native not in births,'retained birth aliases or volume differs')
            births.add(native);record=CasFileRecord(intent,native)
        members.append(RetainedMember(ordinal,intent,member[4],record,member[6]))
    result=RetainedBatch(context,row[1],facts,tuple(members),row[4])
    require(len({member.intent.generation for member in members})==len(members),'duplicate generation')
    require(result.published or all(member.phase!='PUBLISHED' for member in members),
            'partial physical ownership handoff')
    require(row[4]=='RETIRING' or result.published==(row[4]=='PUBLISHED'),
            'batch and member publication phases differ')
    return result


def require_outcome(db,retained):
    """Check committed evidence; no SQL reconstruction or execution replay."""
    transaction(db)
    require(type(retained) is RetainedBatch and observe(db,retained.context.request)==retained,
            'complete retained retirement basis changed')
    for fact in retained.facts:
        if retained.published:fact.require_row(db)
        else:
            require(fetchone(db,'SELECT 1 FROM runtime_outbox WHERE event_id=?',(fact.event_id,)) is None,
                    'unpublished batch has committed fact evidence')
    for member in retained.members:
        row=fetchone(db,'SELECT digest,byte_length,birth,phase FROM storage_cas_generations WHERE generation=?',
                     (member.intent.generation,))
        if retained.published:
            require(row is not None and row[0]==member.intent.digest and row[1]==member.intent.length
                    and identity_from_bytes(row[2])==member.record.identity and row[3]=='LIVE',
                    'published generation ownership is incomplete')
        else:
            require(row is None,'unpublished generation has another owner')
            if member.record is not None:
                from .batch_records import identity_bytes
                require(fetchone(db,'SELECT 1 FROM storage_cas_generations WHERE birth=?',
                                 (identity_bytes(member.record.identity),)) is None,
                        'unpublished birth belongs to another generation')


def mark_retiring(db,retained):
    require_outcome(db,retained)
    require(retained.phase in {'PREPARED','PUBLISHED'},'retirement intent already exists')
    exact_write(db,"UPDATE storage_batches SET phase='RETIRING' WHERE request=? AND phase=?",
                (retained.context.request,retained.phase),1)


def finish(db,retained):
    # The coordinator owns the exact native proofs. Accepting this value alone
    # is not a claim that native deletion, worker death or refund was verified.
    require_outcome(db,retained)
    require(retained.phase=='RETIRING','persisted retirement intent required')
    exact_write(db,'DELETE FROM storage_batch_members WHERE request=?',
                (retained.context.request,),len(retained.members))
    exact_write(db,"DELETE FROM storage_batches WHERE request=? AND phase='RETIRING'",
                (retained.context.request,),1)
