"""Accepted serial worker: repository publication, observation and retirement."""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import ctypes,threading
from .host_records import require
from .models import StoragePolicy
from .project_records import ProjectRecord
from .project_namespace import ProjectNamespace,ROLES
from .project_schema import ProjectDatabases
from .runtime import _protected_checkpoint_catalogue
from .project_registration import ProjectRegistration
from .project_worker import sqlite_library
from .native_windows_cas import WindowsCasWriterLibrary,WindowsCasGenerationLibrary,WindowsCasGeneration,CasPurpose,CasFileIntent,CasFileRecord
from .batch_records import BatchContext,identity_from_bytes
from . import batch_sql as batch,batch_inventory as inventory,batch_retirement as retirement
from .cycle_ledger import COUNTS,READ_MODES,schedule
from .write_window import WriteRange,WriteWindow
from .native_checkpoint import NativeCheckpoint
from .committed_reader import CommittedReader
from .operation_payload import parse_payload
from .repositories.checkpoints import plan_agent_checkpoint
from .repositories.objects import _encoded_object,_register_object

@contextmanager
def snapshot(db):
    require(not db.in_transaction,'one committed observation snapshot')
    db.execute('BEGIN')
    try:yield db
    finally:db.execute('ROLLBACK')

def records(db):
    rows=batch.fetchall(db,'SELECT generation,digest,byte_length,birth,phase FROM storage_cas_generations ORDER BY generation LIMIT 33')
    require(len(rows)<=32,'complete physical generation capacity exceeded')
    result=tuple((CasFileRecord(CasFileIntent(row[1],row[0],row[2]),identity_from_bytes(row[3])),row[4]) for row in rows)
    require(all(phase in {'LIVE','RETIRING'} for _,phase in result),'unqualified generation phase')
    return result

def namespace_inventory(ns,db):
    values=records(db)
    pending=batch.fetchall(db,'SELECT generation,digest,byte_length,birth,phase FROM storage_batch_members WHERE phase!=\'PUBLISHED\' LIMIT 9')
    require(len(pending)<=8 and len(values)+len(pending)<=32,'complete physical ownership capacity exceeded')
    expected={record.intent.name for record,phase in values}
    required={record.intent.name for record,phase in values if phase=='LIVE'}
    for generation,digest,length,birth,phase in pending:
        name=CasFileIntent(digest,generation,length).name
        require(name not in expected,'physical generation has duplicate owners')
        expected.add(name)
    names=ns.pins.inventory(ns.indices['data/cas'])
    require(len(names)==len(set(names)) and required<=set(names)<=expected,'unowned, missing or aliased CAS namespace')
    return values,set(names)

def native_read(ns,library,record,*,purpose=CasPurpose.READ,planned=None,remove=False):
    owner=ns.host._new(WindowsCasGeneration,library,purpose)
    parent=dict(ns.record.identity.members)['data/cas']
    present=owner.open(ns.handles['data/cas'],parent,record)
    if present:
        data=owner.read(planned_bytes=planned)
        if remove:owner.delete()
    else:
        require(remove,'committed generation disappeared');data=None
    if remove:require(owner.observe_absence(),'native deletion absence is unconfirmed')
    require(ns.host._release(owner),'native generation closure is incomplete')
    return data

def require_batch_context(retained,control,ns):
    context=retained.context;request=control.request
    require(context.host==request.host and context.project==request.project and context.slot==request.slot
            and context.store==ns.record.identity.store and context.profile==request.profile
            and context.runtime==request.runtime and context.epoch==control._cycle.epoch
            and context.request==request.request and context.parent==dict(ns.record.identity.members)['data/cas'],
            'retained batch differs from admitted cycle and namespace')

def verify_checkpoint(control,ns,db,readers):
    with snapshot(db):
        namespace_inventory(ns,db)
        if batch.fetchone(db,'SELECT 1 FROM storage_batches WHERE request=?',(control.request.request,)) is None:
            return dict(status='NO_RETAINED_BATCH')
        retained=retirement.observe(db,control.request.request)
        require_batch_context(retained,control,ns);retirement.require_outcome(db,retained)
        if not retained.published:return dict(status='NOT_PUBLISHED')
        for member in retained.members:
            require(native_read(ns,readers,member.record)==member.planned,'published bytes differ from retained preparation')
        reader=CommittedReader(ns,db,readers,control.profile.operational.checkpoint_limits)
        fact=retained.facts[0];payload=reader.read_json(fact.payload_ref)
        require(type(payload.get('checkpoint')) is str and type(payload.get('revision')) is int
                and payload['revision']>0,'committed checkpoint reference is incomplete')
        return dict(status='PUBLISHED',checkpoint=payload['checkpoint'],revision=payload['revision'],event=fact.event_id)

