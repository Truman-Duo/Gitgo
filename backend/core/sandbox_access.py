"""Trusted workspace lifecycle access, never a model-selectable permission tool.

The operator's full-descriptor recovery remains separate. This service uses
only rights already present on the concrete runtime/workspace objects, keeps
private intent/outcome journals, and removes only grants it actually owns.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import threading

from .sandbox import SandboxPolicy, trusted_runtime_roots, validate_runtime_separation, SandboxDenied
from .sandbox_access_records import concrete_roots, record_path, save_record, sid_string

_lock = threading.RLock()


def _minimal_roots(paths):
    result = []
    for path in concrete_roots(paths):
        if not any(path.is_relative_to(parent) for parent in result):
            result.append(path)
    return result


class WorkspaceAccess:
    def __init__(self, workspace, runtimes, record, *, shallow_runtimes=(), metadata_roots=()):
        self.policy = SandboxPolicy(Path(workspace))
        self.runtimes = _minimal_roots(runtimes)
        validate_runtime_separation(self.policy.workspace, self.runtimes)
        self.shallow_runtimes = concrete_roots(shallow_runtimes)
        validate_runtime_separation(self.policy.workspace, self.shallow_runtimes)
        self.roots = _minimal_roots([self.policy.workspace, *self.runtimes])
        self.shallow_runtimes = [p for p in self.shallow_runtimes
            if not any(p.is_relative_to(parent) for parent in self.roots)]
        self.roots.extend(self.shallow_runtimes)
        self.metadata_roots = concrete_roots(metadata_roots)
        if any(p != self.policy.workspace.parent for p in self.metadata_roots):
            raise SandboxDenied("SANDBOX_POLICY_INVALID", "Only the immediate execution parent may receive metadata access")
        if any(p in self.roots for p in self.metadata_roots):
            raise SandboxDenied("SANDBOX_POLICY_INVALID", "Metadata-only access must not overlap another access role")
        self.roots.extend(self.metadata_roots)
        self.record = record_path(record, self.roots)
        self.payload = None

    def prepare(self):
        from .windows_acl import management_lock
        from .windows_access import AccessTree
        from .sandbox_windows import WindowsApi
        from .windows_acl_plan import _entries, _profile_sid
        with _lock, management_lock(wait_ms=30000):
            api = WindowsApi()
            sid = api.preview_profile_sid(self.policy.profile_name)
            try: principal = sid_string(api, sid)
            finally: api.free_sid(sid)
            with AccessTree(self.roots, shallow=self.shallow_runtimes, metadata=self.metadata_roots) as tree:
                original = tree.snapshot()
                principal_bytes = _profile_sid(principal)
                if any(any(principal_bytes in ace for ace in _entries(tree.record_descriptor(r).dacl)) for r in original):
                    raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Workspace sandbox principal already has access; inspect its existing configuration instead of replacing ownership.')
                planned = tree.plan(original, self.policy.workspace, principal)
                payload = dict(version=1, kind='scoped_workspace_access', phase='prepared',
                    workspace=str(self.policy.workspace), runtimes=[str(p) for p in self.runtimes],
                    shallow_runtimes=[str(p) for p in self.shallow_runtimes],
                    metadata_roots=[str(p) for p in self.metadata_roots],
                    profile=self.policy.profile_name, principal=principal,
                    records=original, planned=planned, boundaries=tree.boundaries())
                save_record(self.record, payload, create=True)
                self.payload = payload
                try:
                    sid = api.profile_sid(self.policy.profile_name)
                    api.free_sid(sid)
                    self.payload['phase'] = 'applying'
                    save_record(self.record, self.payload)
                    tree.restore(planned, expected=original, boundaries=self.payload['boundaries'])
                    self.payload['phase'] = 'applied'
                    save_record(self.record, self.payload)
                except BaseException:
                    self.payload['phase'] = 'prepare_failed'
                    # Do not hide the original failure if journal acknowledgement
                    # is also lost. No model/native code has been admitted yet.
                    try: save_record(self.record, self.payload)
                    except OSError: pass
                    raise
        return self

    def close(self):
        if self.payload is None or self.payload.get('phase') == 'revoked':
            return
        from .windows_acl import management_lock
        from .windows_access import AccessTree
        from .sandbox_windows import WindowsApi, _PENDING_JOBS
        if _PENDING_JOBS:
            raise SandboxDenied('SANDBOX_CLEANUP_PENDING', 'Native processes are not retired; keep workspace access and recovery journal for inspection.')
        with _lock, management_lock(wait_ms=30000):
            self.payload['phase'] = 'revoking'
            save_record(self.record, self.payload)
            try:
                # The working tree can contain new files. Pin its current
                # inventory; remove only our SID, keeping peer task grants.
                with AccessTree(self.roots, shallow=self.shallow_runtimes, metadata=self.metadata_roots) as tree:
                    current = tree.snapshot()
                    planned = tree.revoke(current, self.payload['principal'])
                    tree.restore(planned, expected=current, boundaries=tree.boundaries())
                api = WindowsApi()
                delete = api.userenv.DeleteAppContainerProfile
                delete.argtypes, delete.restype = [W.LPCWSTR], C.c_long
                status = delete(self.policy.profile_name)
                if status < 0 and (status & 0xffffffff) not in {0x80070002,0x80070003}:
                    raise OSError(f'AppContainer profile retirement failed: {status & 0xffffffff:#x}')
                import hashlib, json
                raw = json.dumps(self.payload, sort_keys=True).encode('utf8')
                self.payload = {key: value for key, value in self.payload.items()
                    if key not in {'records', 'planned', 'boundaries'}}
                self.payload.update(phase='revoked', preparation_sha256=hashlib.sha256(raw).hexdigest())
                save_record(self.record, self.payload)
            except BaseException:
                self.payload['phase'] = 'revoke_failed'
                try: save_record(self.record, self.payload)
                except OSError: pass
                raise


class WorktreeAccessService:
    """Installed by the daemon; targets come from the managed worktree lease."""
    def __init__(self, worktree_manager):
        self.manager = worktree_manager
        self.active = {}

    def prepare(self, lease):
        if os.name != 'nt': return
        expected = self.manager._target_path(self.manager._safe_id(lease.process_id)).resolve()
        if Path(lease.path).resolve(strict=True) != expected or not lease.isolated:
            raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Expected a concrete managed child worktree')
        journal_dir = self.manager.storage.paths.project_root / 'sandbox_access'
        journal_dir.mkdir(parents=True, exist_ok=True)
        with _lock:
            if lease.process_id in self.active:
                raise SandboxDenied('SANDBOX_CLEANUP_PENDING', 'This managed worktree already owns an access preparation')
            runtimes, shallow = native_runtime_access()
            access = WorkspaceAccess(expected, runtimes, journal_dir / (lease.process_id + '.json'), shallow_runtimes=shallow)
            self.active[lease.process_id] = access
            access.prepare()

    def release(self, process_id):
        access = self.active.get(process_id)
        if access is not None:
            access.close()
            self.active.pop(process_id, None)


def revoke_record(record):
    """Explicit recovery for an inactive configuration; never resume a task."""
    import json
    from .sandbox_access_records import MAX_RECORD_BYTES
    path = Path(record).resolve(strict=True)
    with path.open('rb') as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError('Access journal exceeds size bound')
    payload = json.loads(raw)
    if (payload.get('version') != 1 or payload.get('kind') != 'scoped_workspace_access'
            or payload.get('phase') not in {'prepared','applying','applied','prepare_failed','revoking','revoke_failed','revoked'}):
        raise ValueError('Unsupported scoped access journal')
    access = WorkspaceAccess(payload['workspace'], payload['runtimes'], path, shallow_runtimes=payload.get('shallow_runtimes', []), metadata_roots=payload.get('metadata_roots', []))
    from .sandbox_windows import WindowsApi
    api = WindowsApi()
    sid = api.preview_profile_sid(access.policy.profile_name)
    try: principal = sid_string(api, sid)
    finally: api.free_sid(sid)
    if payload.get('profile') != access.policy.profile_name or payload.get('principal') != principal:
        raise ValueError('Access journal profile identity differs from its workspace')
    access.payload = payload
    access.close()


def native_runtime_access():
    """Executable/library access, not an automatic grant to a whole checkout."""
    import sys
    roots = list(trusted_runtime_roots())
    if getattr(sys, 'frozen', False):
        return _minimal_roots(roots), []
    code = Path(__file__).resolve().parents[2]
    # Source checkouts can contain private developer evidence, Git metadata,
    # user config and project state. Expose only backend code and Python roots;
    # the code root gets a non-inheritable entry for cwd/import traversal.
    runtimes = _minimal_roots([code / 'backend', Path(sys.prefix), Path(sys.base_prefix)])
    if any(code.is_relative_to(p) for p in runtimes):
        raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Source backend and Python installation must be separate for scoped runtime access')
    return runtimes, [code]
