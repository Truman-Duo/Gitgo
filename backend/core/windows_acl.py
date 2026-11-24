"""Operator-only DACL/MIC recovery using stable, non-redirected file handles."""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import os
import base64
import struct
import re
from contextlib import contextmanager
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

from backend.core.windows_security_descriptor import SecurityDescriptor, MAX_DESCRIPTOR_BYTES
from backend.core.windows_security_privileges import security_privileges

DACL, LABEL = 4, 16
FULL_SECURITY = 0x1FF
MAX_TREE_OBJECTS = 100_000


class SecurityConflict(OSError):
    """An identity or descriptor changed outside this configuration transaction."""
    code = 'SANDBOX_SECURITY_DESCRIPTOR_CONFLICT'


class SecurityInventoryChanged(SecurityConflict):
    code = 'SANDBOX_DIRECTORY_INVENTORY_CHANGED'

    def __init__(self, path, expected, current):
        self.path = path
        self.details = {'path': path, 'added': sorted(current - expected)[:32],
                        'removed': sorted(expected - current)[:32]}
        super().__init__(f'Configuration directory changed while security handles were held: {path}')


class SecurityManagementBusy(SecurityConflict):
    code = 'SANDBOX_ACL_MANAGEMENT_BUSY'


class FileInformation(C.Structure):
    _fields_ = [('attributes', W.DWORD), ('created', W.FILETIME),
                ('accessed', W.FILETIME), ('written', W.FILETIME),
                ('volume', W.DWORD), ('size_high', W.DWORD), ('size_low', W.DWORD),
                ('links', W.DWORD), ('index_high', W.DWORD), ('index_low', W.DWORD)]


class FileIdentity(C.Structure):
    _fields_ = [('volume', C.c_ulonglong), ('identifier', C.c_ubyte * 16)]


def _fn(dll, name, args, result):
    function = getattr(dll, name)
    function.argtypes, function.restype = args, result
    return function


