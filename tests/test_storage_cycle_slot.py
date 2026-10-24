from dataclasses import replace
import uuid
import pytest
from backend.core.sandbox_read import ObjectIdentity
from backend.core.storage import slot_records as s,journal_cells as framing
from backend.core.storage.cycle_ledger import start,CycleLedger
from backend.core.storage.project_records import ProjectIdentity,ProjectRecord,MEMBERS
from backend.core.storage.models import StorageBlocked
from tests.test_storage_slot_records import reservation,event
from tests.test_storage_windows_journal import native,fixture


def ready_sequence():
    states=[reservation()]
    value=dict(event(),mode='initialize',profile='b'*64)
    for phase in ('INTENT','CREATED','ACCEPTED'):
        if phase=='CREATED':value=dict(value,pid=43,created=124)
        value=dict(value,phase=phase);states.append(states[-1].with_event(value))
    identity=ProjectIdentity(value['request'],ObjectIdentity('win32',2**64-1,2**128-1))
    building=ProjectRecord('BUILDING','b'*64,identity)
    states.append(states[-1].with_project(building))
    identity=replace(identity,members=tuple((name,ObjectIdentity('win32',identity.root.volume,identity.root.file_id-i-1)) for i,name in enumerate(MEMBERS)))
    record=replace(building,phase='STRUCTURED',identity=identity)
    for phase in ('STRUCTURED','MATERIALIZED'):
        states.append(states[-1].with_project(replace(record,phase=phase)))
    states.append(states[-1].with_event(dict(value,phase='RETIRED')))
    value=dict(value,mode='verify_initialize',request=str(uuid.uuid4()),launch=str(uuid.uuid4()),pid=0,created=0)
    for phase in ('INTENT','CREATED','ACCEPTED'):
        if phase=='CREATED':value=dict(value,pid=44,created=125)
        value=dict(value,phase=phase);states.append(states[-1].with_event(value))
    states.append(states[-1].with_project(replace(record,phase='VERIFIED')))
    states.append(states[-1].with_event(dict(value,phase='RETIRED')).with_project(replace(record,phase='READY')))
    return states


def candidate(state):
    ledger=start(None,str(uuid.uuid4()),'e'*64)
    value=dict(state.value()['event'],mode='publish_checkpoint',request=ledger.request,
               launch=str(uuid.uuid4()),phase='INTENT',pid=0,created=0)
    return ledger,value,state.with_event(value).with_cycle(ledger)


def test_write_intent_without_debit_and_debt_erasure_are_refused():
    state=ready_sequence()[-1]
    ledger,value,after=candidate(state)
    with pytest.raises(StorageBlocked):s.transition(state,state.with_event(value))
    s.transition(state,after)
    with pytest.raises(StorageBlocked):s.transition(after,state.with_event(dict(value,phase='RETIRED')))
    for phase in ('CREATED','ACCEPTED','RETIRED'):
        value=dict(value,phase=phase,pid=45,created=126)
        next_=after.with_event(value);s.transition(after,next_);after=next_
    fresh=dict(value,phase='INTENT',pid=0,created=0,mode='retire_checkpoint',launch=str(uuid.uuid4()))
    with pytest.raises(StorageBlocked):s.transition(after,after.with_event(fresh))
    next_=after.with_event(fresh).with_cycle(ledger.consume('retire_checkpoint'))
    s.transition(after,next_)


def test_complete_native_slot_reopens_with_debit_and_identity(native,tmp_path_factory):
    with fixture(native,tmp_path_factory) as (library,parent,parent_id,file_id,path):
        owner=library.reserve(s.FORMAT);owner.open(parent,0,parent_id,file_id,initialize=True)
        view=owner.read()
        for state in ready_sequence():view=owner.update(view,state)
        ledger,value,state=candidate(view.head.state)
        view=owner.update(view,state)
        owner.close()
        observed=library.reserve(s.FORMAT);observed.open(parent,0,parent_id,file_id)
        actual=observed.read()
        assert actual.head.state==state
        assert CycleLedger.from_bytes(actual.head.state.cycle_bytes).attempts==19
        assert actual.head.state.value()['event']['phase']=='INTENT'
        assert len(actual.raw)==8192 and len(framing.canonical(state.value()))<=framing.PAYLOAD_BYTES
        observed.close()


def test_torn_cycle_debit_cannot_fall_back_to_reusable_ready_slot():
    view=s.FORMAT.read(bytes(framing.SLOT_BYTES))
    for state in ready_sequence():
        update=s.FORMAT.next_update(view,state)
        offset=update.target*framing.CELL_BYTES
        view=s.FORMAT.read(view.raw[:offset]+update.encoded+view.raw[offset+framing.CELL_BYTES:])
    _,_,state=candidate(view.head.state)
    update=s.FORMAT.next_update(view,state);offset=update.target*framing.CELL_BYTES
    damaged=bytearray(view.raw[:offset]+update.encoded+view.raw[offset+framing.CELL_BYTES:])
    damaged[offset+1500]^=1
    with pytest.raises(framing.JournalUnknown):s.FORMAT.read(bytes(damaged))
