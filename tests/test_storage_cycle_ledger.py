"""Fail-closed serial debt bookkeeping; native admission remains separate."""
from dataclasses import replace
import uuid
import pytest
from backend.core.storage import StorageBlocked
from backend.core.storage.cycle_ledger import CycleLedger,start,transition,schedule,COUNTS


def event(ledger,mode='publish_checkpoint',phase='INTENT'):
    return dict(request=ledger.request,mode=mode,phase=phase)


def test_whole_publication_debit_precedes_native_intent():
    ledger=start(None,str(uuid.uuid4()),'a'*64)
    transition(None,ledger,None,event(ledger))
    assert ledger.attempts==19
    assert CycleLedger.from_bytes(ledger.to_bytes())==ledger
    with pytest.raises(StorageBlocked):transition(None,ledger,None,event(ledger,phase='ACCEPTED'))


@pytest.mark.parametrize('mode',[mode for mode,_ in COUNTS])
def test_each_whole_worker_range_is_consumed_once_without_refund(mode):
    ledger=start(None,str(uuid.uuid4()),'a'*64)
    if mode=='reconcile_preparation':ledger=ledger.consume('retire_checkpoint')
    if mode=='reconcile_collection':ledger=ledger.consume('collect_obsolete')
    if mode!='publish_checkpoint':
        after=ledger.consume(mode)
        transition(ledger,after,event(ledger,phase='RETIRED'),event(ledger,mode))
        assert after.attempts-ledger.attempts==dict(COUNTS)[mode]
        ledger=after
    with pytest.raises(StorageBlocked):ledger.consume(mode)
    with pytest.raises(StorageBlocked):start(ledger,str(uuid.uuid4()),'b'*64)


def test_new_epoch_requires_retired_reset_and_carries_complete_future_schedule():
    ledger=start(None,str(uuid.uuid4()),'a'*64).consume('retire_checkpoint')
    verified=ledger.verified()
    with pytest.raises(StorageBlocked):transition(ledger,verified,None,event(ledger,'publish_checkpoint','ACCEPTED'))
    transition(ledger,verified,None,event(ledger,'reset_wal','ACCEPTED'))
    ledger=verified
    settled=ledger.settle()
    with pytest.raises(StorageBlocked):transition(ledger,settled,None,event(ledger,'reset_wal','ACCEPTED'))
    transition(ledger,settled,None,event(ledger,'reset_wal','RETIRED'))
    next_=start(settled,str(uuid.uuid4()),'b'*64)
    assert next_.epoch==2 and next_.attempts==19
    assert schedule().attempts==sum(count for _,count in COUNTS)+1
    assert schedule().wal_bytes>schedule().bound.main_bytes*27
    with pytest.raises(StorageBlocked):start(settled,settled.request,'b'*64)


def test_future_debt_cannot_be_erased_or_counter_wrapped():
    ledger=start(None,str(uuid.uuid4()),'a'*64)
    with pytest.raises(StorageBlocked):ledger.settle()
    with pytest.raises(StorageBlocked):replace(ledger,used=())
    terminal=replace(ledger.consume('retire_checkpoint').verified().settle(),epoch=(1<<63)-1)
    with pytest.raises(StorageBlocked):start(terminal,str(uuid.uuid4()),'b'*64)


def test_changed_input_or_request_cannot_replace_an_active_cycle():
    ledger=start(None,str(uuid.uuid4()),'a'*64)
    after=ledger.consume('retire_checkpoint')
    for changed in (replace(after,input_digest='b'*64),replace(after,request=str(uuid.uuid4()))):
        with pytest.raises(StorageBlocked):transition(ledger,changed,event(ledger,phase='RETIRED'),event(changed,'retire_checkpoint'))
