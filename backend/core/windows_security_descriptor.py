"""Bounded, lossless components of self-relative Windows security descriptors.

This module validates the binary envelope without calling Windows or changing
permissions. ACE payloads remain opaque: native validation is still required
before an operator submits a descriptor to a Windows security API. Equality of
comparison keys preserves inheritance provenance; it is not an access check.
"""
from __future__ import annotations

from dataclasses import dataclass
import struct


SE_OWNER_DEFAULTED = 0x0001
SE_GROUP_DEFAULTED = 0x0002
SE_DACL_PRESENT = 0x0004
SE_DACL_DEFAULTED = 0x0008
SE_SACL_PRESENT = 0x0010
SE_SACL_DEFAULTED = 0x0020
SE_DACL_AUTO_INHERIT_REQ = 0x0100
SE_SACL_AUTO_INHERIT_REQ = 0x0200
SE_DACL_AUTO_INHERITED = 0x0400
SE_SACL_AUTO_INHERITED = 0x0800
SE_DACL_PROTECTED = 0x1000
SE_SACL_PROTECTED = 0x2000
SE_RM_CONTROL_VALID = 0x4000
SE_SELF_RELATIVE = 0x8000

MAX_DESCRIPTOR_BYTES = 1024 * 1024
_SD_HEADER = struct.Struct('<BBHIIII')
_ACL_HEADER = struct.Struct('<BBHHH')
_ACE_HEADER = struct.Struct('<BBH')


def _bytes(value: bytes, name: str) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f'{name} must be bytes')
    size = value.nbytes if isinstance(value, memoryview) else len(value)
    if size > MAX_DESCRIPTOR_BYTES:
        raise ValueError(f'{name} exceeds size limit')
    return bytes(value)


def _sid_size(raw: bytes, offset: int, name: str) -> int:
    if offset + 8 > len(raw):
        raise ValueError(f'Truncated {name} SID header')
    revision, count = raw[offset:offset + 2]
    if revision != 1 or count > 15:
        raise ValueError(f'Invalid {name} SID revision or subauthority count')
    size = 8 + count * 4
    if offset + size > len(raw):
        raise ValueError(f'Truncated {name} SID subauthorities')
    return size


def _acl_parts(raw: bytes, offset: int, name: str) -> tuple[int, int, tuple[bytes, ...]]:
    if offset + _ACL_HEADER.size > len(raw):
        raise ValueError(f'Truncated {name} ACL header')
    revision, reserved1, size, count, reserved2 = _ACL_HEADER.unpack_from(raw, offset)
    if revision not in (2, 4):
        raise ValueError(f'Invalid {name} ACL revision')
    if reserved1 or reserved2:
        raise ValueError(f'Nonzero {name} ACL reserved fields')
    if size < _ACL_HEADER.size or size % 4 or offset + size > len(raw):
        raise ValueError(f'Invalid {name} ACL size')
    end = offset + size
    cursor = offset + _ACL_HEADER.size
    aces = []
    for _ in range(count):
        if cursor + _ACE_HEADER.size > end:
            raise ValueError(f'Truncated {name} ACE header')
        _, _, ace_size = _ACE_HEADER.unpack_from(raw, cursor)
        if ace_size < _ACE_HEADER.size or ace_size % 4 or cursor + ace_size > end:
            raise ValueError(f'Invalid {name} ACE size')
        aces.append(raw[cursor:cursor + ace_size])
        cursor += ace_size
    # The ACL's declared allocation may include unused bytes after its ACEs.
    return size, revision, tuple(aces)


