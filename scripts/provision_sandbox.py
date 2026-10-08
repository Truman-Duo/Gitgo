"""Explicit Windows AppContainer ACL provisioning; never called by tool code.

Run from a trusted operator terminal, not from an Agent tool. Use a dedicated
runtime installation and workspace. Inspect planned paths before --apply.
"""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core.sandbox import SandboxPolicy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--runtime", action="append", default=[], type=Path,
                        help="Dedicated Python/runtime/source directory; read and execute only")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Windows provisioning only; Linux uses bubblewrap mounts.")
    policy = SandboxPolicy(args.workspace)
    roots = [root.resolve(strict=True) for root in args.runtime]
    for root in roots:
        if not root.is_dir() or root == Path(root.anchor):
            parser.error("Runtime access requires a concrete directory, never a drive root.")
    from backend.core.sandbox_windows import WindowsApi
    api = WindowsApi()
    sid = api.profile_sid(policy.profile_name)
    try:
        convert = api.advapi.ConvertSidToStringSidW
        convert.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
        convert.restype = W.BOOL
        sid_text = W.LPWSTR()
        api.check(convert(sid, C.byref(sid_text)))
        try:
            sid_value = sid_text.value
        finally:
            free = api.kernel.LocalFree
            free.argtypes = [C.c_void_p]
            free.restype = C.c_void_p
            free(C.cast(sid_text, C.c_void_p))
    finally:
        api.free_sid(sid)
    commands = [
        ["icacls", str(root), "/grant", f"*{sid_value}:(OI)(CI)RX"]
        for root in roots
    ]
    commands += [
        ["icacls", str(policy.workspace), "/grant", f"*{sid_value}:(OI)(CI)M"],
        ["icacls", str(policy.workspace), "/setintegritylevel", "(OI)(CI)L"],
    ]
    print("Profile:", policy.profile_name)
    print("Use a dedicated workspace: this lowers its mandatory integrity label.")
    for command in commands:
        print(subprocess.list2cmdline(command))
        if args.apply:
            subprocess.run(command, check=True, capture_output=True)
    if not args.apply:
        print("No ACLs changed. Review the paths, then repeat with --apply.")


if __name__ == "__main__":
    main()