class SecurityTree:
    """Hold the entire stopped tree against rename/delete during ACL changes.

    Reparse points are refused, never followed. This is configuration recovery,
    not a backup of file contents or of an actively changing project.
    """
    security_information = FULL_SECURITY
    read_access = 0x01020089
    ancestor_access = 0x010200A0
    descriptor_cache_entries = 128
    descriptor_cache_bytes = 1024 * 1024

    @staticmethod
    def privilege_context():
        return security_privileges()

    def __init__(self, roots, *, writable=True):
        if os.name != 'nt':
            raise OSError('Windows security descriptors are required')
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.advapi = C.WinDLL('advapi32', use_last_error=True)
        self.ntdll = C.WinDLL('ntdll', use_last_error=True)
        self.handles = {}
        self.ancestors = {}
        self.children = {}
        # Cache only the pure interpretation of identical descriptor bytes and
        # diagnostic text. Live handles, identities, inventories and actual
        # security descriptors are still queried at every original boundary.
        self._descriptor_cache = OrderedDict()
        self._descriptor_cache_size = 0
        self._validated_cache = OrderedDict()
        self._validated_cache_size = 0
        self._text_cache = OrderedDict()
        self._text_cache_size = 0
        self.close_handle = _fn(self.kernel, 'CloseHandle', [W.HANDLE], W.BOOL)
        self.free = _fn(self.kernel, 'LocalFree', [C.c_void_p], C.c_void_p)
        self.open_file = _fn(self.kernel, 'CreateFileW',
            [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE)
        self.file_info = _fn(self.kernel, 'GetFileInformationByHandle',
            [W.HANDLE, C.POINTER(FileInformation)], W.BOOL)
        self.file_id = _fn(self.kernel, 'GetFileInformationByHandleEx',
            [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL)
        self.get_security = _fn(self.advapi, 'GetSecurityInfo',
            [W.HANDLE, C.c_int, W.DWORD, C.c_void_p, C.c_void_p, C.c_void_p,
             C.c_void_p, C.POINTER(C.c_void_p)], W.DWORD)
        self.set_security = _fn(self.advapi, 'SetSecurityInfo',
            [W.HANDLE, C.c_int, W.DWORD, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p], W.DWORD)
        self.to_sddl = _fn(self.advapi, 'ConvertSecurityDescriptorToStringSecurityDescriptorW',
            [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(W.LPWSTR), C.c_void_p], W.BOOL)
        self.from_sddl = _fn(self.advapi, 'ConvertStringSecurityDescriptorToSecurityDescriptorW',
            [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p], W.BOOL)
        self.control = _fn(self.advapi, 'GetSecurityDescriptorControl',
            [C.c_void_p, C.POINTER(W.WORD), C.POINTER(W.DWORD)], W.BOOL)
        self.dacl = _fn(self.advapi, 'GetSecurityDescriptorDacl',
            [C.c_void_p, C.POINTER(W.BOOL), C.POINTER(C.c_void_p), C.POINTER(W.BOOL)], W.BOOL)
        self.sacl = _fn(self.advapi, 'GetSecurityDescriptorSacl',
            [C.c_void_p, C.POINTER(W.BOOL), C.POINTER(C.c_void_p), C.POINTER(W.BOOL)], W.BOOL)
        self.set_object_security = _fn(self.ntdll, 'NtSetSecurityObject',
            [W.HANDLE, W.DWORD, C.c_void_p], C.c_long)
        self.status_to_error = _fn(self.ntdll, 'RtlNtStatusToDosError', [C.c_long], W.DWORD)
        self.query_security = _fn(self.ntdll, 'NtQuerySecurityObject',
            [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], C.c_long)
        self.descriptor_length = _fn(self.advapi, 'GetSecurityDescriptorLength', [C.c_void_p], W.DWORD)
        self.valid_descriptor = _fn(self.advapi, 'IsValidSecurityDescriptor', [C.c_void_p], W.BOOL)
        self.valid_acl = _fn(self.advapi, 'IsValidAcl', [C.c_void_p], W.BOOL)
        self.backup_write = _fn(self.kernel, 'BackupWrite',
            [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), W.BOOL, W.BOOL,
             C.POINTER(C.c_void_p)], W.BOOL)
        self.writable = writable
        self.access = self.read_access | (0x40000 | 0x80000 if writable else 0)  # READ_DATA/LIST_DIRECTORY, READ_CONTROL, WRITE_DAC/OWNER
        try:
            self.pin_roots(roots)
        except BaseException:
            self.close()
            raise

    def pin_roots(self, roots):
        """Pin additional paths for the next complete inventory validation."""
        with self.privilege_context():
            for root in roots:
                path = Path(root)
                for ancestor in reversed(path.parents):
                    key = str(ancestor)
                    if key not in self.ancestors:
                        handle = self._open(ancestor, self.ancestor_access)  # TRAVERSE + READ_ATTRIBUTES
                        self.ancestors[key] = handle
                        self._information(handle)
                self._visit(path)

    @staticmethod
    def check(ok):
        if not ok:
            raise C.WinError(C.get_last_error())

    def _visit(self, path):
        key = str(path)
        if key in self.handles:
            return
        if len(self.handles) >= MAX_TREE_OBJECTS:
            raise OSError('Configuration tree exceeds 100000 objects')
        # READ_DATA/LIST_DIRECTORY is necessary for Windows sharing checks;
        # security-only handles do not prevent rename despite a zero delete
        # share flag. Hold every ancestor and target with actual read access.
        # Mutations always use these same validated handles.
        handle = self._open_visited(path)
        self.handles[key] = handle
        info = self._information(handle)
        if info.attributes & 0x10:
            children = self._directory_inventory(path)
            self.children[key] = children
            self._visit_children(path, sorted(children))

    def _open_visited(self, path):
        return self._open(path, self.access)

    def _visit_children(self, parent, children):
        for child in children:
            self._visit(Path(child))

    def _open(self, path, access):
        from backend.core.windows_acl_native import open_file
        return open_file(path, access)

    def _information(self, handle):
        info = FileInformation()
        self.check(self.file_info(handle, C.byref(info)))
        if info.attributes & 0x400:
            raise OSError('Refusing a reparse point in the configuration tree')
        if not info.attributes & 0x10 and info.links != 1:
            raise OSError('Refusing a multiply-linked configuration file')
        return info

    def verify_inventory(self):
        for path, handle in self.handles.items():
            self._information(handle)
            if path in self.children:
                current = self._directory_inventory(path)
                if current != self.children[path]:
                    raise SecurityInventoryChanged(path, self.children[path], current)

    def _directory_inventory(self, path):
        with os.scandir(path) as entries:
            return {item.path for item in entries}

    def _security_bytes(self, handle):
        from backend.core.windows_acl_native import read_descriptor
        raw = read_descriptor(handle, self.security_information)
        self._validated(raw)
        return raw

    def _validated(self, raw):
        # Kernel descriptor reads still happen at each original check. The
        # same immutable returned bytes have the same native validity; avoid
        # reparsing them hundreds of thousands of times in a large tree.
        cache = getattr(self, '_validated_cache', None)
        if cache is None:
            self._validated_cache = cache = OrderedDict()
            self._validated_cache_size = 0
        if isinstance(raw, bytes) and raw in cache:
            descriptor, _ = cache[raw]
            cache.move_to_end(raw)
            return descriptor
        descriptor = SecurityDescriptor.from_bytes(raw)
        # Repack bounded components before exposing any pointers to native code.
        buffer = C.create_string_buffer(descriptor.to_bytes())
        if not self.valid_descriptor(buffer):
            raise ValueError('Invalid native security descriptor')
        for getter in (self.dacl, self.sacl):
            present, defaulted, acl = W.BOOL(), W.BOOL(), C.c_void_p()
            self.check(getter(buffer, C.byref(present), C.byref(acl), C.byref(defaulted)))
            if acl.value and not self.valid_acl(acl):
                raise ValueError('Invalid native ACL')
        if descriptor.owner is None or descriptor.group is None:
            raise ValueError('Recovery requires a complete owner and group snapshot')
        cost = len(raw) * 3
        if isinstance(raw, bytes) and cost <= self.descriptor_cache_bytes:
            while cache and (len(cache) >= self.descriptor_cache_entries
                    or self._validated_cache_size + cost > self.descriptor_cache_bytes):
                _, (_, previous_cost) = cache.popitem(last=False)
                self._validated_cache_size -= previous_cost
            cache[raw] = (descriptor, cost)
            self._validated_cache_size += cost
        return descriptor

    def _text(self, raw):
        cache = getattr(self, '_text_cache', None)
        if cache is None:
            self._text_cache = cache = OrderedDict()
            self._text_cache_size = 0
        if isinstance(raw, bytes) and raw in cache:
            result, _ = cache[raw]
            cache.move_to_end(raw)
            return result
        from backend.core.windows_acl_native import descriptor_text
        result = descriptor_text(raw)
        cost = len(raw) + len(result) * 2
        if isinstance(raw, bytes) and cost <= self.descriptor_cache_bytes:
            while cache and (len(cache) >= self.descriptor_cache_entries
                    or self._text_cache_size + cost > self.descriptor_cache_bytes):
                _, (_, previous_cost) = cache.popitem(last=False)
                self._text_cache_size -= previous_cost
            cache[raw] = (result, cost)
            self._text_cache_size += cost
        return result

    def record_descriptor(self, record):
        value = record.get('descriptor')
        if not isinstance(value, str) or len(value) > ((MAX_DESCRIPTOR_BYTES + 2) // 3) * 4:
            raise ValueError('Recovery requires a bounded full binary descriptor')
        text = record.get('sddl')
        if not isinstance(text, str):
            raise ValueError('Recovery descriptor and diagnostic text disagree')
        cache = getattr(self, '_descriptor_cache', None)
        if cache is None:
            self._descriptor_cache = cache = OrderedDict()
            self._descriptor_cache_size = 0
        key = (value, text)
        if key in cache:
            descriptor, _ = cache[key]
            cache.move_to_end(key)
            return descriptor
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError('Invalid recovery descriptor encoding') from error
        descriptor = self._validated(raw)
        if text != self._text(raw):
            raise ValueError('Recovery descriptor and diagnostic text disagree')
        cost = len(value) + len(text) * 2 + len(raw) * 2
        if cost <= self.descriptor_cache_bytes:
            while cache and (len(cache) >= self.descriptor_cache_entries
                    or self._descriptor_cache_size + cost > self.descriptor_cache_bytes):
                _, (_, previous_cost) = cache.popitem(last=False)
                self._descriptor_cache_size -= previous_cost
            cache[key] = (descriptor, cost)
            self._descriptor_cache_size += cost
        return descriptor

    def _file_identity(self, handle):
        identity = FileIdentity()
        self.check(self.file_id(handle, 18, C.byref(identity), C.sizeof(identity)))
        return identity.volume, bytes(identity.identifier).hex()

    def _snapshot_handles(self, handles):
        records = []
        for path, (device, inode, raw) in zip(handles, self._observations(handles.values())):
            self._validated(raw)
            records.append({'path': path, 'device': device, 'inode': inode,
                            'descriptor': base64.b64encode(raw).decode('ascii'),
                            'sddl': self._text(raw)})
        return records

    def _observations(self, handles):
        handles = tuple(handles)
        if not handles:
            return
        if handles[0].library.batch_acl_reads:
            from backend.core.windows_acl_native import observe_handles
            yield from observe_handles(handles, self.security_information)
        else:
            for handle in handles:
                raw = self._security_bytes(handle)
                yield (*self._file_identity(handle), raw)

    def snapshot(self):
        self.verify_inventory()
        records = self._snapshot_handles(self.handles)
        self.verify_inventory()
        return records

    def boundaries(self):
        return self._snapshot_handles({p: h for p, h in self.ancestors.items()
                                       if p not in self.handles})

    def verify_boundaries(self, expected):
        self._verify_boundaries(self._bind_boundaries(expected))

    def _bind_boundaries(self, expected):
        current = [(path, handle) for path, handle in self.ancestors.items() if path not in self.handles]
        if len(current) != len(expected):
            raise SecurityConflict('Parent security boundary changed; retain recovery record')
        bound = []
        for (path, handle), record in zip(current, expected):
            if path != record['path']:
                raise SecurityConflict('Parent security boundary changed; retain recovery record')
            key = self.record_descriptor(record).comparison_key()
            bound.append((handle, (record['device'], record['inode']), key))
        return bound

    def _verify_boundaries(self, expected):
        observations = self._observations(handle for handle, _, _ in expected)
        for (_, identity, key), (device, inode, raw) in zip(expected, observations):
            actual_identity = (device, inode)
            observed = self._validated(raw).comparison_key()
            if actual_identity != identity or observed != key:
                raise SecurityConflict('Parent security boundary changed; retain recovery record')

    def _comparison_snapshot(self):
        """Inspect live state without formatting records for the recovery journal."""
        self.verify_inventory()
        rows = []
        for path, (device, inode, raw) in zip(self.handles, self._observations(self.handles.values())):
            descriptor = self._validated(raw)
            rows.append(((path, device, inode), descriptor.comparison_key()))
        self.verify_inventory()
        return rows

    @staticmethod
    def identity(records):
        return [(r['path'], r['device'], r['inode']) for r in records]

    def plan(self, records, workspace, principal):
        from backend.core.windows_acl_plan import plan_descriptor

        @lru_cache(maxsize=32)
        def planned_descriptor(descriptor, directory, workspace_object):
            raw = plan_descriptor(descriptor, principal,
                directory=directory, workspace=workspace_object).to_bytes()
            self._validated(raw)
            return base64.b64encode(raw).decode('ascii'), self._text(raw)

        # A runtime contains many files with identical descriptors. Reuse the
        # pure transformation during this plan while still recording and
        # validating each object's identity and live descriptor separately.
        planned = []
        for record in records:
            path = Path(record['path'])
            content, text = planned_descriptor(self.record_descriptor(record),
                record['path'] in self.children, path.is_relative_to(workspace))
            planned.append(dict(record, descriptor=content, sddl=text))
        return planned

    def _write_descriptor(self, handle, raw):
        from backend.core.windows_acl_native import write_descriptor
        with security_privileges():
            write_descriptor(handle, raw)

    def restore(self, records, *, expected, allow_original=False, boundaries=None):
        """Replace a prevalidated tree only from journaled, known states.

        Management must serialize callers externally and stop tree users.
        Held handles pin identities, not WRITE_DAC: these checks detect drift
        but are not an atomic security-descriptor compare-and-swap.
        """
        if not self.writable:
            raise PermissionError('Read-only security tree cannot restore descriptors')
        bound = self._bind_boundaries(boundaries) if boundaries is not None else ()
        self._verify_boundaries(bound)
        current = self._comparison_snapshot()
        identities = [identity for identity, _ in current]
        if identities != self.identity(records) or identities != self.identity(expected):
            raise SecurityConflict('Configuration tree changed; refusing descriptor restoration')
        desired = [self.record_descriptor(r) for r in records]
        previous = [self.record_descriptor(r) for r in expected]
        # Validate ALL records and current states before the first write.
        allowed = [{before.comparison_key()} | ({after.comparison_key()} if allow_original else set())
                   for before, after in zip(previous, desired)]
        for (_, observed), states in zip(current, allowed):
            if observed not in states:
                raise SecurityConflict('Security descriptor conflict; refusing to overwrite external changes')
        for record, target, states in zip(records, desired, allowed):
            if bound:
                self._verify_boundaries(bound)
            handle = self.handles[record['path']]
            self._information(handle)
            observed = self._validated(self._security_bytes(handle)).comparison_key()
            if observed not in states:
                raise SecurityConflict('Security descriptor conflict before write; retain recovery record')
            if observed != target.comparison_key():
                self._write_descriptor(handle, target.to_bytes())
                if self._validated(self._security_bytes(handle)).comparison_key() != target.comparison_key():
                    raise OSError('Descriptor restoration did not reproduce recorded security')
        self._verify_boundaries(bound)
        final = self._comparison_snapshot()
        if any(observed != wanted.comparison_key() for (_, observed), wanted in zip(final, desired)):
            raise SecurityConflict('Security descriptors changed during restoration')

    def close(self):
        if getattr(self, '_descriptor_cache', None) is not None:
            self._descriptor_cache.clear()
            self._descriptor_cache_size = 0
        if getattr(self, '_validated_cache', None) is not None:
            self._validated_cache.clear()
            self._validated_cache_size = 0
        if getattr(self, '_text_cache', None) is not None:
            self._text_cache.clear()
            self._text_cache_size = 0
        # Preserve failed resources for a bounded retry. Never erase ownership
        # just because a close was attempted.
        failure = None
        for owned in (self.handles, self.ancestors):
            for path, handle in reversed(list(owned.items())):
                try:
                    handle.close()
                    del owned[path]
                except OSError as error:
                    failure = error
        if failure is not None:
            raise failure

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def canonical_security(value):
    """Legacy display helper; authorization comparisons use binary descriptors.

    Preserve every inheritance/protection flag. Only absent/NULL/empty label
    representations with no SACL entries can have the same displayed meaning.
    """
    return re.sub(r'S:((?:AI|AR|P)*)(?:NO_ACCESS_CONTROL)?$',
                  lambda m: 'S:' + m[1] if m[1] else '', value)


@contextmanager
def management_lock(*, wait_ms=0):
    """Serialize cooperating Windows ACL operators across processes/sessions.

    This lock is not an ACL freeze or a running Host lease. External permission
    editors are detected by descriptor conflict checks, not blocked by it.
    """
    if type(wait_ms) is not int or not 0 <= wait_ms <= 30000:
        raise ValueError('ACL management wait must be between 0 and 30000 ms')
    if os.name != 'nt':
        raise OSError('Windows ACL management is required')
    kernel = C.WinDLL('kernel32', use_last_error=True)
    create = _fn(kernel, 'CreateMutexExW',
        [C.c_void_p, W.LPCWSTR, W.DWORD, W.DWORD], W.HANDLE)
    wait = _fn(kernel, 'WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)
    release = _fn(kernel, 'ReleaseMutex', [W.HANDLE], W.BOOL)
    close = _fn(kernel, 'CloseHandle', [W.HANDLE], W.BOOL)
    handle = create(None, r'Global\GitgoSandboxAclManagement.v1', 0, 0x100001)
    SecurityTree.check(handle)
    acquired = False
    try:
        status = wait(handle, wait_ms)
        if status == 0x80:  # WAIT_ABANDONED grants ownership, but recovery is explicit.
            acquired = True
            raise SecurityConflict('Previous ACL operator terminated; inspect recovery records before retry')
        if status == 0x102:
            raise SecurityManagementBusy('Another ACL configuration operation is active')
        if status != 0:
            SecurityTree.check(False)
        acquired = True
        yield
    finally:
        try:
            if acquired:
                SecurityTree.check(release(handle))
        finally:
            close(handle)
