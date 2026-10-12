"""Operator-only DACL/MIC recovery using stable, non-redirected file handles."""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import os
import base64
import struct
import re
from contextlib import contextmanager
from pathlib import Path

from backend.core.windows_security_descriptor import SecurityDescriptor, MAX_DESCRIPTOR_BYTES
from backend.core.windows_security_privileges import security_privileges

DACL, LABEL = 4, 16
FULL_SECURITY = 0x1FF


class SecurityConflict(OSError):
    """An identity or descriptor changed outside this configuration transaction."""


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
    def __init__(self, roots, *, writable=True):
        if os.name != 'nt':
            raise OSError('Windows security descriptors are required')
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.advapi = C.WinDLL('advapi32', use_last_error=True)
        self.ntdll = C.WinDLL('ntdll', use_last_error=True)
        self.handles = {}
        self.ancestors = {}
        self.children = {}
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
        self.access = 0x01020089 | (0x40000 | 0x80000 if writable else 0)  # READ_DATA/LIST_DIRECTORY, READ_CONTROL, WRITE_DAC/OWNER
        try:
            with security_privileges():
                for root in roots:
                    path = Path(root)
                    for ancestor in reversed(path.parents):
                        key = str(ancestor)
                        if key not in self.ancestors:
                            handle = self._open(ancestor, 0x010200A0)  # TRAVERSE (execute-sharing) + READ_ATTRIBUTES
                            self.ancestors[key] = handle
                            self._information(handle)
                    self._visit(path)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def check(ok):
        if not ok:
            raise C.WinError(C.get_last_error())

    def _visit(self, path):
        key = str(path)
        if key in self.handles:
            return
        if len(self.handles) >= 100_000:
            raise OSError('Configuration tree exceeds 100000 objects')
        # READ_DATA/LIST_DIRECTORY is necessary for Windows sharing checks;
        # security-only handles do not prevent rename despite a zero delete
        # share flag. Hold every ancestor and target with actual read access.
        # Mutations always use these same validated handles.
        handle = self._open(path, self.access)
        self.handles[key] = handle
        info = self._information(handle)
        if info.attributes & 0x10:
            children = sorted(path.iterdir())
            self.children[key] = {str(child) for child in children}
            for child in children:
                self._visit(child)

    def _open(self, path, access):
        handle = self.open_file(str(path), access, 1 | 2, None, 3, 0x02000000 | 0x00200000, None)
        if handle == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        return handle

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
            if path in self.children and {str(p) for p in Path(path).iterdir()} != self.children[path]:
                raise OSError('Configuration tree changed while security handles were held')

    def _security_bytes(self, handle):
        # GetSecurityInfo(SE_FILE_OBJECT) can return reconstructed inheritance
        # flags on legacy ACLs. Recovery needs the descriptor stored by the
        # kernel, not that synthesized view. Query every security component,
        # including MIC, audit, resource attributes and access filters.
        needed = W.DWORD()
        status = self.query_security(handle, FULL_SECURITY, None, 0, C.byref(needed))
        if status != -1073741789:  # STATUS_BUFFER_TOO_SMALL
            if status:
                raise C.WinError(self.status_to_error(status))
            raise OSError('Unexpected Windows descriptor query result')
        for _ in range(3):
            if not 20 <= needed.value <= MAX_DESCRIPTOR_BYTES:
                raise ValueError('Windows descriptor exceeds recovery bounds')
            buffer = C.create_string_buffer(needed.value)
            status = self.query_security(handle, FULL_SECURITY, buffer, len(buffer), C.byref(needed))
            if status == -1073741789:
                continue
            if status:
                raise C.WinError(self.status_to_error(status))
            if not 20 <= needed.value <= len(buffer):
                raise ValueError('Invalid Windows descriptor query length')
            raw = buffer.raw[:needed.value]
            self._validated(raw)
            return raw
        raise SecurityConflict('Security descriptor changed repeatedly during snapshot')

    def _validated(self, raw):
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
        return descriptor

    def _text(self, raw):
        buffer = C.create_string_buffer(raw)
        text = W.LPWSTR()
        self.check(self.to_sddl(buffer, 1, DACL | LABEL, C.byref(text), None))
        try:
            return text.value
        finally:
            self.free(C.cast(text, C.c_void_p))

    def record_descriptor(self, record):
        value = record.get('descriptor')
        if not isinstance(value, str) or len(value) > ((MAX_DESCRIPTOR_BYTES + 2) // 3) * 4:
            raise ValueError('Recovery requires a bounded full binary descriptor')
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError('Invalid recovery descriptor encoding') from error
        descriptor = self._validated(raw)
        if record.get('sddl') != self._text(raw):
            raise ValueError('Recovery descriptor and diagnostic text disagree')
        return descriptor

    def _snapshot_handles(self, handles):
        records = []
        for path, handle in handles.items():
            raw = self._security_bytes(handle)
            identity = FileIdentity()
            self.check(self.file_id(handle, 18, C.byref(identity), C.sizeof(identity)))
            records.append({'path': path, 'device': identity.volume,
                            'inode': bytes(identity.identifier).hex(),
                            'descriptor': base64.b64encode(raw).decode('ascii'),
                            'sddl': self._text(raw)})
        return records

    def snapshot(self):
        self.verify_inventory()
        records = self._snapshot_handles(self.handles)
        self.verify_inventory()
        return records

    def boundaries(self):
        return self._snapshot_handles({p: h for p, h in self.ancestors.items()
                                       if p not in self.handles})

    def verify_boundaries(self, expected):
        current = self.boundaries()
        if self.identity(current) != self.identity(expected) or any(
                self.record_descriptor(a).comparison_key() != self.record_descriptor(b).comparison_key()
                for a, b in zip(current, expected)):
            raise SecurityConflict('Parent security boundary changed; retain recovery record')

    @staticmethod
    def identity(records):
        return [(r['path'], r['device'], r['inode']) for r in records]

    def plan(self, records, workspace, principal):
        from backend.core.windows_acl_plan import plan_descriptor
        planned = []
        for record in records:
            path = Path(record['path'])
            descriptor = self.record_descriptor(record)
            workspace_object = path == workspace or path.is_relative_to(workspace)
            raw = plan_descriptor(descriptor, principal,
                directory=record['path'] in self.children,
                workspace=workspace_object).to_bytes()
            self._validated(raw)
            planned.append(dict(record, descriptor=base64.b64encode(raw).decode('ascii'),
                                sddl=self._text(raw)))
        return planned

    def _write_descriptor(self, handle, raw):
        # One fixed SECURITY_DATA stream. Never accept caller-controlled stream
        # headers or content/link/alternate-data streams from a recovery record.
        payload = struct.pack('<IIqI', 3, 2, len(raw), 0) + raw
        data = C.create_string_buffer(payload)
        written, context = W.DWORD(), C.c_void_p()
        with security_privileges():
            failure = None
            try:
                self.check(self.backup_write(handle, data, len(payload), C.byref(written),
                                            False, True, C.byref(context)))
                if written.value != len(payload):
                    raise OSError('Incomplete Windows security stream write')
            except BaseException as error:
                failure = error
                raise
            finally:
                try:
                    self.check(self.backup_write(handle, None, 0, C.byref(written),
                                                True, True, C.byref(context)))
                except OSError as cleanup_error:
                    if failure is None:
                        raise
                    failure.add_note('Security stream cleanup also failed: ' + str(cleanup_error))

    def restore(self, records, *, expected, allow_original=False, boundaries=None):
        """Replace a prevalidated tree only from journaled, known states.

        Management must serialize callers externally and stop tree users.
        Held handles pin identities, not WRITE_DAC: these checks detect drift
        but are not an atomic security-descriptor compare-and-swap.
        """
        if not self.writable:
            raise PermissionError('Read-only security tree cannot restore descriptors')
        if boundaries is not None:
            self.verify_boundaries(boundaries)
        current = self.snapshot()
        identities = self.identity(current)
        if identities != self.identity(records) or identities != self.identity(expected):
            raise SecurityConflict('Configuration tree changed; refusing descriptor restoration')
        desired = [self.record_descriptor(r) for r in records]
        previous = [self.record_descriptor(r) for r in expected]
        # Validate ALL records and current states before the first write.
        allowed = [{before.comparison_key()} | ({after.comparison_key()} if allow_original else set())
                   for before, after in zip(previous, desired)]
        for observed, states in zip(current, allowed):
            if self.record_descriptor(observed).comparison_key() not in states:
                raise SecurityConflict('Security descriptor conflict; refusing to overwrite external changes')
        for record, target, states in zip(records, desired, allowed):
            if boundaries is not None:
                self.verify_boundaries(boundaries)
            handle = self.handles[record['path']]
            self._information(handle)
            observed = self._validated(self._security_bytes(handle)).comparison_key()
            if observed not in states:
                raise SecurityConflict('Security descriptor conflict before write; retain recovery record')
            if observed != target.comparison_key():
                self._write_descriptor(handle, target.to_bytes())
                if self._validated(self._security_bytes(handle)).comparison_key() != target.comparison_key():
                    raise OSError('Descriptor restoration did not reproduce recorded security')
        if boundaries is not None:
            self.verify_boundaries(boundaries)
        final = self.snapshot()
        if any(self.record_descriptor(actual).comparison_key() != wanted.comparison_key()
               for actual, wanted in zip(final, desired)):
            raise SecurityConflict('Security descriptors changed during restoration')

    def close(self):
        for handle in reversed(list(self.handles.values())):
            self.close_handle(handle)
        self.handles.clear()
        for handle in reversed(list(self.ancestors.values())):
            self.close_handle(handle)
        self.ancestors.clear()

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
def management_lock():
    """Serialize cooperating Windows ACL operators across processes/sessions.

    This lock is not an ACL freeze or a running Host lease. External permission
    editors are detected by descriptor conflict checks, not blocked by it.
    """
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
        status = wait(handle, 0)
        if status == 0x80:  # WAIT_ABANDONED grants ownership, but recovery is explicit.
            acquired = True
            raise SecurityConflict('Previous ACL operator terminated; inspect recovery records before retry')
        if status == 0x102:
            raise SecurityConflict('Another ACL configuration operation is active')
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