def retire_checkpoint(control,ns,db,readers,window):
    with snapshot(db):
        _,names=namespace_inventory(ns,db)
        if batch.fetchone(db,'SELECT 1 FROM storage_batches WHERE request=?',(control.request.request,)) is None:
            require(batch.fetchone(db,'SELECT 1 FROM storage_batches LIMIT 1') is None,'another batch retains debt')
            return dict(status='ABSENT')
        retained=retirement.observe(db,control.request.request)
        require_batch_context(retained,control,ns);retirement.require_outcome(db,retained)
        # No birth identity can be inferred from a filename after a lost create.
        for member in retained.members:
            require(member.record is not None or member.intent.name not in names,'present CAS name has unknown birth identity')
    if retained.phase!='RETIRING':
        with window.transaction():retirement.mark_retiring(db,retained)
        retained=replace(retained,phase='RETIRING')
    if retained.published:
        for member in retained.members:
            require(native_read(ns,readers,member.record)==member.planned,'committed generation changed before payload retirement')
    else:
        for member in retained.members:
            if member.record is not None:
                native_read(ns,readers,member.record,purpose=CasPurpose.RETIRE_UNPUBLISHED,planned=member.planned,remove=True)
    with window.transaction():retirement.finish(db,retained)
    return dict(status='RETIRED')

def collect_obsolete(control,ns,db,readers,window):
    with snapshot(db):
        require(batch.fetchone(db,'SELECT 1 FROM storage_batches LIMIT 1') is None,'preparation debt prevents collection')
        generations,_=namespace_inventory(ns,db)
        current=batch.fetchall(db,'SELECT digest,generation FROM storage_cas_current ORDER BY digest LIMIT 33')
        require(len(current)<=32,'bounded current generation mapping')
        by_id={record.intent.generation:(record,phase) for record,phase in generations}
        for digest,generation in current:
            require(generation in by_id and by_id[generation][0].intent.digest==digest
                    and by_id[generation][1]=='LIVE','current generation ownership changed')
            native_read(ns,readers,by_id[generation][0])
        current_ids={generation for _,generation in current}
        candidates=tuple((record,phase) for record,phase in generations if record.intent.generation not in current_ids)
        # This collects only superseded copies. Historical digest roots and the
        # current copy remain owned, regardless of business root reachability.
        require(all(record.intent.digest in dict(current) for record,_ in candidates),'generation lacks retained current copy')
    if any(phase=='LIVE' for _,phase in candidates):
        with window.transaction():
            for record,phase in candidates:
                if phase=='LIVE':batch.exact_write(db,"UPDATE storage_cas_generations SET phase='RETIRING' WHERE generation=? AND phase='LIVE'",(record.intent.generation,),1)
    for record,_ in candidates:
        native_read(ns,readers,record,purpose=CasPurpose.RETIRE_COMMITTED,remove=True)
    if candidates:
        with window.transaction():
            for record,_ in candidates:
                batch.exact_write(db,"DELETE FROM storage_cas_generations WHERE generation=? AND phase='RETIRING'",(record.intent.generation,),1)
    return dict(status='COLLECTED',count=len(candidates))