@dataclass(frozen=True, slots=True)
class SecurityDescriptor:
    """Validated descriptor components, including NULL versus absent ACLs.

    ``None`` plus PRESENT means a NULL ACL; ``None`` without PRESENT means an
    absent ACL. An eight-byte ACL with zero ACEs is an empty ACL. DACL variants
    remain distinct, including in ``comparison_key``. Component bytes retain
    ACL allocation padding; serialization gives them deterministic offsets.
    """

    control: int
    rm_control: int
    owner: bytes | None
    group: bytes | None
    sacl: bytes | None
    dacl: bytes | None

    def __post_init__(self) -> None:
        if type(self.control) is not int or not 0 <= self.control <= 0xFFFF:
            raise ValueError('Descriptor control must be a 16-bit integer')
        if not self.control & SE_SELF_RELATIVE:
            raise ValueError('Descriptor must be self-relative')
        if type(self.rm_control) is not int or not 0 <= self.rm_control <= 0xFF:
            raise ValueError('Resource manager control must be an 8-bit integer')
        total = _SD_HEADER.size
        for name in ('owner', 'group', 'sacl', 'dacl'):
            component = getattr(self, name)
            if component is None:
                continue
            component = _bytes(component, name)
            object.__setattr__(self, name, component)
            if name in ('owner', 'group'):
                size = _sid_size(component, 0, name)
            else:
                present = SE_SACL_PRESENT if name == 'sacl' else SE_DACL_PRESENT
                if not self.control & present:
                    raise ValueError(f'{name} bytes require its PRESENT control flag')
                size, _, _ = _acl_parts(component, 0, name)
            if size != len(component):
                raise ValueError(f'Unexpected bytes after {name} component')
            total += size
        if total > MAX_DESCRIPTOR_BYTES:
            raise ValueError('Security descriptor exceeds size limit')

    @classmethod
    def from_bytes(cls, raw: bytes) -> SecurityDescriptor:
        """Parse a revision-one descriptor before exposing bytes to native APIs.

        Components may appear in any order with native allocation padding.
        Overlap is refused except for owner and group sharing one complete SID.
        """
        raw = _bytes(raw, 'Security descriptor')
        if not _SD_HEADER.size <= len(raw) <= MAX_DESCRIPTOR_BYTES:
            raise ValueError('Security descriptor size is outside allowed bounds')
        revision, rm_control, control, *offsets = _SD_HEADER.unpack_from(raw)
        if revision != 1:
            raise ValueError('Invalid security descriptor revision')
        if not control & SE_SELF_RELATIVE:
            raise ValueError('Descriptor must be self-relative')
        components: dict[str, bytes | None] = {}
        spans: list[tuple[int, int, str]] = []
        for name, offset in zip(('owner', 'group', 'sacl', 'dacl'), offsets):
            if not offset:
                components[name] = None
                continue
            if offset < _SD_HEADER.size or offset % 4 or offset >= len(raw):
                raise ValueError(f'Invalid {name} component offset')
            if name in ('owner', 'group'):
                size = _sid_size(raw, offset, name)
            else:
                present = SE_SACL_PRESENT if name == 'sacl' else SE_DACL_PRESENT
                if not control & present:
                    raise ValueError(f'{name} offset requires its PRESENT control flag')
                size, _, _ = _acl_parts(raw, offset, name)
            end = offset + size
            for start2, end2, name2 in spans:
                if offset < end2 and start2 < end:
                    shared_sid = ({name, name2} == {'owner', 'group'}
                                  and offset == start2 and end == end2)
                    if not shared_sid:
                        raise ValueError(f'Overlapping {name} and {name2} components')
            spans.append((offset, end, name))
            components[name] = raw[offset:end]
        return cls(control=control, rm_control=rm_control, **components)

    def to_bytes(self) -> bytes:
        """Pack owner, group, SACL, then DACL without changing ACEs or control."""
        body = bytearray()
        offsets = []
        for component in (self.owner, self.group, self.sacl, self.dacl):
            offsets.append(_SD_HEADER.size + len(body) if component is not None else 0)
            if component is not None:
                body.extend(component)
        return _SD_HEADER.pack(1, self.rm_control, self.control, *offsets) + body

    def comparison_key(self) -> tuple:
        """Compare authorization and inheritance fields, not allocation layout.

        Zero-ACE SACLs carry no audit or mandatory-label entries. Only that case
        normalizes absent/NULL/empty SACLs and SACL_PRESENT; every other control
        flag, RM byte, ACL revision, and complete ordered ACE stays significant.
        In particular, neither AUTO_INHERITED nor INHERITED_ACE is normalized.
        """
        control = self.control
        sacl_key = None
        if self.sacl is not None:
            _, revision, aces = _acl_parts(self.sacl, 0, 'sacl')
            if aces:
                sacl_key = (revision, aces)
        if sacl_key is None:
            control &= ~SE_SACL_PRESENT
        dacl_key = None
        if self.dacl is not None:
            _, revision, aces = _acl_parts(self.dacl, 0, 'dacl')
            dacl_key = (revision, aces)
        return control, self.rm_control, self.owner, self.group, sacl_key, dacl_key
