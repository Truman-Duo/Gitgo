"""Binary-envelope tests; no Windows APIs, credentials, or ACL changes."""
from dataclasses import FrozenInstanceError, replace
import struct

import pytest

from backend.core.windows_security_descriptor import (
    MAX_DESCRIPTOR_BYTES,
    SE_DACL_PRESENT,
    SE_SACL_PRESENT,
    SE_SELF_RELATIVE,
    SecurityDescriptor,
)


SID = bytes.fromhex('010100000000000512000000')  # S-1-5-18
OTHER_SID = bytes.fromhex('010100000000000513000000')
ALLOW = struct.pack('<BBHI', 0, 3, 20, 0x120089) + SID
DENY = struct.pack('<BBHI', 1, 0, 20, 0x40000) + OTHER_SID
LABEL = struct.pack('<BBHI', 17, 3, 20, 1) + bytes.fromhex('010100000000001000200000')


def acl(*aces, revision=2, padding=b''):
    body = b''.join(aces) + padding
    return struct.pack('<BBHHH', revision, 0, 8 + len(body), len(aces), 0) + body


def header(control=SE_SELF_RELATIVE, *, owner=0, group=0, sacl=0, dacl=0,
           revision=1, rm=0):
    return struct.pack('<BBHIIII', revision, rm, control, owner, group, sacl, dacl)


def descriptor(*, dacl=acl(ALLOW, DENY), sacl=acl(LABEL), control=0xFFFF):
    return SecurityDescriptor(control, 123, SID, OTHER_SID, sacl, dacl)


def test_round_trip_preserves_all_control_bits_rm_byte_and_ordered_aces():
    original = descriptor()
    parsed = SecurityDescriptor.from_bytes(original.to_bytes())
    assert parsed == original
    assert parsed.control == 0xFFFF
    assert parsed.rm_control == 123
    assert parsed.dacl == acl(ALLOW, DENY)
    assert parsed.sacl == acl(LABEL)
    assert parsed.to_bytes() == original.to_bytes()


def test_serialization_has_deterministic_offsets_and_no_alias_dependency():
    shared = header(owner=20, group=20) + SID
    parsed = SecurityDescriptor.from_bytes(shared)
    expected = header(owner=20, group=32) + SID + SID
    assert parsed.to_bytes() == expected
    assert parsed.owner == parsed.group == SID


def test_native_layout_padding_and_shared_sid_do_not_affect_comparison():
    dacl = acl(ALLOW)
    raw = (header(SE_SELF_RELATIVE | SE_DACL_PRESENT, owner=60, group=60, dacl=24)
           + b'\xff' * 4 + dacl + b'\xaa' * 8 + SID + b'\xbb' * 16)
    parsed = SecurityDescriptor.from_bytes(raw)
    canonical = SecurityDescriptor(SE_SELF_RELATIVE | SE_DACL_PRESENT,
                                   0, SID, SID, None, dacl)
    assert parsed.comparison_key() == canonical.comparison_key()
    assert parsed.to_bytes() == canonical.to_bytes()


def test_component_buffers_are_copied_to_keep_descriptor_immutable():
    mutable_sid = bytearray(SID)
    mutable_acl = bytearray(acl(ALLOW))
    parsed = SecurityDescriptor(SE_SELF_RELATIVE | SE_DACL_PRESENT,
                                0, mutable_sid, None, None, memoryview(mutable_acl))
    mutable_sid[-1] = 8
    mutable_acl[-1] = 8
    assert parsed.owner == SID
    assert parsed.dacl == acl(ALLOW)
    with pytest.raises(FrozenInstanceError):
        parsed.control = 0


def test_absent_null_and_empty_dacl_remain_distinct():
    variants = [SecurityDescriptor(SE_SELF_RELATIVE, 0, None, None, None, None),
                SecurityDescriptor(SE_SELF_RELATIVE | SE_DACL_PRESENT,
                                   0, None, None, None, None),
                SecurityDescriptor(SE_SELF_RELATIVE | SE_DACL_PRESENT,
                                   0, None, None, None, acl())]
    assert len({sd.comparison_key() for sd in variants}) == 3
    assert [SecurityDescriptor.from_bytes(sd.to_bytes()) for sd in variants] == variants


def test_only_zero_ace_sacl_presence_is_normalized():
    absent = SecurityDescriptor(SE_SELF_RELATIVE, 0, None, None, None, None)
    null = replace(absent, control=absent.control | SE_SACL_PRESENT)
    empty = replace(null, sacl=acl())
    empty_with_padding = replace(empty, sacl=acl(revision=4, padding=b'\xaa' * 16))
    assert len({sd.comparison_key() for sd in (absent, null, empty, empty_with_padding)}) == 1
    assert absent.to_bytes() != null.to_bytes() != empty.to_bytes()
    label = replace(null, sacl=acl(LABEL))
    assert label.comparison_key() != absent.comparison_key()


