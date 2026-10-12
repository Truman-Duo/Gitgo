"""Pure descriptor planning for explicit, journaled AppContainer provisioning."""
from __future__ import annotations

from dataclasses import replace
import re
import struct

from backend.core.windows_security_descriptor import SecurityDescriptor, SE_SACL_PRESENT


READ_EXECUTE = 0x1200A9
MODIFY = 0x1301BF
_LOW_SID = bytes.fromhex('010100000000001000100000')
_PROFILE_SID = re.compile(r'S-1-15-2-(0|[1-9][0-9]*)-(0|[1-9][0-9]*)-(0|[1-9][0-9]*)-'
                          r'(0|[1-9][0-9]*)-(0|[1-9][0-9]*)-(0|[1-9][0-9]*)-(0|[1-9][0-9]*)')


def _profile_sid(principal: str) -> bytes:
    if not isinstance(principal, str) or len(principal) > 128:
        raise ValueError('Expected an AppContainer profile SID')
    match = _PROFILE_SID.fullmatch(principal)
    if match is None:
        raise ValueError('Expected an AppContainer profile SID')
    hashes = tuple(int(value) for value in match.groups())
    if any(value > 0xFFFFFFFF for value in hashes):
        raise ValueError('AppContainer SID subauthority exceeds 32 bits')
    return bytes([1, 8]) + (15).to_bytes(6, 'big') + struct.pack('<8I', 2, *hashes)


def _entries(acl: bytes) -> list[bytes]:
    # SecurityDescriptor has already bounded the ACL and every ACE header.
    count = struct.unpack_from('<H', acl, 4)[0]
    cursor, result = 8, []
    for _ in range(count):
        size = struct.unpack_from('<H', acl, cursor + 2)[0]
        result.append(acl[cursor:cursor + size])
        cursor += size
    return result


def _acl(revision: int, entries: list[bytes]) -> bytes:
    size = 8 + sum(len(ace) for ace in entries)
    if size > 0xFFFF or len(entries) > 0xFFFF:
        raise ValueError('Planned ACL exceeds Windows size limits')
    return struct.pack('<BBHHH', revision, 0, size, len(entries), 0) + b''.join(entries)


def _grant(original: bytes, principal: bytes, *, directory: bool, workspace: bool) -> bytes:
    entries = _entries(original)
    inherited_seen = False
    for ace in entries:
        if ace[1] & 0x10:
            inherited_seen = True
        elif inherited_seen:
            raise ValueError('Cannot plan a DACL with explicit ACEs after inherited ACEs')
    flags = 3 if directory else 0
    wanted = struct.pack('<BBHI', 0, flags, 8 + len(principal),
                         MODIFY if workspace else READ_EXECUTE) + principal
    matches = [ace for ace in entries if principal in ace]
    if matches:
        if matches == [wanted]:
            return original
        # Refuse conditional/object ACEs and deny entries for this principal;
        # neither their meaning nor their order may be rewritten.
        raise ValueError('Existing AppContainer principal ACE conflicts with the planned grant')
    index = next((i for i, ace in enumerate(entries) if ace[1] & 0x10), len(entries))
    entries.insert(index, wanted)
    return _acl(original[0], entries)


def _workspace_label(original: bytes | None, *, directory: bool) -> bytes:
    entries = _entries(original) if original is not None else []
    labels = [i for i, ace in enumerate(entries) if ace[0] == 0x11]
    if len(labels) > 1:
        raise ValueError('Multiple mandatory label ACEs are ambiguous')
    if labels:
        index = labels[0]
        ace = entries[index]
        if len(ace) != 20 or ace[8:16] != _LOW_SID[:8]:
            raise ValueError('Malformed mandatory label SID or ACE')
        mask = struct.unpack_from('<I', ace, 4)[0]
        if mask & ~7 or ace[1] & ~0x1F or ace[1] & 8:
            raise ValueError('Unsupported mandatory label policy or inheritance flags')
        changed = ace[:8] + _LOW_SID
        if changed == ace:
            return original
        entries[index] = changed
    else:
        flags = 3 if directory else 0
        label = struct.pack('<BBHI', 0x11, flags, 20, 1) + _LOW_SID
        index = next((i for i, ace in enumerate(entries) if ace[1] & 0x10), len(entries))
        entries.insert(index, label)
    return _acl(original[0] if original is not None else 2, entries)


def plan_descriptor(original: SecurityDescriptor, principal: str, *,
                    directory: bool, workspace: bool) -> SecurityDescriptor:
    """Plan a specific profile grant and, for a workspace, a Low integrity label.

    Native ACE semantic validation belongs to the caller, before planning and
    before applying. This function only computes bytes: it cannot authorize or
    perform a permission change. Existing ACE bytes, order, owner, group, and
    inheritance controls are retained; ambiguous existing policy is refused.
    """
    if not isinstance(original, SecurityDescriptor):
        raise TypeError('Expected a validated SecurityDescriptor')
    if type(directory) is not bool or type(workspace) is not bool:
        raise TypeError('directory and workspace must be booleans')
    principal_sid = _profile_sid(principal)
    if original.dacl is None:
        raise ValueError('Cannot provision a NULL or absent DACL')
    dacl = _grant(original.dacl, principal_sid, directory=directory, workspace=workspace)
    sacl = (_workspace_label(original.sacl, directory=directory)
            if workspace else original.sacl)
    control = (original.control | SE_SACL_PRESENT) if workspace else original.control
    return replace(original, control=control, dacl=dacl, sacl=sacl)
