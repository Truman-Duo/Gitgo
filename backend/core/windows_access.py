"""Scoped DACL/MIC access preparation using existing pinned-tree machinery.

Unlike operator full-security recovery, this never reads/writes audit policy,
changes owner/group or requests backup/restore/security privileges. The current token
must already hold READ_CONTROL, WRITE_DAC and WRITE_OWNER on the targets.
"""
from __future__ import annotations

import ctypes as C
import struct
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

from .windows_acl import SecurityTree, DACL, LABEL
from .windows_acl_plan import _entries, _acl, _profile_sid


class AccessTree(SecurityTree):
    security_information = 1 | 2 | DACL | LABEL
    read_access = 0x20089
    ancestor_access = 0x200A0
    privilege_context = staticmethod(nullcontext)

    def __init__(self, roots, *, writable=True, shallow=(), metadata=()):
        self.metadata = {str(Path(p)) for p in metadata}
        self.shallow = {str(Path(p)) for p in shallow} | self.metadata
        super().__init__(roots, writable=writable)

    def _visit(self, path):
        if str(path) not in self.shallow:
            return super()._visit(path)
        # Interpreter imports need the code root itself, not every sibling
        # directory in a developer checkout. No inheritable ACE is planned.
        key = str(path)
        if key not in self.handles:
            handle = self._open(path, self.access)
            self.handles[key] = handle
            self._information(handle)

    def plan(self, records, workspace, principal):
        planned = super().plan(records, workspace, principal)
        sid = _profile_sid(principal)
        import base64
        for index, record in enumerate(planned):
            if record["path"] not in self.metadata:
                continue
            descriptor = self.record_descriptor(record)
            entries = []
            for ace in _entries(descriptor.dacl):
                if ace[8:] == sid:
                    # SYNCHRONIZE | READ_ATTRIBUTES | TRAVERSE only. No list,
                    # data, write, security modification or inherited grant.
                    ace = struct.pack("<BBHI", 0, 0, 8 + len(sid), 0x1000A0) + sid
                entries.append(ace)
            raw = replace(descriptor, dacl=_acl(descriptor.dacl[0], entries)).to_bytes()
            self._validated(raw)
            planned[index] = dict(record, descriptor=base64.b64encode(raw).decode("ascii"), sddl=self._text(raw))
        return planned

    def _write_descriptor(self, handle, raw):
        # NtSetSecurityObject changes only these components and does not
        # recursively propagate inheritance behind our pinned inventory.
        descriptor = self._validated(raw)
        flags = descriptor.control | (0x100 if descriptor.control & 0x400 else 0) | (0x200 if descriptor.control & 0x800 else 0)
        buffer = C.create_string_buffer(replace(descriptor, control=flags).to_bytes())
        status = self.set_object_security(handle, DACL | LABEL, buffer)
        if status:
            raise C.WinError(self.status_to_error(status))

    def revoke(self, records, principal):
        """Remove only this preparation's exact principal, preserving peers."""
        sid = _profile_sid(principal)
        targets = []
        for record in records:
            descriptor = self.record_descriptor(record)
            entries = _entries(descriptor.dacl)
            kept = []
            for ace in entries:
                if sid not in ace:
                    kept.append(ace)
                elif (ace[0] != 0 or ace[8:] != sid or ace[1] not in {0,3,16,19}
                        or struct.unpack_from('<I', ace, 4)[0] not in {0x1200A9,0x1301BF,0x1000A0}):
                    raise ValueError('Sandbox principal has an unsupported ACE; retain recovery record')
            changed = replace(descriptor, dacl=_acl(descriptor.dacl[0], kept))
            import base64
            raw = changed.to_bytes()
            targets.append(dict(record, descriptor=base64.b64encode(raw).decode('ascii'),
                                sddl=self._text(raw)))
        return targets
