"""Trusted workspace lifecycle access, never a model-selectable permission tool.

The operator's full-descriptor recovery remains separate. This service uses
only rights already present on the concrete runtime/workspace objects, keeps
private intent/outcome journals, and removes only grants it actually owns.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import os
from contextlib import contextmanager, ExitStack
from pathlib import Path
import threading
from time import monotonic

from .sandbox import SandboxPolicy, trusted_runtime_roots, validate_runtime_separation, SandboxDenied
from .sandbox_access_records import (
    SCOPED_ACCESS_EVIDENCE, SCOPED_ACCESS_KIND, SCOPED_ACCESS_VERSION,
    concrete_roots, load_scoped_access_record, pack_access_evidence, record_path, save_record, sid_string,
)

# Keep enough short-lived retention budget for two measured source runtimes.
# Larger or more numerous invocations still reopen at retirement.
_RUNTIME_PIN_LIMIT = 24_576
_runtime_pins_lock = threading.Lock()
_runtime_pin_count = 0


def _minimal_roots(paths, *, allow_files=False):
    result = []
    for path in concrete_roots(paths, allow_files=allow_files):
        if not any(path.is_relative_to(parent) for parent in result):
            result.append(path)
    return result


class WorkspaceAccess:
    def __init__(self, workspace, runtimes, record, *, shallow_runtimes=(), metadata_roots=(), read_roots=(),
                 read_workspace=None, private_temp_roots=(), excluded_read_dirs=(), read_max_depth=None,
                 reuse_runtime_pins=False):
        self.policy = SandboxPolicy(Path(workspace))
        from .sandbox import child_environment_paths
        expected_temp = child_environment_paths(self.policy.workspace)['TEMP']
        self.private_temp_roots = [Path(p).absolute() for p in private_temp_roots]
        if any(p != expected_temp for p in self.private_temp_roots):
            raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Private temp access must name the owned execution temp directory')
        self.runtimes = _minimal_roots(runtimes, allow_files=True)
        validate_runtime_separation(self.policy.workspace, self.runtimes)
        self.shallow_runtimes = concrete_roots(shallow_runtimes)
        validate_runtime_separation(self.policy.workspace, self.shallow_runtimes)
        self.roots = _minimal_roots([self.policy.workspace, *self.runtimes], allow_files=True)
        self.shallow_runtimes = [p for p in self.shallow_runtimes
            if not any(p.is_relative_to(parent) for parent in self.roots)]
        self.roots.extend(self.shallow_runtimes)
        self.read_workspace = Path(read_workspace).absolute() if read_workspace is not None else None
        self.excluded_read_dirs = tuple(sorted(set(excluded_read_dirs)))
        if read_max_depth is not None and (type(read_max_depth) is not int or not 1 <= read_max_depth <= 64):
            raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Read depth must be an integer in 1..64')
        self.read_max_depth = read_max_depth
        if any(not isinstance(name, str) or not name or Path(name).name != name or name in {'.', '..'}
               for name in self.excluded_read_dirs):
            raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Read exclusions must be concrete directory names')
        self.read_roots = sorted(set(Path(p).absolute() for p in read_roots), key=lambda p: (len(p.parts), str(p)))
        for path in self.read_roots:
            path.stat()
            if path == Path(path.anchor) or path.is_relative_to(self.policy.workspace):
                raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Invalid read resource')
        if self.read_roots and self.read_workspace is None:
            raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Read view requires its logical workspace')
        if self.read_workspace is not None and (not self.read_workspace.is_dir() or self.read_workspace == Path(self.read_workspace.anchor)):
            raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Invalid logical read workspace')
        self.roots.extend(self.read_roots)
        self.metadata_roots = concrete_roots(metadata_roots)
        allowed_metadata = {self.policy.workspace.parent}
        if self.read_workspace is not None:
            allowed_metadata.add(self.read_workspace)
            allowed_metadata.update(parent for p in self.read_roots for parent in p.parents if parent != Path(parent.anchor))
        if any(p not in allowed_metadata for p in self.metadata_roots):
            raise SandboxDenied("SANDBOX_POLICY_INVALID", "Only the immediate execution parent may receive metadata access")
        if any(p in self.roots for p in self.metadata_roots):
            raise SandboxDenied("SANDBOX_POLICY_INVALID", "Metadata-only access must not overlap another access role")
        self.roots.extend(self.metadata_roots)
        self.record = record_path(record, [p for p in self.roots if p not in self.metadata_roots])
        self.payload = None
        self._reuse_runtime_pins = reuse_runtime_pins
        self._runtime_pins = None
        self.timings = {}

    def _tree(self, roots):
        from .windows_access import AccessTree
        return AccessTree(roots, shallow=self.shallow_runtimes, metadata=self.metadata_roots,
            readonly=self.read_roots, execute=self.runtimes, private_temp=self.private_temp_roots,
            excluded_read_dirs=self.excluded_read_dirs, read_max_depth=self.read_max_depth)

    def _retain_runtime_pins(self, tree):
        """Move only trusted runtime object pins to this short-lived invocation."""
        global _runtime_pin_count
        if not self._reuse_runtime_pins or not (self.runtimes or self.shallow_runtimes):
            return
        roots = [*self.runtimes, *self.shallow_runtimes]
        exact = {os.path.normcase(str(path)) for path in roots}
        recursive = tuple(os.path.normcase(str(path)) + os.sep for path in self.runtimes)
        handles = [path for path in tree.handles
                   if (key := os.path.normcase(path)) in exact or key.startswith(recursive)]
        parent_paths = {parent for root in roots for parent in root.parents}
        ancestors = [path for path in tree.ancestors if Path(path) in parent_paths]
        count = len(handles) + len(ancestors)
        if not count:
            return
        with _runtime_pins_lock:
            if count + _runtime_pin_count > _RUNTIME_PIN_LIMIT:
                return
            _runtime_pin_count += count
        kept = None
        try:
            kept = self._tree(())
            for path in handles:
                kept.handles[path] = tree.handles.pop(path)
                if path in tree.children:
                    kept.children[path] = tree.children.pop(path)
            for path in ancestors:
                kept.ancestors[path] = tree.ancestors.pop(path)
            self._runtime_pins = kept, count
        except BaseException:
            try:
                if kept is not None:
                    kept.close()
            finally:
                with _runtime_pins_lock:
                    _runtime_pin_count -= count
            raise

    def release_runtime_pins(self):
        """Drop the optional optimization when native cleanup is unconfirmed."""
        if self._runtime_pins is not None:
            with self._retirement_tree(()) as (_tree, _retained):
                pass

    @contextmanager
    def _retirement_tree(self, roots):
        global _runtime_pin_count
        retained, self._runtime_pins = self._runtime_pins, None
        try:
            started = monotonic()
            with (retained[0] if retained else self._tree(roots)) as tree:
                self.timings['retire_inventory_ms'] = round((monotonic() - started) * 1000, 3)
                self.timings['reused_runtime_pins'] = retained[1] if retained else 0
                yield tree, retained is not None
        finally:
            if retained:
                with _runtime_pins_lock:
                    _runtime_pin_count -= retained[1]

    def prepare(self):
        from .windows_acl import management_lock
        from .sandbox_windows import WindowsApi
        from .windows_acl_plan import _entries, _profile_sid
        # Reserve names and handles while no ACL mutation lock is held. The
        # inventory and live descriptors are still checked under that lock
        # immediately before planning and on the normal restoration path.
        started = monotonic()
        with self._tree(self.roots) as tree:
            waiting = monotonic()
            self.timings['prepare_inventory_ms'] = round((waiting - started) * 1000, 3)
            with management_lock(wait_ms=30000):
                acquired = monotonic()
                self.timings['prepare_acl_wait_ms'] = round((acquired - waiting) * 1000, 3)
                api = WindowsApi()
                sid = api.preview_profile_sid(self.policy.profile_name)
                try: principal = sid_string(api, sid)
                finally: api.free_sid(sid)
                original = tree.snapshot()
                principal_bytes = _profile_sid(principal)
                if any(any(principal_bytes in ace for ace in _entries(tree.record_descriptor(r).dacl)) for r in original):
                    raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Workspace sandbox principal already has access; inspect its existing configuration instead of replacing ownership.')
                planned = tree.plan(original, self.policy.workspace, principal)
                boundaries = tree.boundaries()
                payload = dict(version=SCOPED_ACCESS_VERSION, kind=SCOPED_ACCESS_KIND, phase='prepared',
                    workspace=str(self.policy.workspace), runtimes=[str(p) for p in self.runtimes],
                    shallow_runtimes=[str(p) for p in self.shallow_runtimes],
                    metadata_roots=[str(p) for p in self.metadata_roots],
                    read_roots=[str(p) for p in self.read_roots], read_workspace=str(self.read_workspace) if self.read_workspace else None,
                    excluded_read_dirs=list(self.excluded_read_dirs),
                    **({'read_max_depth': self.read_max_depth} if self.read_max_depth is not None else {}),
                    private_temp_roots=[str(p) for p in self.private_temp_roots],
                    profile=self.policy.profile_name, principal=principal,
                    **pack_access_evidence(original, planned, boundaries))
                save_record(self.record, payload, create=True)
                self.payload = payload
                try:
                    sid = api.profile_sid(self.policy.profile_name)
                    api.free_sid(sid)
                    self.payload['phase'] = 'applying'
                    save_record(self.record, self.payload)
                    tree.restore(planned, expected=original, boundaries=boundaries)
                    self.payload['phase'] = 'applied'
                    save_record(self.record, self.payload)
                except BaseException:
                    self.payload['phase'] = 'prepare_failed'
                    # Do not hide the original failure if journal acknowledgement
                    # is also lost. No model/native code has been admitted yet.
                    try: save_record(self.record, self.payload)
                    except OSError: pass
                    raise
                finally:
                    self.timings['prepare_acl_ms'] = round((monotonic() - acquired) * 1000, 3)
            self._retain_runtime_pins(tree)
        return self

    def close(self):
        if self.payload is None or self.payload.get('phase') == 'revoked':
            self.release_runtime_pins()
            return
        from .windows_acl import management_lock, SecurityInventoryChanged
        from .sandbox_windows import WindowsApi
        from .windows_sandbox_native import retire_profile_access
        runtime_roots = [*self.runtimes, *self.shallow_runtimes]
        # Pins in shared runtime files can be collected before the Job and
        # mutation gates. Pin private/workspace objects only after their Job
        # has retired. The complete live tree is revalidated inside the gates.
        while True:
            with self._retirement_tree(runtime_roots) as (tree, retained):
                waiting = monotonic()
                with management_lock(wait_ms=30000), ExitStack() as retiring:
                    acquired = monotonic()
                    self.timings['retire_acl_wait_ms'] = round((acquired - waiting) * 1000, 3)
                    if self.payload is None or self.payload.get('phase') == 'revoked':
                        return
                    retiring.enter_context(retire_profile_access(self.policy.profile_name))
                    self.payload['phase'] = 'revoking'
                    save_record(self.record, self.payload)
                    try:
                        tree.pin_roots(path for path in self.roots if path not in runtime_roots)
                        try:
                            current = tree.snapshot()
                        except SecurityInventoryChanged:
                            if not retained:
                                raise
                            # No descriptor has been written. Reopen the complete
                            # live runtime outside the cross-process mutation gate.
                            self.timings['refreshed_runtime_inventory'] = True
                            continue
                        planned = tree.revoke(current, self.payload['principal'])
                        tree.restore(planned, expected=current, boundaries=tree.boundaries())
                        tree.close()
                        api = WindowsApi()
                        delete = api.userenv.DeleteAppContainerProfile
                        delete.argtypes, delete.restype = [W.LPCWSTR], C.c_long
                        status = delete(self.policy.profile_name)
                        if status < 0 and (status & 0xffffffff) not in {0x80070002,0x80070003}:
                            raise OSError(f'AppContainer profile retirement failed: {status & 0xffffffff:#x}')
                        import hashlib, json
                        raw = json.dumps(self.payload, sort_keys=True).encode('utf8')
                        retired = {key: value for key, value in self.payload.items()
                                   if key not in SCOPED_ACCESS_EVIDENCE}
                        retired.update(phase='revoked', preparation_sha256=hashlib.sha256(raw).hexdigest())
                        save_record(self.record, retired)
                        self.payload = retired
                        return
                    except BaseException:
                        self.payload['phase'] = 'revoke_failed'
                        try: save_record(self.record, self.payload)
                        except OSError: pass
                        raise
                    finally:
                        self.timings['retire_acl_ms'] = round((monotonic() - acquired) * 1000, 3)


class WorktreeAccessService:
    """Installed by the daemon; targets come from the managed worktree lease."""
    def __init__(self, worktree_manager):
        self.manager = worktree_manager
        self.active = {}
        self._active_lock = threading.Lock()

    def prepare(self, lease):
        if os.name != 'nt': return
        expected = self.manager._target_path(self.manager._safe_id(lease.process_id)).resolve()
        if Path(lease.path).resolve(strict=True) != expected or not lease.isolated:
            raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Expected a concrete managed child worktree')
        journal_dir = self.manager.storage.paths.project_root / 'sandbox_access'
        journal_dir.mkdir(parents=True, exist_ok=True)
        # Different worktrees can collect their handles in parallel. Keep
        # prepare/release ordered for this lease, even if preparation fails.
        lifecycle = threading.Lock()
        with lifecycle:
            with self._active_lock:
                if lease.process_id in self.active:
                    raise SandboxDenied('SANDBOX_CLEANUP_PENDING', 'This managed worktree already owns an access preparation')
                runtimes, shallow = native_runtime_access()
                access = WorkspaceAccess(expected, runtimes, journal_dir / (lease.process_id + '.json'), shallow_runtimes=shallow)
                self.active[lease.process_id] = (access, lifecycle)
            access.prepare()

    def release(self, process_id):
        with self._active_lock:
            entry = self.active.get(process_id)
        if entry is None:
            return
        access, lifecycle = entry
        with lifecycle:
            with self._active_lock:
                if self.active.get(process_id) is not entry:
                    return
            access.close()
            with self._active_lock:
                self.active.pop(process_id, None)


def revoke_record(record):
    """Explicit recovery for an inactive configuration; never resume a task."""
    path = Path(record).resolve(strict=True)
    payload = load_scoped_access_record(path)
    access = WorkspaceAccess(payload['workspace'], payload['runtimes'], path,
        shallow_runtimes=payload.get('shallow_runtimes', []), metadata_roots=payload.get('metadata_roots', []),
        read_roots=payload.get('read_roots', []), read_workspace=payload.get('read_workspace'),
        private_temp_roots=payload.get('private_temp_roots', []), excluded_read_dirs=payload.get('excluded_read_dirs', []),
        read_max_depth=payload.get('read_max_depth'))
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


def native_read_runtime_access(handler, target):
    """Narrow only source Python readers; producers retain their existing runtime.

    Frozen distributions retain their packaged boundary. Test/installed runtime
    layouts supplied by the existing resolver are preserved unless they are the
    actual source interpreter prefix. No access is shared between invocations.
    """
    import sys
    runtimes, shallow = native_runtime_access()
    if getattr(sys, 'frozen', False):
        return runtimes, shallow
    from .native_python_runtime import reader_python_roots
    prefixes = {Path(sys.prefix).resolve(strict=True), Path(sys.base_prefix).resolve(strict=True)}
    selected = []
    for root in runtimes:
        if root in prefixes:
            files, parents = reader_python_roots(root, handler, target)
            selected.extend(files)
            shallow = [*shallow, *parents]
        else:
            selected.append(root)
    return _minimal_roots(selected, allow_files=True), concrete_roots(shallow)
