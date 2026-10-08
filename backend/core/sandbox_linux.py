"""Linux cgroup ownership and bootstrap, outside all model-controlled inputs."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import time
import threading
import uuid

from backend.core.sandbox import SandboxDenied

class LinuxCgroup:
    def __init__(self, policy):
        self.path = None
        try:
            configured = os.environ.get("GITGO_SANDBOX_CGROUP_ROOT", "")
            if not configured:
                raise OSError("GITGO_SANDBOX_CGROUP_ROOT must name a delegated cgroup v2 subtree")
            root = Path(configured).resolve(strict=True)
            if not root.is_relative_to(Path('/sys/fs/cgroup')) or root == Path('/sys/fs/cgroup'):
                raise OSError("A dedicated delegated subtree under /sys/fs/cgroup is required")
            controllers = set((root / 'cgroup.subtree_control').read_text().split())
            if not {'cpu', 'memory', 'pids'} <= controllers:
                raise OSError("Delegate and enable cpu, memory and pids controllers")
            self.path = root / ('gitgo-' + uuid.uuid4().hex)
            self.path.mkdir(mode=0o700)
            if not (self.path / 'cgroup.kill').is_file():
                raise OSError('Kernel cgroup.kill support is required')
            for name, value in {'memory.max': policy.memory_bytes, 'memory.swap.max': 0,
                                'memory.oom.group': 1, 'pids.max': policy.process_limit,
                                'cpu.max': '100000 100000'}.items():
                (self.path / name).write_text(str(value))
        except (OSError, ValueError) as exc:
            self.close()
            raise SandboxDenied('SANDBOX_UNAVAILABLE', f'Linux cgroup configuration is unavailable: {exc}') from exc

    def kill(self):
        path = self.path
        if path is not None:
            try:
                (path / 'cgroup.kill').write_text('1')
            except FileNotFoundError:
                pass

    def close(self):
        if self.path is not None:
            try:
                self.kill()
                deadline = time.monotonic() + 3
                while 'populated 1' in (self.path / 'cgroup.events').read_text():
                    if time.monotonic() >= deadline:
                        return
                    time.sleep(0.01)
                self.path.rmdir()
                self.path = None
            except OSError:
                # A dead Host can leave an empty cgroup directory. Never move
                # processes into the caller's cgroup or relax its limits.
                pass

class LinuxSandboxProcess(subprocess.Popen):
    def __init__(self, argv, *, cgroup, cpu_seconds, **kwargs):
        self._gitgo_cgroup = cgroup
        self.sandbox_failure = ""
        try:
            super().__init__(argv, **kwargs)
        except BaseException:
            cgroup.close()
            raise
        cpu_path = cgroup.path / "cpu.stat"
        def guard_cpu():
            try:
                while self.poll() is None:
                    values = dict(line.split() for line in cpu_path.read_text().splitlines())
                    if int(values.get('usage_usec', 0)) >= cpu_seconds * 1_000_000:
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
        self._gitgo_cgroup.close()
