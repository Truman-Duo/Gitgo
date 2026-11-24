"""Scoped DACL/MIC access preparation using existing pinned-tree machinery.

Unlike operator full-security recovery, this never reads/writes audit policy,
changes owner/group or requests backup/restore/security privileges. The current token
must already hold READ_CONTROL, WRITE_DAC and WRITE_OWNER on the targets.
"""
from __future__ import annotations

import ctypes as C
import struct
import base64
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from .windows_acl import MAX_TREE_OBJECTS, SecurityTree, DACL, LABEL
from .windows_acl_plan import _entries, _acl, _profile_sid
from .windows_security_privileges import uses_process_token


_RUNTIME_OPEN_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='gitgo-acl-open')
_PREFETCH_MIN = 12
_PREFETCH_LIMIT = 64


class AccessTree(SecurityTree):
    security_information = 1 | 2 | DACL | LABEL
    read_access = 0x20089
    ancestor_access = 0x200A0
    privilege_context = staticmethod(nullcontext)

    def __init__(self, roots, *, writable=True, shallow=(), metadata=(), readonly=(), execute=(), private_temp=(),
                 excluded_read_dirs=(), read_max_depth=None):
        self.readonly = tuple(Path(p) for p in readonly)
        self.excluded_read_dirs = frozenset(excluded_read_dirs)
        self.read_max_depth = read_max_depth
        self.execute = tuple(Path(p) for p in execute)
        self.metadata = {str(Path(p)) for p in metadata}
        self.private_temp = tuple(Path(p) for p in private_temp)
        self.shallow = {str(Path(p)) for p in shallow} | self.metadata
        self._prefetched = {}
        self._parallel_open = False
        super().__init__(roots, writable=writable)

    def pin_roots(self, roots):
        self._parallel_open = (bool(self.execute) and not self.readonly
                               and uses_process_token(self.kernel, self.advapi))
        try:
            super().pin_roots(roots)
        finally:
            self._parallel_open = False

    def _open_visited(self, path):
        handle = self._prefetched.pop(str(path), None)
        if handle is not None:
            return handle
        if len(self.handles) + len(self._prefetched) >= MAX_TREE_OBJECTS:
            raise OSError('Configuration tree exceeds 100000 objects')
        return super()._open_visited(path)

    def _visit_children(self, parent, children):
        if (not self._parallel_open or len(children) < _PREFETCH_MIN
                or not any(parent.is_relative_to(root) for root in self.execute)):
            return super()._visit_children(parent, children)
        for offset in range(0, len(children), _PREFETCH_LIMIT):
            group = children[offset:offset + _PREFETCH_LIMIT]
            available = min(_PREFETCH_LIMIT, MAX_TREE_OBJECTS - len(self.handles)) - len(self._prefetched)
            candidates = [child for child in group if child not in self.handles and child not in self.shallow]
            if _PREFETCH_MIN <= len(candidates) <= available:
                self._prefetch(candidates)
            super()._visit_children(parent, group)

    def _prefetch(self, candidates):
        # Only open independent siblings under a pinned trusted runtime. Keep
        # DFS ordering and live checks for the journal and all ACL operations.
        def pin(path):
            self._prefetched[path] = self._open(path, self.access)

        pending = []
        try:
            for path in candidates:
                pending.append(_RUNTIME_OPEN_POOL.submit(pin, path))
            for future in pending:
                future.result()
        finally:
            for future in pending:
                future.cancel()
            wait(pending)

    def close(self):
        # After a failed open, the completed siblings still belong to this
        # tree and use the ordinary handle retirement and retry path.
        self.handles.update(self._prefetched)
        self._prefetched.clear()
        super().close()

    def _directory_inventory(self, path):
        if self.read_max_depth is None and not self.excluded_read_dirs:
            return super()._directory_inventory(path)
        path = Path(path)
        read_root = next((root for root in self.readonly if path.is_relative_to(root)), None)
        if read_root is not None and self.read_max_depth is not None:
            if len(path.relative_to(read_root).parts) >= self.read_max_depth:
                return set()
        children = super()._directory_inventory(path)
        if self.excluded_read_dirs and read_root is not None:
            children = {child for child in children
                        if Path(child).name not in self.excluded_read_dirs or not Path(child).is_dir()}
        return children

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

        @lru_cache(maxsize=32)
        def scoped_descriptor(descriptor, mask, non_inheritable):
            entries = []
            for ace in _entries(descriptor.dacl):
                if ace[8:] == sid:
                    inheritance = 0 if non_inheritable else ace[1]
                    ace = struct.pack("<BBHI", 0, inheritance, 8 + len(sid), mask) + sid
                entries.append(ace)
            raw = replace(descriptor, dacl=_acl(descriptor.dacl[0], entries)).to_bytes()
            self._validated(raw)
            return base64.b64encode(raw).decode('ascii'), self._text(raw)

        for index, record in enumerate(planned):
            path = Path(record['path'])
            private_temp = any(path.is_relative_to(root) for root in self.private_temp)
            metadata = record['path'] in self.metadata
            # Execute roots only affect records already in the read scope.
            readonly = any(path.is_relative_to(root) for root in self.readonly) and not any(
                path.is_relative_to(root) for root in self.execute)
            if not metadata and not readonly and not private_temp:
                continue
            # Metadata grants only SYNCHRONIZE, READ_ATTRIBUTES and TRAVERSE.
            mask = 0x1F01FF if private_temp else (0x1000A0 if metadata else 0x120089)
            content, text = scoped_descriptor(self.record_descriptor(record), mask,
                metadata or bool(readonly and (self.excluded_read_dirs or self.read_max_depth is not None)))
            planned[index] = dict(record, descriptor=content, sddl=text)
        return planned

    def _write_descriptor(self, handle, raw):
        # NtSetSecurityObject changes only these components and does not
        # recursively propagate inheritance behind our pinned inventory.
        descriptor = self._validated(raw)
        flags = descriptor.control | (0x100 if descriptor.control & 0x400 else 0) | (0x200 if descriptor.control & 0x800 else 0)
        from .windows_acl_native import write_scoped_descriptor
        write_scoped_descriptor(handle, replace(descriptor, control=flags).to_bytes())

    def revoke(self, records, principal):
        """Remove only this preparation's exact principal, preserving peers."""
        sid = _profile_sid(principal)

        @lru_cache(maxsize=32)
        def revoked_descriptor(descriptor):
            entries = _entries(descriptor.dacl)
            kept = []
            for ace in entries:
                if sid not in ace:
                    kept.append(ace)
                elif (ace[0] != 0 or ace[8:] != sid or ace[1] not in {0,3,16,19}
                        or struct.unpack_from('<I', ace, 4)[0] not in {0x1200A9,0x1301BF,0x1000A0,0x120089,0x1F01FF}):
                    raise ValueError('Sandbox principal has an unsupported ACE; retain recovery record')
            raw = replace(descriptor, dacl=_acl(descriptor.dacl[0], kept)).to_bytes()
            return base64.b64encode(raw).decode('ascii'), self._text(raw)

        targets = []
        for record in records:
            content, text = revoked_descriptor(self.record_descriptor(record))
            targets.append(dict(record, descriptor=content, sddl=text))
        return targets
