from dataclasses import replace
import hashlib
import uuid

import pytest
from backend.core.storage import slot_records as c
from backend.core.storage import journal_cells as framing


def reservation():
    body=dict(version=1,registry_id=str(uuid.uuid4()),envelope='a'*64,slot=0,
        project=str(uuid.uuid4()),fingerprint='b'*64)
    return c.WorkerSlotState(c.canonical(dict(body=body,checksum=hashlib.sha256(c.canonical(body)).hexdigest())))


def event():
    return dict(phase='INTENT',launch=str(uuid.uuid4()),pid=0,created=0,parent_pid=42,parent_created=123,
        profile='c'*64,mode='publish',request=str(uuid.uuid4()),generation=0,runtime=dict(generation=str(uuid.uuid4()),manifest='d'*64,epoch=1))


def applied(view,state):
    update=c.FORMAT.next_update(view,state);update.require_prior(view)
    begin=update.target*framing.CELL_BYTES
    changed=view.raw[:begin]+update.encoded+view.raw[begin+framing.CELL_BYTES:]
    result=c.FORMAT.read(changed);update.require_result(result)
    return result,update


def initial():return applied(c.FORMAT.read(bytes(framing.SLOT_BYTES)),reservation())[0]


def test_thousand_workers_have_fixed_slot_size_and_monotone_sequence():
    view=initial();state=view.head.state
    for number in range(1000):
        value=event();value['generation']=number
        for phase in ('INTENT','CREATED','ACCEPTED','RETIRED'):
            if phase=='CREATED':value=dict(value,pid=43,created=124)
            value=dict(value,phase=phase)
            view,_=applied(view,state.with_event(value))
            assert len(view.raw)==8192
        assert view.head.sequence==1+4*(number+1)
    assert view.head.sequence==4001


def test_every_torn_prefix_preserves_unknown_or_exact_previous_complete_cell():
    view=initial();state=view.head.state;intent=event()
    view,_=applied(view,state.with_event(intent))
    update=c.FORMAT.next_update(view,state.with_event(dict(intent,phase='CREATED',pid=43,created=124)))
    offset=update.target*framing.CELL_BYTES
    total=0
    for count in range(1,framing.CELL_BYTES):
        torn=view.raw[:offset]+update.encoded[:count]+view.raw[offset+count:]
        try:observed=c.FORMAT.read(torn)
        except framing.JournalUnknown:
            total+=1;continue
        # A prefix may be byte-for-byte identical to old or completed new data.
        # No other parse is allowed, and an old parse cannot confirm the update.
        if observed.raw==view.raw:
            with pytest.raises(framing.JournalUnknown):update.require_result(observed)
        else:
            assert observed.raw[offset:offset+framing.CELL_BYTES]==update.encoded
            update.require_result(observed)
    assert total>4000


def test_corrupt_later_intent_never_falls_back_to_reusable_retired_state():
    view=initial();state=view.head.state;value=event()
    view,_=applied(view,state.with_event(value))
    view,_=applied(view,state.with_event(dict(value,phase='RETIRED')))
    view,_=applied(view,state.with_event(event()))
    head=view.head.index*framing.CELL_BYTES
    for offset in (0,8,16,24,56,100,2000,4064,4095):
        damaged=bytearray(view.raw);damaged[head+offset]^=1
        with pytest.raises(framing.JournalUnknown):c.FORMAT.read(bytes(damaged))


def test_zeroed_peer_is_unknown_after_initial_reservation():
    view=initial();view,_=applied(view,view.head.state.with_event(event()))
    damaged=bytearray(view.raw)
    offset=view.peer.index*framing.CELL_BYTES
    damaged[offset:offset+framing.CELL_BYTES]=bytes(framing.CELL_BYTES)
    with pytest.raises(framing.JournalUnknown,match='missing peer'):c.FORMAT.read(bytes(damaged))


def test_stale_view_cannot_publish_or_confirm_another_worker():
    prior=initial();update=c.FORMAT.next_update(prior,prior.head.state.with_event(event()))
    actual,_=applied(prior,prior.head.state.with_event(event()))
    with pytest.raises(framing.JournalUnknown,match='stale'):update.require_prior(actual)
    with pytest.raises(framing.JournalUnknown,match='unconfirmed'):update.require_result(actual)


