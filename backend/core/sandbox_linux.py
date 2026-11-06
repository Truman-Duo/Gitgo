"""Linux cgroup ownership and bootstrap, outside all model-controlled inputs."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import stat
import time
import threading
import uuid

from backend.core.sandbox import SandboxDenied


def trusted_system_file(candidate: Path, workspace: Path, *, executable=False) -> Path | None:
    """Resolve only root-owned, non-group/world-writable system files."""
    try:
        path = candidate.resolve(strict=True)
        if path.is_relative_to(workspace.resolve(strict=True)):
            return None
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or (executable and not info.st_mode & 0o111):
            return None
        for component in (path, *path.parents):
            info = component.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                return None
        return path
    except (OSError, RuntimeError):
        return None


def trusted_bwrap(workspace: Path) -> str:
    """Only root-owned system helpers; PATH and workspace cannot select one."""
    for candidate in (Path('/usr/bin/bwrap'), Path('/bin/bwrap')):
        path = trusted_system_file(candidate, workspace, executable=True)
        if path is not None:
            return str(path)
    raise SandboxDenied('SANDBOX_UNAVAILABLE',
        'Install root-owned system bubblewrap at /usr/bin/bwrap or /bin/bwrap. '
        'The helper and its parent directories must not be group/world writable; '
        'PATH-selected helpers and unsandboxed fallback are forbidden.')


_PENDING_CGROUPS = {}


class LinuxCgroup:
    """Python retains the existing recovery gate; Rust owns kernel objects."""
    def __init__(self, policy):
        from backend.core.native_sandbox import get_native, NativeBoundaryError
        if _PENDING_CGROUPS:
            raise SandboxDenied("SANDBOX_CLEANUP_PENDING", "An earlier cgroup has unconfirmed cleanup.")
        self.path, self._token, self._unknown = None, None, False
        self._lock = threading.RLock()
        self.cleanup = {"complete": False, "mode": "linux-cgroup", "issues": ["cleanup_not_observed"]}
        try:
            self._library = get_native()
            if self._library.call(op='capabilities').get('linux_cgroup_version') != 1:
                raise OSError('The matching Rust cgroup boundary is required')
            configured = os.environ.get("GITGO_SANDBOX_CGROUP_ROOT", "")
            if not configured:
                raise OSError("GITGO_SANDBOX_CGROUP_ROOT must name a delegated cgroup v2 subtree")
            root = Path(configured).resolve(strict=True)
            identity = policy.recovery_id or uuid.uuid4().hex
            # The location remains compatible with the existing durable journal.
            # Native code independently validates root, name and filesystem.
            self.path = root / ('gitgo-' + identity)
            response = self._library.call(op='linux_cgroup_create', root=list(os.fsencode(root)),
                recovery_id=identity, memory=policy.memory_bytes, processes=policy.process_limit)
            self._token = response['token']
        except (OSError, ValueError) as exc:
            if isinstance(exc, NativeBoundaryError):
                response = getattr(exc, 'response', {})
                self._token = response.get('token')
                self._unknown = exc.outcome_unknown or response.get('allocation_unknown') is True
            cleanup = self.close()
            denial = SandboxDenied('SANDBOX_UNAVAILABLE',
                f'Linux native cgroup configuration is unavailable; no fallback: {exc}')
            denial.cleanup = cleanup
            raise denial from exc

    @property
    def procs_fd(self):
        with self._lock:
            if self._token is None or self._unknown:
                raise OSError('Native cgroup membership object is unavailable')
            return self._library.call(op='linux_cgroup_procs', token=self._token)['fd']

    def usage_usec(self):
        with self._lock:
            if self._token is None or self._unknown:
                raise OSError('Native cgroup accounting is unavailable')
            return self._library.call(op='linux_cgroup_query', token=self._token)['usage_usec']

    def kill(self):
        with self._lock:
            if self._unknown:
                raise OSError('Cgroup outcome is unknown; retain recovery debt')
            if self._token is not None:
                self._library.call(op='linux_cgroup_kill', token=self._token)

    def close(self):
        from backend.core.native_sandbox import NativeBoundaryError
        with self._lock:
            if self.cleanup['complete']:
                return dict(self.cleanup)
            _PENDING_CGROUPS[id(self)] = self
            if self._unknown:
                self.cleanup.update(complete=False, issues=['cgroup_retirement_unknown'])
                return dict(self.cleanup)
            try:
                complete = (self._token is None or
                    self._library.call(op='linux_cgroup_retire', token=self._token)['complete'] is True)
                if not complete:
                    self.cleanup.update(complete=False, issues=['cgroup_tree_still_active'])
                    return dict(self.cleanup)
            except OSError as error:
                if isinstance(error, NativeBoundaryError) and error.outcome_unknown:
                    self._unknown = True
                self.cleanup.update(complete=False, issues=['cgroup_retirement_unknown'])
                return dict(self.cleanup)
            self.path, self._token = None, None
            _PENDING_CGROUPS.pop(id(self), None)
            self.cleanup.update(complete=True, issues=[])
            return dict(self.cleanup)


def recover_cgroup(root, recovery_id):
    """Existing Publish lease/journal checks must precede this resource operation."""
    from backend.core.native_sandbox import get_native
    root = Path(root)
    path = root / ('gitgo-' + recovery_id)
    for group in tuple(_PENDING_CGROUPS.values()):
        if group.path == path:
            return group.close()['complete'] is True
    library = get_native()
    if library.call(op='capabilities').get('linux_cgroup_version') != 1:
        raise OSError('The matching Rust cgroup recovery boundary is required')
    response = library.call(op='linux_cgroup_recover', root=list(os.fsencode(root)), recovery_id=recovery_id)
    if response['absent'] is True:
        return True
    group = LinuxCgroup.__new__(LinuxCgroup)
    group.path, group._token, group._library = path, response['token'], library
    group._unknown, group._lock = False, threading.RLock()
    group.cleanup = {'complete': False, 'mode': 'linux-cgroup', 'issues': ['cleanup_not_observed']}
    _PENDING_CGROUPS[id(group)] = group
    return group.close()['complete'] is True


class LinuxSandboxProcess(subprocess.Popen):
    def __init__(self, argv, *, cgroup, cpu_seconds, **kwargs):
        self._gitgo_cgroup = cgroup
        self.sandbox_failure = ""
        try:
            super().__init__(argv, **kwargs)
        except BaseException:
            cgroup.close()
            raise
        def guard_cpu():
            try:
                while True:
                    if self.poll() is not None:
                        # Do not leave remaining group members waiting for
                        # the consumer to drain pipes or release the boundary.
                        cgroup.kill()
                        return
                    if cgroup.usage_usec() >= cpu_seconds * 1_000_000:
                        self.sandbox_failure = 'The invocation exceeded its aggregate user+kernel CPU budget.'
                        cgroup.kill()
                        return
                    time.sleep(0.05)
            except OSError:
                if self.poll() is None:
                    self.sandbox_failure = 'Native cgroup CPU accounting became unavailable.'
                    cgroup.kill()  # Loss of resource accounting must fail closed.
        try:
            threading.Thread(target=guard_cpu, daemon=True).start()
        except BaseException as exc:
            try:
                self.kill()
                self.wait(timeout=5)
            finally:
                cgroup.close()
                for stream in (self.stdin, self.stdout, self.stderr):
                    if stream is not None:
                        stream.close()
            if not isinstance(exc, (RuntimeError, OSError)):
                raise
            denial = SandboxDenied('SANDBOX_LAUNCH_DENIED',
                                   f'Native resource accounting monitor could not start: {exc}')
            denial.effect_state = 'ambiguous'
            raise denial from exc

    def kill(self):
        self._gitgo_cgroup.kill()
        super().kill()

    def terminate(self):
        self._gitgo_cgroup.kill()
        super().terminate()

    def close_sandbox(self):
        self._gitgo_cleanup = self._gitgo_cgroup.close()
        return dict(self._gitgo_cleanup)
