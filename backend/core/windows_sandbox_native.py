"""Popen transport adapter; all AppContainer launch resources belong to Rust."""
from contextlib import ExitStack, contextmanager
import os
import subprocess
import sys
import threading
from time import monotonic

from backend.core.native_sandbox import get_native, NativeBoundaryError
from backend.core.sandbox import SandboxDenied

PENDING_JOBS = {}
JOB_ADMISSION = threading.RLock()
_RETIRING_ACCESS = set()


def _retiring_jobs():
    return tuple(job for job in PENDING_JOBS.values() if job.retirement_started and not job._closed)


def require_retired_jobs(*, deadline=None):
    """Observe an existing retirement without retrying it or holding admission."""
    with JOB_ADMISSION:
        retiring = _retiring_jobs()
    for job in retiring:
        if job._closed:
            continue
        if deadline is not None and job._lock.acquire(timeout=max(0, deadline - monotonic())):
            job._lock.release()
            if job._closed:
                continue
        raise SandboxDenied("SANDBOX_CLEANUP_PENDING",
            "An earlier native Job still owns unconfirmed resources; inspect cleanup before another launch.")


@contextmanager
def retire_profile_access(profile):
    """Fence this profile's Job registration during access retirement."""
    with JOB_ADMISSION:
        if profile in _RETIRING_ACCESS or any(job.profile_name in (None, profile)
                                               for job in PENDING_JOBS.values()):
            raise SandboxDenied('SANDBOX_CLEANUP_PENDING',
                'Native processes are not retired; keep workspace access and recovery journal for inspection.')
        _RETIRING_ACCESS.add(profile)
    try:
        yield
    finally:
        with JOB_ADMISSION:
            _RETIRING_ACCESS.remove(profile)


def reserve_job(library, policy, deadline):
    wait_deadline = monotonic() + 3
    if deadline is not None:
        wait_deadline = min(deadline, wait_deadline)
    while True:
        require_retired_jobs(deadline=wait_deadline)
        with JOB_ADMISSION:
            # A retirement can begin during invocation journal publication.
            # Recheck under the gate that protects Job registration.
            if _retiring_jobs():
                continue
            if policy.profile_name in _RETIRING_ACCESS:
                raise SandboxDenied('SANDBOX_ACL_MANAGEMENT_BUSY',
                    'The requested native profile is retiring access; no process was started.')
            if deadline is not None and monotonic() >= deadline:
                raise SandboxDenied('TOOL_TIMEOUT', 'The native invocation deadline expired before launch.')
            result = library.call(op='job_create', recovery_id=policy.recovery_id,
                                  processes=policy.process_limit, memory=policy.memory_bytes, cpu_seconds=policy.cpu_seconds)
            return NativeJob(result['token'], library, policy.recovery_id, policy.profile_name)


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
    def __init__(self, token, library, recovery_id, profile_name=None):
        self.token, self.library, self.recovery_id = token, library, recovery_id
        self.profile_name = profile_name
        self._lock = threading.Lock()
        self._closed, self._failure_reason = False, ''
        self.retirement_started = False
        self._image_pins = ExitStack()
        self._pin_close_failed = False
        self.cleanup = {'complete': False, 'mode': 'windows-job', 'issues': ['cleanup_not_observed']}
        # Own from allocation, including failures before Popen receives control.
        with JOB_ADMISSION:
            PENDING_JOBS[id(self)] = self

    @property
    def failure_reason(self):
        with self._lock:
            return self._failure_reason

    def _close_locked(self):
        if self._closed:
            return dict(self.cleanup)
        with JOB_ADMISSION:
            self.retirement_started = True
        if self._pin_close_failed:
            return dict(self.cleanup)
        try:
            complete = self.library.call(op='job_retire', token=self.token)['complete'] is True
            self.cleanup.update(complete=complete, issues=[] if complete else ['job_tree_still_active'])
            if complete:
                try:
                    self._image_pins.close()
                except OSError:
                    self._pin_close_failed = True
                    self.cleanup.update(complete=False, issues=['native_image_pin_cleanup_unknown'])
                    return dict(self.cleanup)
                with JOB_ADMISSION:
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
    with JOB_ADMISSION:
        job = next((job for job in PENDING_JOBS.values() if job.recovery_id == recovery_id), None)
        if job is None:
            if _RETIRING_ACCESS:
                raise SandboxDenied('SANDBOX_ACL_MANAGEMENT_BUSY',
                    'Wait for named-profile access retirement before recovering a Job with unknown profile.')
            result = library.call(op='job_recover', recovery_id=recovery_id)
            if result.get('absent') is True:
                return True
            job = NativeJob(result['token'], library, recovery_id)
    return job.close()['complete'] is True


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
        job = reserve_job(library, policy, proc._launch_deadline)
        proc._sandbox_job = job
        proc._gitgo_job_handle = job
        from backend.core.native_sandbox import pin_windows_runtime_images, IMAGE_PINS_ENV
        images = pin_windows_runtime_images(job._image_pins, child_cwd=os.fsdecode(cwd))
        with ExitStack() as inherited_scope:
            leases = inherited_scope.enter_context(windows_sandbox_lease_handles())
            env = {key: value for key, value in (env or {}).items()
                   if key.upper() not in {LEASE_HANDLES_ENV, IMAGE_PINS_ENV}}
            if leases:
                env[LEASE_HANDLES_ENV] = ','.join(str(handle) for handle in leases)
            env[IMAGE_PINS_ENV] = ','.join(str(handle) for handle in images)
            sys.audit('subprocess.Popen', executable, args, cwd, env)
            with _spawn_lock:
                if proc._launch_check is not None:
                    proc._launch_check()
                try:
                    for handle in [*leases, *images]:
                        os.set_handle_inheritable(handle, True)
                    # Once issued, a lost response is ambiguous: the existing
                    # native invocation journal must recover this named Job.
                    proc._native_launch_issued = True
                    try:
                        result = library.call(op='launch', token=job.token, profile=policy.profile_name,
                            executable=None if executable is None else os.fsdecode(executable),
                            command=subprocess.list2cmdline(args), cwd=os.fsdecode(cwd),
                            environment=sorted(env.items(), key=lambda item: item[0].upper()),
                            handles=[int(p2cread), int(c2pwrite), int(errwrite), *leases, *images], flags=creationflags)
                    except NativeBoundaryError as error:
                        if not error.outcome_unknown:
                            proc._native_launch_issued = False
                        raise
                    proc._child_created = True
                    proc._handle = NativeProcessHandle(result['process_handle'], result['process_token'], library)
                    proc.pid = result['pid']
                finally:
                    for handle in [*leases, *images]:
                        os.set_handle_inheritable(handle, False)
    except BaseException:
        if job is not None:
            job.close()
        raise
    finally:
        proc._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)
