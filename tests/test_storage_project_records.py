"""Closed project facts and request contracts, independent of OS admission."""
from dataclasses import replace
import json,sqlite3,uuid

import pytest
from backend.core.sandbox_read import ObjectIdentity
from backend.core.storage import journal_cells as j,slot_records as s
from backend.core.storage.models import StorageBlocked
from backend.core.storage.project_records import ProjectIdentity,ProjectRecord,MEMBERS,PHASES
from backend.core.storage.project_profile import ProjectProfile
from backend.core.storage.project_control import ProjectRequest
from backend.core.storage.native_windows_sqlite import SqliteFileLimits
from backend.core.storage.runtime_lifecycle import RuntimeReference
from tests.test_storage_slot_records import reservation,event

def sequence():
    state=reservation()
    value=dict(event(),mode='initialize',profile='b'*64)
    for phase in ('INTENT','CREATED','ACCEPTED'):
        if phase=='CREATED':value=dict(value,pid=43,created=124)
        value=dict(value,phase=phase);next_state=state.with_event(value);s.transition(state,next_state);state=next_state
    root=ObjectIdentity('win32',2**64-1,2**128-1)
    building=ProjectRecord('BUILDING','b'*64,ProjectIdentity(value['request'],root))
    structured=replace(building,phase='STRUCTURED',identity=replace(building.identity,members=
        tuple((name,ObjectIdentity('win32',root.volume,root.file_id-i-1)) for i,name in enumerate(MEMBERS))))
    return state,building,structured

def advance(state,record):
    result=state.with_project(record);s.transition(state,result);return result

def materialized():
    state,building,structured=sequence()
    state=advance(state,building);state=advance(state,structured)
    return advance(state,replace(structured,phase='MATERIALIZED'))

def test_initializer_cannot_supply_independent_verification_or_readiness():
    state=materialized();record=ProjectRecord.from_bytes(state.project_bytes)
    for phase in ('VERIFIED','READY'):
        with pytest.raises(StorageBlocked):advance(state,replace(record,phase=phase))
    retired=state.with_event(dict(state.value()['event'],phase='RETIRED'));s.transition(state,retired)
    with pytest.raises(StorageBlocked):advance(retired,replace(record,phase='READY'))

def test_separate_verifier_and_confirmed_retirement_are_both_required():
    state=materialized();record=ProjectRecord.from_bytes(state.project_bytes)
    retired=state.with_event(dict(state.value()['event'],phase='RETIRED'));s.transition(state,retired);state=retired
    verification=dict(state.value()['event'],mode='verify_initialize',launch=str(uuid.uuid4()),request=str(uuid.uuid4()),pid=0,created=0)
    for phase in ('INTENT','CREATED','ACCEPTED'):
        if phase=='CREATED':verification=dict(verification,pid=44,created=125)
        verification=dict(verification,phase=phase)
        next_state=state.with_event(verification);s.transition(state,next_state);state=next_state
    state=advance(state,replace(record,phase='VERIFIED'))
    with pytest.raises(StorageBlocked):advance(state,replace(record,phase='READY'))
    ready=state.with_event(dict(verification,phase='RETIRED')).with_project(replace(record,phase='READY'))
    s.transition(state,ready)
    assert s._parse_state(j.canonical(ready.value()))==ready

@pytest.mark.parametrize('case',['erase','root','members','profile','skip','repeat'])
def test_project_facts_cannot_be_erased_rebound_or_skipped(case):
    state,building,structured=sequence();state=advance(state,building)
    if case=='erase':candidate=s.WorkerSlotState(state.reservation_bytes,state.event_bytes)
    elif case=='root':candidate=state.with_project(replace(building,identity=replace(building.identity,root=ObjectIdentity('win32',2**64-1,99))))
    elif case=='members':candidate=state.with_project(replace(structured,identity=replace(structured.identity,store=str(uuid.uuid4()))))
    elif case=='profile':
        with pytest.raises(StorageBlocked):state.with_project(replace(building,profile='c'*64))
        return
    elif case=='skip':candidate=state.with_project(replace(structured,phase='MATERIALIZED'))
    else:candidate=state
    with pytest.raises(StorageBlocked):s.transition(state,candidate)

def test_complete_record_fits_existing_cell_without_changing_legacy_payload():
    original=reservation()
    assert set(original.value())=={'reservation','event'} and original.project_bytes is None
    state=materialized();record=ProjectRecord.from_bytes(state.project_bytes)
    value=dict(state.value()['event'],phase='RETIRED',pid=2**32-1,parent_pid=2**32-2,
        created=2**64-1,parent_created=2**64-1,generation=2**63-1,mode='a'*32,
        runtime=dict(state.value()['event']['runtime'],epoch=2**63-1))
    for phase in PHASES[1:]:
        candidate=state.with_project(replace(record,phase=phase)).with_event(value)
        raw=j.canonical(candidate.value())
        assert len(raw)<=j.PAYLOAD_BYTES and s._parse_state(raw)==candidate

@pytest.mark.parametrize('kind',['duplicate','alias','volume','incomplete'])
def test_native_identity_map_is_complete_disjoint_and_one_volume(kind):
    _,_,record=sequence();identity=record.identity;members=identity.members
    if kind=='duplicate':members=(members[0],)*len(members)
    elif kind=='alias':members=tuple((name,identity.root) for name,_ in members)
    elif kind=='volume':members=tuple((name,replace(value,volume=1)) for name,value in members)
    else:members=members[:-1]
    with pytest.raises(StorageBlocked):replace(identity,members=members)

def profile():
    limits=SqliteFileLimits(4096,65536,4096,32768)
    return ProjectProfile(sqlite3.sqlite_version,(limits,limits),('a'*64,'b'*64))

@pytest.mark.parametrize('field,value',[('operations',['initialize','publish']),('spill',True),('page_size',8192),('version',2),('sqlite','0.0.0'),('unknown',True)])
def test_profile_never_acquires_unqualified_operations_or_settings(field,value):
    source=profile();raw=source.to_bytes();assert ProjectProfile.from_bytes(raw)==source
    modified=json.loads(raw);modified[field]=value
    with pytest.raises(StorageBlocked):ProjectProfile.from_bytes(j.canonical(modified))

def test_request_roundtrip_binds_one_selected_runtime_and_closed_mode():
    reference=RuntimeReference(str(uuid.uuid4()),'a'*64,1)
    request=ProjectRequest('initialize','b'*64,0,str(uuid.uuid4()),profile().sha256,str(uuid.uuid4()),str(uuid.uuid4()),reference)
    assert ProjectRequest.from_bytes(request.to_bytes())==request
    for mode in ('publish','shell','verify'):
        with pytest.raises(StorageBlocked):replace(request,mode=mode)
    raw=json.loads(request.to_bytes());raw['runtime']['path']='untrusted'
    with pytest.raises(StorageBlocked):ProjectRequest.from_bytes(j.canonical(raw))
