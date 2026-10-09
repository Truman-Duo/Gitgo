"""Operator-only DACL/MIC recovery using stable, non-redirected file handles."""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import os
import re
from pathlib import Path

DACL, LABEL = 4, 16


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
        self.access = 0x20001 | (0x40000 | 0x80000 if writable else 0)  # READ_DATA/LIST_DIRECTORY, READ_CONTROL, WRITE_DAC/OWNER
        try:
            for root in roots:
                path = Path(root)
                for ancestor in reversed(path.parents):
                    key = str(ancestor)
                    if key not in self.ancestors:
                        handle = self._open(ancestor, 0xA0)  # TRAVERSE (execute-sharing) + READ_ATTRIBUTES
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

    def snapshot(self):
        self.verify_inventory()
        records = []
        for path, handle in self.handles.items():
            descriptor, text = C.c_void_p(), W.LPWSTR()
            status = self.get_security(handle, 1, DACL | LABEL, None, None, None, None, C.byref(descriptor))
            if status:
                raise C.WinError(status)
            try:
                self.check(self.to_sddl(descriptor, 1, DACL | LABEL, C.byref(text), None))
                identity = FileIdentity()
                self.check(self.file_id(handle, 18, C.byref(identity), C.sizeof(identity)))
                records.append({'path': path, 'device': identity.volume,
                                'inode': bytes(identity.identifier).hex(), 'sddl': text.value})
            finally:
                self.free(C.cast(text, C.c_void_p))
                self.free(descriptor)
        self.verify_inventory()
        return records

    def restore(self, records):
        # Validate EVERY identity before any mutation. Changed/missing objects
        # require operator inspection; never apply an old ACL to a replacement.
        current = self.snapshot()
        expected = [(r['path'], r['device'], r['inode']) for r in records]
        if expected != [(r['path'], r['device'], r['inode']) for r in current]:
            raise OSError('Configuration tree changed; refusing descriptor restoration')
        descriptors = []
        try:
            # Decode every descriptor before applying any of them. A damaged
            # recovery record cannot cause a partial restoration first.
            for record in records:
                descriptor = C.c_void_p()
                self.check(self.from_sddl(record['sddl'], 1, C.byref(descriptor), None))
                descriptors.append(descriptor)
                present, defaulted, dacl = W.BOOL(), W.BOOL(), C.c_void_p()
                self.check(self.dacl(descriptor, C.byref(present), C.byref(dacl), C.byref(defaulted)))
                if not present.value:
                    raise ValueError('Recovery descriptor has no recorded DACL')
            for record, descriptor in zip(records, descriptors):
                control, revision = W.WORD(), W.DWORD()
                self.check(self.control(descriptor, C.byref(control), C.byref(revision)))
                # SetSecurityInfo propagates inheritance across descendants,
                # rewriting explicit ACEs even without UNPROTECTED. Recovery
                # must restore each recorded object, not recalculate its ACL.
                # The documented user-mode NtSetSecurityObject entry applies
                # this validated self-relative descriptor to the held handle.
                # LABEL replaces only MIC, leaving audit ACEs untouched.
                protection = 0x80000000 if control.value & 0x1000 else 0x20000000
                status = self.set_object_security(self.handles[record['path']],
                    DACL | LABEL | protection, descriptor)
                if status != 0:
                    raise C.WinError(self.status_to_error(status))
        finally:
            for descriptor in descriptors:
                self.free(descriptor)
        if [canonical_security(r['sddl']) for r in self.snapshot()] != [canonical_security(r['sddl']) for r in records]:
            raise OSError('Descriptor restoration did not reproduce the original DACL/MIC')

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
    """Compare authorization, preserving every ACE and protected-DACL flag.

    Native descriptor updates can recompute the AI bookkeeping flag. An absent/empty/null
    *label* ACL has the same default MIC policy. Never normalize a null DACL:
    it allows access, whereas an empty DACL denies access.
    """
    value = re.sub(r'([DS]:)([^()]*)', lambda match:
                   match[1] + match[2].replace('AI', ''), value)
    for empty_label in ('S:NO_ACCESS_CONTROL', 'S:'):
        if value.endswith(empty_label):
            return value[:-len(empty_label)]
    return value