def execute(control):
    require(control._accepted and control.request.payload is not None,'accepted operational worker required')
    request=control.request;profile=control.profile;host=control.host
    record=ProjectRecord.from_bytes(control.read(request.slot).head.state.project_bytes)
    ns=ProjectNamespace(host,record,request.project,profile.limits).pin()
    databases=ProjectDatabases(ns,sqlite_library(control.selected),request.slot,profile.schemas,profile=profile)
    databases.open_operational();db=databases.databases['state']
    root=Path(control.selected.require_live().root)
    readers=WindowsCasGenerationLibrary(ctypes.CDLL(str(root/'gitgo-storage-cas_generation.dll'),winmode=0x100|0x800))
    mode=request.mode;payload=parse_payload(request.payload)
    window=None
    if mode in dict(COUNTS):
        count=dict(COUNTS)[mode]
        window=WriteWindow(db,databases.owners['state'],WriteRange(schedule().bound,control._cycle.attempts-count,count))
    if mode=='publish_checkpoint':
        with snapshot(db):
            namespace_inventory(ns,db)
            require(batch.fetchone(db,'SELECT 1 FROM storage_batches LIMIT 1') is None,'previous preparation remains charged')
        facade=SimpleNamespace(_state=db,_state_lock=threading.RLock(),_ensure_open=ns.require_live,
            paths=SimpleNamespace(project_id=request.project),policy=StoragePolicy(),
            _encoded_object=_encoded_object,_register_object=_register_object)
        def read_prior(reference,*,repair=False):
            with snapshot(db):return CommittedReader(ns,db,readers,profile.operational.checkpoint_limits).read_json(reference,repair=repair)
        facade._read_json_blob=read_prior
        with window.attempt():
            publication=plan_agent_checkpoint(facade,payload['snapshot'],limits=profile.operational.checkpoint_limits,
                registration=ProjectRegistration(request.project,payload['workspace_hint'],payload['created_at']))
        event=control.read(request.slot).head.state.value()['event']
        context=BatchContext(request.host,request.project,record.identity.store,request.slot,request.profile,
            request.runtime,control._cycle.epoch,request.request,request.launch,event['pid'],event['created'],
            dict(record.identity.members)['data/cas'])
        writers=WindowsCasWriterLibrary(ctypes.CDLL(str(root/'gitgo-storage-cas_writer.dll'),winmode=0x100|0x800))
        operation=NativeCheckpoint(ns,db,writers,readers,context,window,_protected_checkpoint_catalogue())
        operation.publish(publication)
        for owner in reversed(operation.pins):require(host._release(owner),'publication pin closure incomplete')
        result=dict(status='PUBLICATION_ATTEMPT_FINISHED')
    elif mode=='verify_checkpoint':result=verify_checkpoint(control,ns,db,readers)
    elif mode in {'retire_checkpoint','reconcile_preparation'}:result=retire_checkpoint(control,ns,db,readers,window)
    elif mode in {'collect_obsolete','reconcile_collection'}:result=collect_obsolete(control,ns,db,readers,window)
    elif mode=='read_checkpoint':
        db.execute('PRAGMA query_only=ON')
        with snapshot(db):
            namespace_inventory(ns,db)
            result=dict(status='READ',session=CommittedReader(ns,db,readers,profile.operational.checkpoint_limits).session(payload['process_id']))
    else:
        require(mode=='reset_wal','closed operation required')
        with snapshot(db):
            require(batch.fetchone(db,'SELECT 1 FROM storage_batches LIMIT 1') is None
                    and batch.fetchone(db,'SELECT 1 FROM storage_batch_members LIMIT 1') is None,'preparation remains charged')
            generations,_=namespace_inventory(ns,db)
            current={row[0] for row in batch.fetchall(db,'SELECT generation FROM storage_cas_current LIMIT 33')}
            require(len(current)==len(generations) and all(record.intent.generation in current and phase=='LIVE'
                    for record,phase in generations),'unsettled generation debt remains')
            for record,_ in generations:native_read(ns,readers,record)
        for role in ROLES:
            cursor=databases.databases[role].execute('PRAGMA wal_checkpoint(TRUNCATE)')
            try:require(tuple(cursor.fetchone())==(0,0,0),'quiescent WAL reset is incomplete')
            finally:cursor.close()
            require(databases.owners[role].extent('wal')==0,'known zero WAL extent required')
        result=dict(status='RESET_VERIFIED')
    for owner in reversed(tuple(databases.owners.values())):
        require(host._release(owner),'operational database closure is incomplete')
    ns.verify()
    if mode=='reset_wal':
        from .operational_launch import require_zero_wals
        require_zero_wals(ns)
        prior=control.read(request.slot)
        require(prior.head.state.value()['event']['phase']=='ACCEPTED'
                and prior.head.state.value()['event']['launch']==request.launch,'reset worker changed')
        control.update(prior,prior.head.state.with_cycle(control._cycle.verified()))
    return result
