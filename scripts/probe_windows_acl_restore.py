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
    for mode, mask in [('recorded', 0), ('dacl-ai', 0x400), ('dacl-sacl-ai', 0xC00)]:
        with tempfile.TemporaryDirectory(prefix='gitgo_acl_probe_') as temporary:
            workspace = Path(temporary).resolve() / 'workspace'
            workspace.mkdir(); (workspace / 'nested').mkdir()
            (workspace / 'nested' / 'file').write_text('test-owned probe', encoding='utf-8')
            with SecurityTree([workspace]) as tree:
                originals = tree.snapshot()
                controls = []
                change = _fn(tree.advapi, 'SetSecurityDescriptorControl',
                             [C.c_void_p, W.WORD, W.WORD], W.BOOL)
                for original in originals:
                    desc = C.c_void_p(); control = W.WORD(); revision = W.DWORD()
                    tree.check(tree.from_sddl(original['sddl'], 1, C.byref(desc), None))
                    try:
                        tree.check(tree.control(desc, C.byref(control), C.byref(revision)))
                        controls.append(control.value)
                        if mask:
                            tree.check(change(desc, mask, mask))
                        status = tree.set_object_security(tree.handles[original['path']], 4 | 16, desc)
                        if status != 0:
                            raise C.WinError(tree.status_to_error(status))
                    finally:
                        tree.free(desc)
                actual = tree.snapshot()
                pairs = [{'original': safe(before['sddl']), 'actual': safe(after['sddl'])}
                         for before, after in zip(originals, actual)]
                matched = all(canonical_security(before['sddl']) == canonical_security(after['sddl'])
                              for before, after in zip(originals, actual))
                results.append({'mode': mode, 'matched': matched, 'input_controls': controls,
                                'descriptors': pairs})
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