@pytest.mark.parametrize('bit', [1 << bit for bit in range(15) if bit != 4])
def test_every_other_control_bit_remains_significant_even_with_empty_sacl(bit):
    original = SecurityDescriptor(SE_SELF_RELATIVE | SE_SACL_PRESENT,
                                  0, None, None, acl(), None)
    assert original.comparison_key() != replace(
        original, control=original.control ^ bit).comparison_key()


@pytest.mark.parametrize('bit', [1 << bit for bit in range(15) if bit not in (2, 4)])
def test_control_bits_with_populated_acls_remain_significant(bit):
    original = descriptor()
    assert original.comparison_key() != replace(
        original, control=original.control ^ bit).comparison_key()


def test_resource_manager_byte_and_sid_identity_are_significant():
    original = descriptor()
    for changed in (replace(original, rm_control=122),
                    replace(original, owner=OTHER_SID),
                    replace(original, group=SID)):
        assert changed.comparison_key() != original.comparison_key()


def test_acl_unused_allocation_bytes_are_ignored_but_preserved_on_serialization():
    original = descriptor(dacl=acl(ALLOW), sacl=acl(LABEL))
    padded = replace(original, dacl=acl(ALLOW, padding=b'\xab' * 12),
                     sacl=acl(LABEL, padding=b'\xcd' * 4))
    assert original.comparison_key() == padded.comparison_key()
    assert SecurityDescriptor.from_bytes(padded.to_bytes()).dacl == padded.dacl
    assert original.to_bytes() != padded.to_bytes()


@pytest.mark.parametrize('field', ['dacl', 'sacl'])
def test_acl_revision_remains_significant_with_aces(field):
    original = descriptor()
    revised = bytes([4]) + getattr(original, field)[1:]
    assert original.comparison_key() != replace(original, **{field: revised}).comparison_key()


def test_empty_dacl_revision_remains_significant():
    original = descriptor(dacl=acl())
    assert original.comparison_key() != replace(original, dacl=acl(revision=4)).comparison_key()


@pytest.mark.parametrize('flags', [0, 1, 2, 4, 8, 0x10, 0x40, 0x80])
def test_ace_flags_including_inherited_provenance_are_never_normalized(flags):
    original = descriptor(dacl=acl(ALLOW))
    changed = ALLOW[:1] + bytes([flags]) + ALLOW[2:]
    assert original.comparison_key() != replace(original, dacl=acl(changed)).comparison_key()


def test_ace_order_mask_type_and_opaque_payload_remain_significant():
    original = descriptor()
    changed_mask = ALLOW[:4] + struct.pack('<I', 0x1F01FF) + ALLOW[8:]
    changed_type = bytes([1]) + ALLOW[1:]
    opaque = struct.pack('<BBH', 0x7F, 0, 12) + b'opaque!!'
    for changed in (acl(DENY, ALLOW), acl(changed_mask, DENY),
                    acl(changed_type, DENY), acl(opaque, DENY)):
        assert original.comparison_key() != replace(original, dacl=changed).comparison_key()
    assert SecurityDescriptor.from_bytes(replace(original, dacl=acl(opaque)).to_bytes()).dacl == acl(opaque)


@pytest.mark.parametrize('raw', [b'', b'\x01' * 19, header(revision=2),
                               header(control=0), b'\0' * (MAX_DESCRIPTOR_BYTES + 1)],
                         ids=['empty', 'truncated', 'revision', 'absolute', 'oversize'])
def test_invalid_descriptor_envelope_is_rejected(raw):
    with pytest.raises(ValueError):
        SecurityDescriptor.from_bytes(raw)


@pytest.mark.parametrize('offset', [1, 16, 19, 21, 24, 0xFFFFFFFC])
def test_header_misaligned_and_out_of_bounds_offsets_are_rejected(offset):
    with pytest.raises(ValueError):
        SecurityDescriptor.from_bytes(header(owner=offset) + SID)


@pytest.mark.parametrize('field,offset', [('owner', 20), ('group', 20), ('sacl', 20), ('dacl', 20)])
def test_component_header_truncation_is_rejected(field, offset):
    with pytest.raises(ValueError):
        SecurityDescriptor.from_bytes(header(0xFFFF, **{field: offset}) + b'\0' * 7)


