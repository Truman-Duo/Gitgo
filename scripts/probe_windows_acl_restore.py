"""Diagnose native ACL restoration only in a fresh owned temporary tree."""
from __future__ import annotations
import ctypes as C
from contextlib import contextmanager
from ctypes import wintypes as W
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core.windows_acl import SecurityTree, canonical_security, _fn


def safe(sddl):
    return re.sub(r'S-1-[0-9-]+', lambda match:
        'principal-' + hashlib.sha256(match[0].encode()).hexdigest()[:12], sddl)


class Luid(C.Structure):
    _fields_ = [('low', W.DWORD), ('high', W.LONG)]


class Privileges(C.Structure):
    _fields_ = [('count', W.DWORD), ('luid', Luid), ('attributes', W.DWORD)]


@contextmanager
def security_privilege(tree):
    token = W.HANDLE()
    open_token = _fn(tree.advapi, 'OpenProcessToken',
                    [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL)
    lookup = _fn(tree.advapi, 'LookupPrivilegeValueW',
                 [W.LPCWSTR, W.LPCWSTR, C.POINTER(Luid)], W.BOOL)
    adjust = _fn(tree.advapi, 'AdjustTokenPrivileges',
                 [W.HANDLE, W.BOOL, C.c_void_p, W.DWORD, C.c_void_p, C.c_void_p], W.BOOL)
    tree.check(open_token(C.c_void_p(-1), 0x20 | 8, C.byref(token)))
    previous, desired, returned = Privileges(), Privileges(), W.DWORD()
    try:
        tree.check(lookup(None, 'SeSecurityPrivilege', C.byref(desired.luid)))
        desired.count, desired.attributes = 1, 2
        C.set_last_error(0)
        tree.check(adjust(token, False, C.byref(desired), C.sizeof(previous),
                          C.byref(previous), C.byref(returned)))
        yield C.get_last_error() != 1300
    finally:
        tree.check(adjust(token, False, C.byref(previous), 0, None, None))
        tree.close_handle(token)


def main():
    if sys.platform != 'win32':
        raise SystemExit('Windows only')
    results = []
    for mode, mask in [('native-original', None), ('recorded', 0), ('dacl-ai', 0x400), ('dacl-sacl-ai', 0xC00), ('dacl-only', 0), ('label-then-dacl', 0), ('dacl-then-label', 0), ('set-file', 0), ('empty-label', 0), ('empty-label-then-dacl', 0), ('label-then-protected-dacl', 0), ('label-then-ai-dacl', 0), ('full-sacl', None), ('full-sacl-then-dacl', None)]:
        with tempfile.TemporaryDirectory(prefix='gitgo_acl_probe_') as temporary:
            workspace = Path(temporary).resolve() / 'workspace'
            workspace.mkdir(); (workspace / 'nested').mkdir()
            (workspace / 'nested' / 'file').write_text('test-owned probe', encoding='utf-8')
            with SecurityTree([workspace]) as tree:
                originals = tree.snapshot()
                native = []
                def metadata(desc):
                    control, revision = W.WORD(), W.DWORD()
                    tree.check(tree.control(desc, C.byref(control), C.byref(revision)))
                    dacl = C.c_void_p(); present, defaulted = W.BOOL(), W.BOOL()
                    tree.check(tree.dacl(desc, C.byref(present), C.byref(dacl), C.byref(defaulted)))
                    header = C.string_at(dacl, 8)
                    return {'control': control.value, 'dacl_revision': header[0],
                            'dacl_size': int.from_bytes(header[2:4], 'little'),
                            'dacl_defaulted': bool(defaulted.value)}
                change = _fn(tree.advapi, 'SetSecurityDescriptorControl',
                             [C.c_void_p, W.WORD, W.WORD], W.BOOL)
                evidence = []
                extra_handles = []
                privilege = security_privilege(tree) if mode.startswith('full-sacl') else None
                assigned = privilege.__enter__() if privilege else True
                try:
                    if not assigned:
                        results.append({'mode': mode, 'available': False})
                        continue
                    for original in originals:
                        raw = C.c_void_p()
                        handle = tree.handles[original['path']]
                        query = 4 | 16
                        if mode.startswith('full-sacl'):
                            handle = tree._open(Path(original['path']), tree.access | 0x1000000)
                            extra_handles.append(handle)
                            query = 4 | 8
                        status = tree.get_security(handle, 1,
                            query, None, None, None, None, C.byref(raw))
                        if status:
                            raise C.WinError(status)
                        native.append(raw)
                    for index, (original, raw) in enumerate(zip(originals, native)):
                        desc = raw if mask is None else C.c_void_p()
                        if mask is not None:
                            tree.check(tree.from_sddl(original['sddl'] + ('S:' if mode.startswith('empty-label') else ''), 1, C.byref(desc), None))
                        try:
                            text = W.LPWSTR()
                            tree.check(tree.to_sddl(desc, 1, 4 | 16, C.byref(text), None))
                            try:
                                evidence.append({'native': metadata(raw), 'input': metadata(desc),
                                                 'roundtrip': safe(text.value)})
                            finally:
                                tree.free(C.cast(text, C.c_void_p))
                            if mask:
                                tree.check(change(desc, mask, mask))
                            steps = [4 | 16]
                            if mode == 'full-sacl':
                                steps = [4 | 8]
                            elif mode == 'full-sacl-then-dacl':
                                steps = [8, 4]
                            if mode == 'dacl-only':
                                steps = [4]
                            elif mode == 'label-then-dacl':
                                steps = [16, 4]
                            elif mode == 'dacl-then-label':
                                steps = [4, 16]
                            elif mode in ('empty-label-then-dacl', 'label-then-protected-dacl', 'label-then-ai-dacl'):
                                steps = [16, 4]
                            if mode == 'set-file':
                                set_file = _fn(tree.advapi, 'SetFileSecurityW',
                                               [W.LPCWSTR, W.DWORD, C.c_void_p], W.BOOL)
                                tree.check(set_file(original['path'], 4 | 16, desc))
                            else:
                                for flags in steps:
                                    if flags == 4 and mode == 'label-then-protected-dacl':
                                        tree.check(change(desc, 0x1000, 0x1000))
                                    if flags == 4 and mode == 'label-then-ai-dacl':
                                        tree.check(change(desc, 0x400, 0x400))
                                    status = tree.set_object_security(extra_handles[index] if mode.startswith('full-sacl') else tree.handles[original['path']], flags, desc)
                                    if status != 0:
                                        raise C.WinError(tree.status_to_error(status))
                        finally:
                            if mask is not None:
                                tree.free(desc)
                    actual = tree.snapshot()
                    pairs = [{'original': safe(before['sddl']), 'actual': safe(after['sddl'])}
                             for before, after in zip(originals, actual)]
                    matched = all(canonical_security(before['sddl']) == canonical_security(after['sddl'])
                                  for before, after in zip(originals, actual))
                    results.append({'mode': mode, 'matched': matched, 'descriptor_metadata': evidence,
                                    'descriptors': pairs})
                finally:
                    for raw in native:
                        tree.free(raw)
                    for handle in extra_handles:
                        tree.close_handle(handle)
                    if privilege:
                        privilege.__exit__(None, None, None)
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
