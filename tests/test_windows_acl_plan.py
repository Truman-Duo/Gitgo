"""Pure planning tests: existing policy bytes survive, ambiguous input is refused."""
from dataclasses import replace
import struct

import pytest

from backend.core.windows_acl_plan import MODIFY, READ_EXECUTE, plan_descriptor
from backend.core.windows_security_descriptor import (
    SE_DACL_PRESENT, SE_SACL_PRESENT, SE_SELF_RELATIVE, SecurityDescriptor,
)


PROFILE = 'S-1-15-2-1-2-3-4-5-6-4294967295'
PROFILE_SID = bytes([1, 8]) + (15).to_bytes(6, 'big') + struct.pack('<8I', 2, 1, 2, 3, 4, 5, 6, 0xFFFFFFFF)
OWNER = bytes.fromhex('010100000000000512000000')
GROUP = bytes.fromhex('010100000000000513000000')
LOW = bytes.fromhex('010100000000001000100000')
MEDIUM = bytes.fromhex('010100000000001000200000')


def ace(kind=0, flags=0, mask=READ_EXECUTE, sid=OWNER):
    return struct.pack('<BBHI', kind, flags, 8 + len(sid), mask) + sid


def acl(*entries, revision=2, padding=b''):
    data = b''.join(entries)
    return struct.pack('<BBHHH', revision, 0, 8 + len(data) + len(padding), len(entries), 0) + data + padding


def entries(value):
    result, offset = [], 8
    for _ in range(struct.unpack_from('<H', value, 4)[0]):
        length = struct.unpack_from('<H', value, offset + 2)[0]
        result.append(value[offset:offset + length])
        offset += length
    return result


def descriptor(dacl=None, sacl=None, control=None):
    if dacl is None:
        dacl = acl(ace(kind=1, mask=0x40000), ace(flags=0x13, sid=GROUP))
    if control is None:
        control = SE_SELF_RELATIVE | SE_DACL_PRESENT | (SE_SACL_PRESENT if sacl is not None else 0)
    return SecurityDescriptor(control, 73, OWNER, GROUP, sacl, dacl)


@pytest.mark.parametrize('directory', [False, True])
@pytest.mark.parametrize('workspace', [False, True])
def test_plan_inserts_exact_explicit_grant_before_inherited_policy(directory, workspace):
    original = descriptor()
    planned = plan_descriptor(original, PROFILE, directory=directory, workspace=workspace)
    wanted = ace(flags=3 if directory else 0, mask=MODIFY if workspace else READ_EXECUTE, sid=PROFILE_SID)
    assert entries(planned.dacl) == [entries(original.dacl)[0], wanted, entries(original.dacl)[1]]
    assert (planned.owner, planned.group, planned.rm_control) == (OWNER, GROUP, 73)
    assert original.dacl == descriptor().dacl
    assert planned.control == original.control | (SE_SACL_PRESENT if workspace else 0)
    if workspace:
        assert entries(planned.sacl) == [ace(kind=17, flags=3 if directory else 0, mask=1, sid=LOW)]
    else:
        assert planned.sacl is None
    assert SecurityDescriptor.from_bytes(planned.to_bytes()) == planned
    assert plan_descriptor(planned, PROFILE, directory=directory, workspace=workspace) == planned


def test_existing_controls_ace_order_audit_and_mic_policy_are_preserved():
    audit = ace(kind=2, flags=0xC3, mask=0x1F01FF)
    label = ace(kind=17, flags=0x13, mask=7, sid=MEDIUM)
    inherited_audit = ace(kind=2, flags=0xD3, mask=0x10000, sid=GROUP)
    original = descriptor(sacl=acl(audit, label, inherited_audit, revision=4), control=0xFFFF)
    planned = plan_descriptor(original, PROFILE, directory=True, workspace=True)
    assert planned.control == original.control
    assert planned.owner == original.owner and planned.group == original.group
    assert planned.rm_control == original.rm_control
    assert planned.sacl[0] == 4
    assert entries(planned.sacl) == [audit, label[:8] + LOW, inherited_audit]
    assert [a for a in entries(planned.dacl) if PROFILE_SID not in a] == entries(original.dacl)


def test_runtime_does_not_rewrite_existing_sacl_or_controls():
    original = descriptor(sacl=acl(ace(kind=17, flags=8, mask=1, sid=MEDIUM), padding=b'\xaa' * 4), control=0xFFFF)
    planned = plan_descriptor(original, PROFILE, directory=False, workspace=False)
    assert planned.sacl == original.sacl
    assert planned.control == original.control


@pytest.mark.parametrize('sacl,control', [(None, SE_SELF_RELATIVE | SE_DACL_PRESENT),
                                        (None, SE_SELF_RELATIVE | SE_DACL_PRESENT | SE_SACL_PRESENT),
                                        (acl(), SE_SELF_RELATIVE | SE_DACL_PRESENT | SE_SACL_PRESENT)])
def test_absent_null_and_empty_sacl_get_explicit_low_label(sacl, control):
    original = descriptor(sacl=sacl, control=control)
    planned = plan_descriptor(original, PROFILE, directory=False, workspace=True)
    assert entries(planned.sacl) == [ace(kind=17, mask=1, sid=LOW)]
    assert planned.control == control | SE_SACL_PRESENT


def test_new_label_precedes_inherited_audit_without_reordering_existing_entries():
    audit = ace(kind=2, flags=0x40)
    inherited_audit = ace(kind=2, flags=0x50, sid=GROUP)
    original = descriptor(sacl=acl(audit, inherited_audit))
    planned = plan_descriptor(original, PROFILE, directory=True, workspace=True)
    assert entries(planned.sacl) == [audit, ace(kind=17, flags=3, mask=1, sid=LOW), inherited_audit]


