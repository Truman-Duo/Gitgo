"""Diagnose native ACL restoration only in a fresh owned temporary tree."""
from __future__ import annotations
import ctypes as C
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


def main():
    if sys.platform != 'win32':
        raise SystemExit('Windows only')
    results = []
    for mode, mask in [('native-original', None), ('recorded', 0), ('dacl-ai', 0x400), ('dacl-sacl-ai', 0xC00), ('dacl-only', 0), ('label-then-dacl', 0), ('dacl-then-label', 0), ('set-file', 0), ('empty-label', 0), ('empty-label-then-dacl', 0), ('label-then-protected-dacl', 0), ('label-then-ai-dacl', 0)]:
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
                try:
                    for original in originals:
                        raw = C.c_void_p()
                        status = tree.get_security(tree.handles[original['path']], 1,
                            4 | 16, None, None, None, None, C.byref(raw))
                        if status:
                            raise C.WinError(status)
                        native.append(raw)
                    for original, raw in zip(originals, native):
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
                                    status = tree.set_object_security(tree.handles[original['path']], flags, desc)
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
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