def test_reservation_and_exact_created_identity_cannot_change():
    view=initial();state=view.head.state;value=event()
    view,_=applied(view,state.with_event(value))
    with pytest.raises(framing.JournalUnknown):c.FORMAT.next_update(view,reservation().with_event(dict(value,phase='CREATED',pid=43,created=124)))
    created=dict(value,phase='CREATED',pid=43,created=124)
    view,_=applied(view,state.with_event(created))
    with pytest.raises(framing.JournalUnknown):c.FORMAT.next_update(view,state.with_event(dict(created,phase='ACCEPTED',created=125)))
    with pytest.raises(framing.JournalUnknown):c.FORMAT.next_update(view,state.with_event(dict(created,phase='RETIRED',request=str(uuid.uuid4()))))


@pytest.mark.parametrize('change',[
    dict(phase='ACCEPTED'),dict(pid=43),dict(created=124),dict(parent_pid=0),dict(generation=-1),
    dict(generation=True),dict(mode='unknown/path'),dict(phase=[]),dict(pid=42,created=124),
])
def test_invalid_worker_identity_and_phase_data_refuse(change):
    with pytest.raises(framing.JournalUnknown):reservation().with_event(dict(event(),**change))


def test_replay_or_erasure_of_worker_debt_refuses():
    view=initial();state=view.head.state;value=event()
    view,_=applied(view,state.with_event(value))
    with pytest.raises(framing.JournalUnknown):c.FORMAT.next_update(view,state)
    with pytest.raises(framing.JournalUnknown):c.FORMAT.next_update(view,state.with_event(value))
    view,_=applied(view,state.with_event(dict(value,phase='RETIRED')))
    with pytest.raises(framing.JournalUnknown):c.FORMAT.next_update(view,state.with_event(value))


def test_sequence_exhaustion_has_no_wraparound():
    view=initial()
    view=replace(view,head=replace(view.head,sequence=framing.MAX_SEQUENCE))
    with pytest.raises(framing.JournalUnknown):c.FORMAT.next_update(view,view.head.state.with_event(event()))


@pytest.mark.parametrize('raw',[b'',bytes(8191),bytes(8193)],ids=['empty','short','long'])
def test_slot_size_must_match_prepaid_native_family(raw):
    with pytest.raises(framing.JournalUnknown):c.FORMAT.read(raw)


def test_worker_intent_requires_selected_runtime_lease():
    value=event();del value['runtime']
    with pytest.raises(framing.JournalUnknown):reservation().with_event(value)


@pytest.mark.parametrize('field,value',[('generation',str(uuid.uuid4())),('manifest','e'*64),('epoch',2)])
def test_selected_runtime_cannot_change_between_intent_and_creation(field,value):
    view=initial();state=view.head.state;intent=event()
    view,_=applied(view,state.with_event(intent))
    changed=dict(intent,phase='CREATED',pid=43,created=124,runtime=dict(intent['runtime']))
    changed['runtime'][field]=value
    with pytest.raises(framing.JournalUnknown):c.FORMAT.next_update(view,state.with_event(changed))


def test_forged_snapshot_metadata_is_not_a_committed_transition_basis():
    view=initial();changed=replace(view,head=replace(view.head,state=reservation()))
    with pytest.raises(framing.JournalUnknown,match='snapshot'):
        c.FORMAT.next_update(changed,changed.head.state.with_event(event()))


def test_bootstrap_cell_in_wrong_physical_position_is_refused():
    view=initial()
    swapped=view.raw[4096:]+view.raw[:4096]
    with pytest.raises(framing.JournalUnknown,match='position'):c.FORMAT.read(swapped)


def test_runtime_slot_cannot_be_reserved_as_a_project_slot():
    value=reservation().value()['reservation'];value['body']['slot']=63
    value['checksum']=hashlib.sha256(c.canonical(value['body'])).hexdigest()
    with pytest.raises(framing.JournalUnknown):c.WorkerSlotState(c.canonical(value))