def test_exact_existing_grant_is_idempotent_including_unused_acl_allocation():
    dacl = acl(ace(mask=MODIFY, sid=PROFILE_SID), padding=b'\xab' * 12)
    sacl = acl(ace(kind=17, mask=0, sid=LOW), padding=b'\xcd' * 4)
    original = descriptor(dacl=dacl, sacl=sacl)
    planned = plan_descriptor(original, PROFILE, directory=False, workspace=True)
    assert planned == original
    assert planned.to_bytes() == original.to_bytes()


@pytest.mark.parametrize('grant', [ace(kind=1, mask=MODIFY, sid=PROFILE_SID),
                                  ace(mask=READ_EXECUTE, sid=PROFILE_SID),
                                  ace(flags=3, mask=MODIFY, sid=PROFILE_SID),
                                  ace(flags=0x10, mask=MODIFY, sid=PROFILE_SID),
                                  ace(kind=9, mask=MODIFY, sid=PROFILE_SID),
                                  struct.pack('<BBH', 0x7F, 0, 48) + b'XXXX' + PROFILE_SID])
def test_conflicting_target_principal_aces_are_refused_without_modification(grant):
    original = descriptor(dacl=acl(grant))
    before = original.to_bytes()
    with pytest.raises(ValueError, match='conflicts'):
        plan_descriptor(original, PROFILE, directory=False, workspace=True)
    assert original.to_bytes() == before


@pytest.mark.parametrize('other', [ace(mask=MODIFY, sid=PROFILE_SID), ace(kind=1, sid=PROFILE_SID)])
def test_exact_match_does_not_hide_duplicate_or_conflicting_target_ace(other):
    original = descriptor(dacl=acl(ace(mask=MODIFY, sid=PROFILE_SID), other))
    with pytest.raises(ValueError, match='conflicts'):
        plan_descriptor(original, PROFILE, directory=False, workspace=True)


def test_noncanonical_explicit_after_inherited_dacl_is_refused():
    original = descriptor(dacl=acl(ace(flags=0x10), ace(kind=1, sid=GROUP)))
    with pytest.raises(ValueError, match='explicit ACEs after inherited'):
        plan_descriptor(original, PROFILE, directory=False, workspace=False)


@pytest.mark.parametrize('present', [False, True])
def test_absent_and_null_dacl_are_refused(present):
    original = SecurityDescriptor(SE_SELF_RELATIVE | (SE_DACL_PRESENT if present else 0),
                                  0, OWNER, GROUP, None, None)
    with pytest.raises(ValueError, match='NULL or absent'):
        plan_descriptor(original, PROFILE, directory=False, workspace=False)


def test_empty_dacl_can_be_granted_without_losing_its_revision():
    planned = plan_descriptor(descriptor(dacl=acl(revision=4)), PROFILE, directory=False, workspace=False)
    assert planned.dacl[0] == 4
    assert entries(planned.dacl) == [ace(mask=READ_EXECUTE, sid=PROFILE_SID)]


@pytest.mark.parametrize('principal', [None, b'S-1-15-2', '', 'S-1-5-18', 'S-1-15-2-1',
    'S-1-15-2-1-2-3-4-5-6-7-8', 'S-1-15-3-1-2-3-4-5-6-7', 'S-1-15-2-1-2-3-4-5-6-4294967296',
    'S-1-15-2-1-2-3-4-5-6-07', 'S-1-15-2-1-2-3-4-5-6--7',
    'S-1-15-2-1-2-3-4-5-6-7\n', 'S-1-15-2-' + '9' * 129])
def test_only_canonical_bounded_appcontainer_profile_sid_is_accepted(principal):
    with pytest.raises(ValueError, match='SID'):
        plan_descriptor(descriptor(), principal, directory=False, workspace=False)


@pytest.mark.parametrize('label', [ace(kind=17, sid=OWNER),
    ace(kind=17, sid=MEDIUM + b'\0' * 4), ace(kind=17, sid=MEDIUM[:8]),
    ace(kind=17, mask=8, sid=MEDIUM), ace(kind=17, flags=0x20, mask=1, sid=MEDIUM),
    ace(kind=17, flags=8, mask=1, sid=MEDIUM)])
def test_malformed_or_unsupported_mandatory_label_is_refused(label):
    with pytest.raises(ValueError, match='mandatory label'):
        plan_descriptor(descriptor(sacl=acl(label)), PROFILE, directory=False, workspace=True)


def test_multiple_mandatory_labels_are_refused():
    labels = acl(ace(kind=17, mask=1, sid=MEDIUM), ace(kind=17, flags=0x10, mask=1, sid=LOW))
    with pytest.raises(ValueError, match='Multiple mandatory'):
        plan_descriptor(descriptor(sacl=labels), PROFILE, directory=False, workspace=True)


@pytest.mark.parametrize('field', ['dacl', 'sacl'])
def test_planning_refuses_acl_size_overflow(field):
    huge = struct.pack('<BBH', 0x7F, 0, 65524) + b'\0' * 65520
    original = descriptor(**{field: acl(huge)})
    with pytest.raises(ValueError, match='size limits'):
        plan_descriptor(original, PROFILE, directory=False, workspace=True)


@pytest.mark.parametrize('options', [{'directory': 1, 'workspace': False},
                                   {'directory': False, 'workspace': 'yes'}])
def test_scope_flags_require_actual_booleans(options):
    with pytest.raises(TypeError, match='booleans'):
        plan_descriptor(descriptor(), PROFILE, **options)
