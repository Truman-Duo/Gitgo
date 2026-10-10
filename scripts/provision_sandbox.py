"""Preview or transactionally provision dedicated Windows sandbox ACLs.

Operator entry only. Stop project processes before applying or restoring.
Recovery restores security, not file contents, and refuses a changed tree.
"""
from __future__ import annotations
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core.sandbox import SandboxPolicy, trusted_runtime_roots, validate_runtime_separation
from backend.core.executable_identity import windows_system_executable

MAX_RECORD_BYTES = 64 * 1024 * 1024


def concrete_roots(paths):
    roots = sorted(set(Path(p).resolve(strict=True) for p in paths),
                   key=lambda p: (len(p.parts), str(p)))
    if any(not p.is_dir() or p == Path(p.anchor) for p in roots):
        raise ValueError('A concrete directory is required, never a drive root')
    return roots


def record_path(path, roots):
    target = Path(path).resolve(strict=False)
    if any(target.is_relative_to(root) for root in (*roots, *trusted_runtime_roots())):
        raise ValueError('Recovery record must be outside workspace and exposed runtime trees')
    if not target.parent.is_dir():
        raise ValueError('Recovery record requires an existing private Host directory')
    return target


def save_record(path, payload, *, create=False):
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError('Recovery record exceeds 64 MiB')
    if create:
        with open(path, 'xb') as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        return
    descriptor, temporary = tempfile.mkstemp(prefix='.gitgo-acl-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sid_string(api, sid):
    value = W.LPWSTR()
    convert = api.advapi.ConvertSidToStringSidW
    convert.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
    convert.restype = W.BOOL
    api.check(convert(sid, C.byref(value)))
    try:
        return value.value
    finally:
        free = api.kernel.LocalFree
        free.argtypes = [C.c_void_p]
        free.restype = C.c_void_p
        free(C.cast(value, C.c_void_p))


def provision(workspace, runtimes, *, record=None, apply=False, run=None):
    from backend.core.sandbox_windows import WindowsApi
    from backend.core.windows_acl import SecurityTree
    policy = SandboxPolicy(workspace)
    runtimes = concrete_roots(runtimes)
    validate_runtime_separation(policy.workspace, runtimes)
    roots = concrete_roots([policy.workspace, *runtimes])
    destination = record_path(record, roots) if record is not None else None
    if apply and destination is None:
        raise ValueError('--apply requires --record in a private external Host directory')
    api = WindowsApi()
    sid = api.preview_profile_sid(policy.profile_name)
    try:
        principal = sid_string(api, sid)
    finally:
        api.free_sid(sid)
    # Never allow cwd/PATH to select the ACL editor in an operator terminal.
    editor = str(windows_system_executable('System32/icacls.exe'))
    commands = [[editor, str(root), '/grant', f'*{principal}:(OI)(CI)RX'] for root in runtimes]
    commands += [[editor, str(policy.workspace), '/grant', f'*{principal}:(OI)(CI)M'],
                 [editor, str(policy.workspace), '/setintegritylevel', '(OI)(CI)L']]
    print('Profile:', policy.profile_name)
    for command in commands:
        print(subprocess.list2cmdline(command))
    if not apply:
        print('Read-only preview: no profile, ACL or recovery record created.')
        return
    execute = run or subprocess.run
    # All objects are opened with restoration rights and held against deletion
    # BEFORE a profile or ACL is changed. The original record is durable first.
    with SecurityTree(roots) as tree:
        payload = {'version': 1, 'phase': 'prepared', 'workspace': str(policy.workspace),
                   'runtimes': [str(p) for p in runtimes], 'profile': policy.profile_name,
                   'records': tree.snapshot()}
        save_record(destination, payload, create=True)
        try:
            sid = api.profile_sid(policy.profile_name)
            api.free_sid(sid)
            for command in commands:
                execute(command, check=True, capture_output=True)
            tree.verify_inventory()
            payload['phase'] = 'applied'
            save_record(destination, payload)
        except BaseException:
            try:
                tree.restore(payload['records'])
                payload['phase'] = 'rolled_back'
                save_record(destination, payload)
            except BaseException as recovery_error:
                payload['phase'] = 'rollback_failed'
                try:
                    save_record(destination, payload)
                except (OSError, ValueError):
                    pass
                raise RuntimeError('Configuration failed and rollback is incomplete; '
                                   'retain the external record for operator recovery') from recovery_error
            raise
    print('Applied. Retain private recovery record:', destination)


def restore_record(path, *, apply=False):
    from backend.core.windows_acl import SecurityTree
    path = Path(path).resolve(strict=True)
    with path.open('rb') as handle:
        raw = handle.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError('Recovery record exceeds 64 MiB')
    payload = json.loads(raw)
    if payload.get('version') != 1 or payload.get('phase') not in {'prepared', 'applied', 'rollback_failed'}:
        raise ValueError('Record is invalid, already recovered or not a supported recovery phase')
    policy = SandboxPolicy(Path(payload['workspace']))
    runtimes = concrete_roots(payload['runtimes'])
    validate_runtime_separation(policy.workspace, runtimes)
    roots = concrete_roots([policy.workspace, *runtimes])
    record_path(path, roots)
    if payload['profile'] != policy.profile_name:
        raise ValueError('Recovery profile does not match the canonical workspace')
    with SecurityTree(roots, writable=apply) as tree:
        current = tree.snapshot()
        identity = lambda records: [(r['path'], r['device'], r['inode']) for r in records]
        if identity(current) != identity(payload['records']):
            raise ValueError('Configuration tree changed; inspect it before recovery')
        if not apply:
            print('Read-only restore preview:', len(current), 'matching objects; no ACL changes.')
            return
        tree.restore(payload['records'])
        payload['phase'] = 'restored'
        save_record(path, payload)
    print('Original DACL authorization and MIC restored; profile retained:', policy.profile_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--runtime', action='append', default=[], type=Path)
    parser.add_argument('--record', type=Path, help='New private external Host recovery file, required for --apply')
    parser.add_argument('--restore', type=Path, help='Preview restoration from an existing recovery record')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Windows provisioning only; Linux uses bubblewrap mounts')
    if args.restore:
        if args.workspace or args.runtime or args.record:
            parser.error('--restore cannot be combined with provisioning targets')
        restore_record(args.restore, apply=args.apply)
    elif args.workspace:
        provision(args.workspace, args.runtime, record=args.record, apply=args.apply)
    else:
        parser.error('--workspace or --restore is required')


if __name__ == '__main__':
    main()