@pytest.mark.parametrize('sid', [bytes([2, 1]) + b'\0' * 10,
                               bytes([1, 16]) + b'\0' * 70,
                               bytes([1, 2]) + b'\0' * 10])
def test_invalid_sid_revision_count_and_extent_are_rejected(sid):
    with pytest.raises(ValueError):
        SecurityDescriptor.from_bytes(header(owner=20) + sid)


def test_sid_maximum_subauthority_count_is_supported():
    sid = bytes([1, 15]) + b'\0' * 6 + struct.pack('<15I', *range(15))
    assert SecurityDescriptor.from_bytes(header(owner=20) + sid).owner == sid


@pytest.mark.parametrize('bad_acl', [
    struct.pack('<BBHHH', 3, 0, 8, 0, 0),
    struct.pack('<BBHHH', 2, 1, 8, 0, 0),
    struct.pack('<BBHHH', 2, 0, 8, 0, 1),
    struct.pack('<BBHHH', 2, 0, 4, 0, 0),
    struct.pack('<BBHHH', 2, 0, 9, 0, 0) + b'\0',
    struct.pack('<BBHHH', 2, 0, 12, 0, 0),
    struct.pack('<BBHHH', 2, 0, 8, 1, 0),
    struct.pack('<BBHHH', 2, 0, 12, 1, 0) + struct.pack('<BBH', 0, 0, 0),
    struct.pack('<BBHHH', 2, 0, 12, 1, 0) + struct.pack('<BBH', 0, 0, 2),
    struct.pack('<BBHHH', 2, 0, 16, 1, 0) + struct.pack('<BBH', 0, 0, 6) + b'\0' * 4,
    struct.pack('<BBHHH', 2, 0, 12, 1, 0) + struct.pack('<BBH', 0, 0, 8),
    struct.pack('<BBHHH', 2, 0, 28, 2, 0) + ALLOW,
])
def test_invalid_acl_revision_reserved_fields_count_and_sizes_are_rejected(bad_acl):
    with pytest.raises(ValueError):
        SecurityDescriptor.from_bytes(header(SE_SELF_RELATIVE | SE_DACL_PRESENT, dacl=20) + bad_acl)


@pytest.mark.parametrize('field', ['sacl', 'dacl'])
def test_acl_bytes_without_present_bit_are_rejected(field):
    with pytest.raises(ValueError, match='PRESENT'):
        SecurityDescriptor.from_bytes(header(**{field: 20}) + acl())
    with pytest.raises(ValueError, match='PRESENT'):
        SecurityDescriptor(SE_SELF_RELATIVE, 0, None, None,
                           acl() if field == 'sacl' else None,
                           acl() if field == 'dacl' else None)


def test_partially_overlapping_sids_are_rejected_even_if_each_sid_is_valid():
    large_sid = bytes([1, 3]) + b'\0' * 6 + SID
    with pytest.raises(ValueError, match='Overlapping'):
        SecurityDescriptor.from_bytes(header(owner=20, group=28) + large_sid)


def test_exact_sacl_dacl_alias_is_rejected():
    with pytest.raises(ValueError, match='Overlapping'):
        SecurityDescriptor.from_bytes(header(0xFFFF, sacl=20, dacl=20) + acl())


def test_sid_inside_acl_ace_is_rejected_as_an_overlapping_component():
    with pytest.raises(ValueError, match='Overlapping'):
        SecurityDescriptor.from_bytes(header(0xFFFF, owner=36, dacl=20) + acl(ALLOW))


def test_acl_free_allocation_cannot_hide_an_overlapping_sid():
    raw_acl = acl(padding=SID)
    with pytest.raises(ValueError, match='Overlapping'):
        SecurityDescriptor.from_bytes(header(0xFFFF, owner=28, dacl=20) + raw_acl)


def test_every_truncation_of_complete_descriptor_is_rejected():
    raw = descriptor().to_bytes()
    for length in range(len(raw)):
        with pytest.raises(ValueError):
            SecurityDescriptor.from_bytes(raw[:length])


@pytest.mark.parametrize('updates', [
    {'control': -1}, {'control': 0x10000}, {'control': True}, {'control': 0},
    {'rm_control': -1}, {'rm_control': 256}, {'rm_control': True},
    {'owner': SID + b'\0' * 4}, {'dacl': acl() + b'\0' * 4},
])
def test_direct_construction_cannot_bypass_component_validation(updates):
    with pytest.raises(ValueError):
        replace(descriptor(), **updates)


@pytest.mark.parametrize('value', ['not binary', 20, [1, 2, 3], None])
def test_non_binary_input_is_rejected_without_coercion(value):
    with pytest.raises(TypeError):
        SecurityDescriptor.from_bytes(value)
