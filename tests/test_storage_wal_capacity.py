"""Aggregate capacity must cover all retained work, not just one small pass."""
from dataclasses import FrozenInstanceError, replace
from itertools import permutations

import pytest

from backend.core.storage.envelope import StorageEnvelopeInvalid, MAX_ACCOUNTED_BYTES
from backend.core.storage.wal_capacity import WalFrameBound, WalDebt, WalSchedule


def schedule():
    return WalSchedule(WalFrameBound(4096,256,65536),4152,
        (WalDebt('checkpoint',2,3,2),WalDebt('fact_retirement',1,0,2)),1)


def test_every_debt_and_housekeeping_are_charged_after_business_success():
    value = schedule()
    assert value.attempts == 13
    assert value.wal_bytes == 4152+13*272*4120
    assert value.bound.main_bytes == 1048576
    assert value.shm_bytes == 32768
    reduced = replace(value,debts=(WalDebt('checkpoint',1,3,2),WalDebt('fact_retirement',1,0,2)))
    assert value.wal_bytes-reduced.wal_bytes == 5*value.bound.transaction_bytes


def test_any_order_of_all_cleanup_and_business_attempts_fits_without_reset():
    value = WalSchedule(WalFrameBound(4096,256,4096),100001,
        (WalDebt('checkpoint',1,2,2),),1)
    # Charge worst-case growth even for failed/unknown transaction attempts.
    attempts = [('business',True),('business',False),('cleanup',True),('cleanup',False),('housekeeping',True)]
    for order in permutations(attempts):
        highwater = value.retained_wal_bytes
        for operation, confirmed in order:
            highwater += value.bound.transaction_bytes
            assert highwater <= value.wal_bytes
        assert highwater == value.wal_bytes


@pytest.mark.parametrize('page',[512,1024,4096,65536])
@pytest.mark.parametrize('sector',[1,512,4096,65536])
def test_sector_padding_bound_covers_every_possible_sector_alignment(page,sector):
    bound = WalFrameBound(page,8,sector)
    for offset_in_sector in range(sector):
        needed = (sector-offset_in_sector)%sector
        actual_frames = (needed+bound.frame_bytes-1)//bound.frame_bytes
        assert actual_frames <= bound.padding_frames


@pytest.mark.parametrize('frames,blocks',[(0,1),(1,1),(4062,1),(4063,2),(8158,2),(8159,3)])
def test_shm_accounts_for_retained_partial_frames_and_each_index_boundary(frames,blocks):
    # Current schedule contributes exactly two one-page frames, no padding.
    bound = WalFrameBound(4096,1,1)
    remaining = max(0,frames-2)
    value = WalSchedule(bound,32+remaining*4120,(WalDebt('cleanup',1,0,1),),1)
    assert value.shm_bytes == blocks*32768
    if remaining:
        partial = replace(value,retained_wal_bytes=value.retained_wal_bytes-1)
        assert partial.max_frames == value.max_frames


@pytest.mark.parametrize('kwargs',[
    dict(page_size=True),dict(page_size=0),dict(page_size=3000),dict(page_size=256),
    dict(page_size=131072),dict(max_pages=0),dict(max_pages=0xffffffff),
    dict(max_sector_size=0),dict(max_sector_size=8193),dict(max_sector_size=131072),
])
def test_unqualified_frame_shapes_refuse(kwargs):
    with pytest.raises(StorageEnvelopeInvalid): replace(schedule().bound,**kwargs)


@pytest.mark.parametrize('kwargs',[
    dict(operation=''),dict(operation='../x'),dict(operation='x'*65),dict(copies=True),
    dict(copies=0),dict(copies=65537),dict(business_attempts=-1),dict(cleanup_attempts=0),
    dict(cleanup_attempts=False),dict(business_attempts=MAX_ACCOUNTED_BYTES),
])
def test_missing_cleanup_or_invalid_debt_is_not_admitted(kwargs):
    with pytest.raises(StorageEnvelopeInvalid): replace(WalDebt('checkpoint',1,2,3),**kwargs)


@pytest.mark.parametrize('kwargs',[
    dict(retained_wal_bytes=-1),dict(retained_wal_bytes=True),dict(retained_wal_bytes=31),
    dict(retained_wal_bytes=MAX_ACCOUNTED_BYTES),dict(debts=[]),dict(debts=()),
    dict(debts=('checkpoint',)),dict(debts=(WalDebt('x',1,1,1),)*2),
    dict(debts=(WalDebt('z',1,1,1),WalDebt('a',1,1,1))),
    dict(housekeeping_attempts=0),dict(housekeeping_attempts=False),
    dict(housekeeping_attempts=1<<32),dict(bound=None),
])
def test_underdescribed_or_overflowed_schedule_refuses(kwargs):
    with pytest.raises(StorageEnvelopeInvalid): replace(schedule(),**kwargs)


def test_native_classes_include_aggregate_wal_and_shm_but_are_not_a_full_envelope():
    value = schedule()
    classes = value.file_classes('state')
    assert [item.name for item in classes] == ['state_main','state_shm','state_wal']
    assert classes[-1].max_bytes == value.wal_bytes
    assert all(item.max_files == 1 for item in classes)
    assert not any('journal' in item.name for item in classes)
    with pytest.raises(StorageEnvelopeInvalid): value.file_classes('../state')


def test_descriptor_is_immutable_and_cannot_refund_or_restart_an_epoch():
    value = schedule()
    with pytest.raises(FrozenInstanceError): value.retained_wal_bytes = 0
    with pytest.raises(FrozenInstanceError): value.debts[0].cleanup_attempts = 0
    assert not any(hasattr(value,name) for name in ('refund','reset','reserve','execute','commit'))
