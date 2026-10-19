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
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core.sandbox import SandboxPolicy, trusted_runtime_roots, validate_runtime_separation
from backend.core.windows_acl import SecurityTree, management_lock
from backend.core.windows_security_privileges import ThreadTokenRestoreError

RECORD_VERSION = 3

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


def provision(workspace, runtimes, *, record=None, apply=False):
    with management_lock():
        return _provision(workspace, runtimes, record=record, apply=apply)


def _provision(workspace, runtimes, *, record=None, apply=False):
    from backend.core.sandbox_windows import WindowsApi
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
    print('Profile:', policy.profile_name)
    print('Workspace: modify and Low integrity; dedicated runtimes: read/execute.')
    if not apply:
        print('Read-only preview: no profile, ACL or recovery record created.')
        return
    # Full descriptors and both permitted transaction states are durable before
    # any write. No recursive ACL editor with an unknown partial result is used.
    with SecurityTree(roots) as tree:
        original = tree.snapshot()
        planned = tree.plan(original, policy.workspace, principal)
        payload = {'version': RECORD_VERSION, 'phase': 'prepared',
                   'workspace': str(policy.workspace),
                   'runtimes': [str(p) for p in runtimes], 'profile': policy.profile_name,
                   'records': original, 'planned': planned, 'boundaries': tree.boundaries()}
        save_record(destination, payload, create=True)
        try:
            sid = api.profile_sid(policy.profile_name)
            api.free_sid(sid)
            payload['phase'] = 'applying'
            save_record(destination, payload)
            tree.restore(planned, expected=original, boundaries=payload['boundaries'])
            payload['phase'] = 'applied'
            save_record(destination, payload)
        except ThreadTokenRestoreError:
            # The applying journal is already durable. Do not start rollback
            # under an identity that could not be restored. Stop this operator.
            raise
        except BaseException:
            try:
                payload['phase'] = 'rolling_back'
                save_record(destination, payload)
                tree.restore(original, expected=planned, allow_original=True,
                             boundaries=payload['boundaries'])
                payload['phase'] = 'rolled_back'
                save_record(destination, payload)
            except ThreadTokenRestoreError:
                raise
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
    with management_lock():
        return _restore_record(path, apply=apply)


def _restore_record(path, *, apply=False):
    path = Path(path).resolve(strict=True)
    with path.open('rb') as handle:
        raw = handle.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError('Recovery record exceeds 64 MiB')
    payload = json.loads(raw)
    if payload.get('version') != RECORD_VERSION or payload.get('phase') not in {
            'prepared', 'applying', 'applied', 'rolling_back', 'rollback_failed', 'restoring', 'restore_failed'}:
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
        if tree.identity(current) != tree.identity(payload['records']) or tree.identity(current) != tree.identity(payload['planned']):
            raise ValueError('Configuration tree changed; inspect it before recovery')
        tree.verify_boundaries(payload['boundaries'])
        allow_original = payload['phase'] != 'applied'
        for now, before, after in zip(current, payload['records'], payload['planned']):
            states = {tree.record_descriptor(after).comparison_key()}
            original_key = tree.record_descriptor(before).comparison_key()
            if allow_original:
                states.add(original_key)
            if tree.record_descriptor(now).comparison_key() not in states:
                raise ValueError('Security descriptor conflict; refusing to overwrite external changes')
        if not apply:
            print('Read-only restore preview:', len(current), 'matching objects; no ACL changes.')
            return
        payload['phase'] = 'restoring'
        save_record(path, payload)
        try:
            tree.restore(payload['records'], expected=payload['planned'],
                         allow_original=allow_original, boundaries=payload['boundaries'])
            payload['phase'] = 'restored'
            save_record(path, payload)
        except ThreadTokenRestoreError:
            raise
        except BaseException:
            payload['phase'] = 'restore_failed'
            try:
                save_record(path, payload)
            except (OSError, ValueError):
                pass
            raise
    print('Original owner/group, DACL, audit and MIC restored; profile retained:', policy.profile_name)


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
