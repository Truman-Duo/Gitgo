"""Canonical finite envelopes are descriptions, never allocation authority."""
from dataclasses import FrozenInstanceError, replace
import json

import pytest

from backend.core.storage.envelope import (
    FileEnvelope, StorageEnvelope, StorageEnvelopeInvalid, MAX_ACCOUNTED_BYTES,
)


def profile():
    return StorageEnvelope(1,4096,2,32768,(FileEnvelope('registry',1,1),),
                           (FileEnvelope('cas',1,2),FileEnvelope('wal',4096,1)))


def test_each_copy_is_rounded_before_aggregate_reservation():
    value = profile()
    assert value.store_max_bytes == 12288
    assert value.reserved_max_bytes == 28672
    assert StorageEnvelope.from_bytes(value.to_bytes()) == value
    assert StorageEnvelope.from_bytes(value.to_bytes()).digest == value.digest


@pytest.mark.parametrize('field,invalid',[
    ('version',True),('version',2),('allocation_unit',0),('allocation_unit',3000),
    ('allocation_unit',131072),('slots',False),('slots',65),('slots',0),
    ('host_max_bytes',28671),('host_max_bytes',MAX_ACCOUNTED_BYTES+1),
    ('registry_files',()),('registry_files',(FileEnvelope('registry',32768,1),)),
    ('files',[]),('files',()),('files',(FileEnvelope('x',1,1),)*2),
    ('files',(FileEnvelope('wal',1,1),FileEnvelope('cas',1,1))),
])
def test_unknown_or_underfunded_descriptor_is_rejected(field,invalid):
    with pytest.raises(StorageEnvelopeInvalid): replace(profile(),**{field:invalid})


@pytest.mark.parametrize('args',[('A',1,1),('../cas',1,1),('x'*33,1,1),('cas',True,1),
                                 ('cas',-1,1),('cas',1,0),('cas',1,65537)])
def test_unbounded_or_ambiguous_file_classes_refused(args):
    with pytest.raises(StorageEnvelopeInvalid): FileEnvelope(*args)


def test_aggregate_and_rounding_overflow_do_not_wrap():
    with pytest.raises(StorageEnvelopeInvalid):
        StorageEnvelope(1,4096,1,MAX_ACCOUNTED_BYTES,(FileEnvelope('registry',1,1),),
                        (FileEnvelope('x',MAX_ACCOUNTED_BYTES,1),))
    with pytest.raises(StorageEnvelopeInvalid):
        StorageEnvelope(1,4096,64,MAX_ACCOUNTED_BYTES,(FileEnvelope('registry',1,1),),
                        (FileEnvelope('x',1<<40,65536),))


def test_canonical_bytes_and_nested_unknown_fields_are_required():
    value = json.loads(profile().to_bytes())
    with pytest.raises(StorageEnvelopeInvalid): StorageEnvelope.from_bytes(json.dumps(value).encode())
    value['files'][0]['grant'] = 'unlimited'
    with pytest.raises(StorageEnvelopeInvalid):
        StorageEnvelope.from_bytes(json.dumps(value,sort_keys=True,separators=(',',':')).encode())


@pytest.mark.parametrize('raw',[b'',b'x'*8193,b'[]',b'{}',b'NaN',b'null',b'\xff',
    b'{"version":1,"version":1}',b'['*1000+b']'*1000])
def test_invalid_duplicate_nonfinite_and_deep_encodings_are_refused(raw):
    with pytest.raises(StorageEnvelopeInvalid): StorageEnvelope.from_bytes(raw)


def test_snapshot_is_immutable_and_policy_change_has_new_digest():
    value = profile()
    with pytest.raises(FrozenInstanceError): value.slots = 1
    with pytest.raises(FrozenInstanceError): value.files[0].max_files = 1
    assert replace(value,slots=1).digest != value.digest
    assert replace(value,host_max_bytes=65536).digest != value.digest


def test_registry_files_are_individually_rounded_too():
    value = replace(profile(),host_max_bytes=65536,
                    registry_files=(FileEnvelope('gates',1,2),FileEnvelope('records',4097,2)))
    assert value.registry_max_bytes == 24576
    assert value.reserved_max_bytes == 49152
    with pytest.raises(StorageEnvelopeInvalid): replace(value,host_max_bytes=49151)
    assert StorageEnvelope.from_bytes(value.to_bytes()) == value


@pytest.mark.parametrize('registry_files',[
    [FileEnvelope('registry',1,1)],(FileEnvelope('x',1,1),)*2,
    (FileEnvelope('z',1,1),FileEnvelope('a',1,1)),('unbounded',),
])
def test_registry_cannot_skip_file_class_validation(registry_files):
    with pytest.raises(StorageEnvelopeInvalid): replace(profile(),registry_files=registry_files)


def test_unknown_registry_fields_and_old_aggregate_only_shape_are_refused():
    value = json.loads(profile().to_bytes())
    value['registry_files'][0]['default'] = 'allow'
    with pytest.raises(StorageEnvelopeInvalid):
        StorageEnvelope.from_bytes(json.dumps(value,sort_keys=True,separators=(',',':')).encode())
    del value['registry_files']
    value['registry_max_bytes'] = 4096
    with pytest.raises(StorageEnvelopeInvalid):
        StorageEnvelope.from_bytes(json.dumps(value,sort_keys=True,separators=(',',':')).encode())
