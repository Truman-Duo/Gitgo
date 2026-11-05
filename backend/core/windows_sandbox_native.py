"""Popen transport adapter; all AppContainer launch resources belong to Rust."""
from contextlib import ExitStack
import os
import subprocess
import sys
import threading

from backend.core.native_sandbox import get_native, NativeBoundaryError

PENDING_JOBS = {}


class NativeProcessHandle(int):
    """Borrow a Rust-owned HANDLE for Popen waits; never CloseHandle in Python."""
    def __new__(cls, value, token, library):
        value = super().__new__(cls, value)
        value.token, value.library, value.closed = token, library, False
        return value

    def Close(self):
        if not self.closed:
            self.library.call(op='process_release', token=self.token)
            self.closed = True

    def __del__(self):
        # A failed close remains owned by Rust and is not cleanup evidence.
        try:
            self.Close()
        except OSError:
            pass


class NativeJob:
    def __init__(self, token, library, recovery_id):
        self.token, self.library, self.recovery_id = token, library, recovery_id
        self._lock = threading.Lock()
        self._closed, self._failure_reason = False, ''
        self.retirement_started = False
        self.cleanup = {'complete': False, 'mode': 'windows-job', 'issues': ['cleanup_not_observed']}
        # Own from allocation, including failures before Popen receives control.
        PENDING_JOBS[id(self)] = self

    @property
    def failure_reason(self):
        with self._lock:
            return self._failure_reason

    def _close_locked(self):
        if self._closed:
            return dict(self.cleanup)
        self.retirement_started = True
        try:
            complete = self.library.call(op='job_retire', token=self.token)['complete'] is True
            self.cleanup.update(complete=complete, issues=[] if complete else ['job_tree_still_active'])
            if complete:
                self._closed = True
                PENDING_JOBS.pop(id(self), None)
        except OSError:
            self.cleanup.update(complete=False, issues=['job_retirement_unknown'])
        return dict(self.cleanup)

    def close(self):
        with self._lock:
            return self._close_locked()

    def check_cpu(self, seconds):
        with self._lock:
            if self._closed:
                return False
            try:
                report = self.library.call(op='job_query', token=self.token)
                if report['cpu_100ns'] < seconds * 10_000_000:
                    return True
                self._failure_reason = 'The invocation exceeded its aggregate user+kernel CPU budget.'
            except OSError:
                self._failure_reason = 'Native Job CPU accounting became unavailable.'
            self._close_locked()
            return False


def recover(recovery_id):
    library = get_native()
    for job in tuple(PENDING_JOBS.values()):
        if job.recovery_id == recovery_id:
            return job.close()['complete'] is True
    result = library.call(op='job_recover', recovery_id=recovery_id)
    return result.get('absent') is True or NativeJob(result['token'], library, recovery_id).close()['complete'] is True


def execute_child(proc, args, executable, preexec_fn, close_fds, pass_fds,
                  cwd, env, startupinfo, creationflags, shell,
                  p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite, *unused):
    if shell or pass_fds or startupinfo is not None or preexec_fn or creationflags & ~(0x200 | 0x08000000):
        raise ValueError('unsupported sandbox process launch option')
    if -1 in (p2cread, c2pwrite, errwrite):
        raise ValueError('sandbox requires private stdin/stdout/stderr pipes')
    from backend.core.installation_lease import windows_sandbox_lease_handles, LEASE_HANDLES_ENV
    from backend.core.windows_job import _spawn_lock
    policy = proc._sandbox_policy
    library = get_native()  # Required: no ctypes launch fallback.
    job = None
    try:
        result = library.call(op='job_create', recovery_id=policy.recovery_id,
                              processes=policy.process_limit, memory=policy.memory_bytes, cpu_seconds=policy.cpu_seconds)
        job = NativeJob(result['token'], library, policy.recovery_id)
        proc._sandbox_job = job
        proc._gitgo_job_handle = job
        with ExitStack() as inherited_scope:
            leases = inherited_scope.enter_context(windows_sandbox_lease_handles())
            env = {key: value for key, value in (env or {}).items() if key.upper() != LEASE_HANDLES_ENV}
            if leases:
                env[LEASE_HANDLES_ENV] = ','.join(str(handle) for handle in leases)
            sys.audit('subprocess.Popen', executable, args, cwd, env)
            with _spawn_lock:
                try:
                    for handle in leases:
                        os.set_handle_inheritable(handle, True)
                    # Once issued, a lost response is ambiguous: the existing
                    # native invocation journal must recover this named Job.
                    proc._native_launch_issued = True
                    try:
                        result = library.call(op='launch', token=job.token, profile=policy.profile_name,
                            executable=None if executable is None else os.fsdecode(executable),
                            command=subprocess.list2cmdline(args), cwd=os.fsdecode(cwd),
                            environment=sorted(env.items(), key=lambda item: item[0].upper()),
                            handles=[int(p2cread), int(c2pwrite), int(errwrite), *leases], flags=creationflags)
                    except NativeBoundaryError as error:
                        if not error.outcome_unknown:
                            proc._native_launch_issued = False
                        raise
                    proc._child_created = True
                    proc._handle = NativeProcessHandle(result['process_handle'], result['process_token'], library)
                    proc.pid = result['pid']
                finally:
                    for handle in leases:
                        os.set_handle_inheritable(handle, False)
    except BaseException:
        if job is not None:
            job.close()
        raise
    finally:
        proc._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)
