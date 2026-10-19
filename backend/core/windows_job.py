"""Parent-owned Windows Job scope with verified zero-member cleanup."""
import ctypes as C
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

U32, HANDLE = C.c_uint32, C.c_void_p
_spawn_lock = threading.Lock()
_pending_lock = threading.Lock()
_pending_scopes = set()  # Own unresolved native resources, not installation state.


def _api():
    if sys.platform != "win32": raise OSError("Windows Job API requires Windows")
    dll = C.WinDLL("kernel32", use_last_error=True)
    specs = {"CreateJobObjectW": (HANDLE, [C.c_void_p, C.c_wchar_p]),
        "CloseHandle": (C.c_int, [HANDLE]),
        "GetCurrentProcess": (HANDLE, []),
        "DuplicateHandle": (C.c_int, [HANDLE, HANDLE, HANDLE, C.c_void_p, U32, C.c_int, U32]),
        "WaitForSingleObject": (U32, [HANDLE, U32]),
        "AssignProcessToJobObject": (C.c_int, [HANDLE, HANDLE]),
        "SetInformationJobObject": (C.c_int, [HANDLE, C.c_int, C.c_void_p, U32]),
        "QueryInformationJobObject": (C.c_int, [HANDLE, C.c_int, C.c_void_p, U32, C.c_void_p]),
        "TerminateJobObject": (C.c_int, [HANDLE, U32])}
    for name, (returns, args) in specs.items():
        call = getattr(dll, name); call.restype, call.argtypes = returns, args
    return dll


def _check(result):
    if not result: raise C.WinError(C.get_last_error())
    return result


class BasicLimits(C.Structure):
    _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", U32),
        ("min_working", C.c_size_t), ("max_working", C.c_size_t), ("active_limit", U32),
        ("affinity", C.c_size_t), ("priority", U32), ("scheduling", U32)]


class ExtendedLimits(C.Structure):
    _fields_ = [("basic", BasicLimits), ("io", C.c_uint64 * 6),
        ("process_memory", C.c_size_t), ("job_memory", C.c_size_t),
        ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]


class Accounting(C.Structure):
    _fields_ = [("times", C.c_int64 * 4), ("faults", U32), ("total", U32), ("active", U32), ("terminated", U32)]


class JobScope:
    def __init__(self, api, job, report):
        self.api, self.job, self.report = api, job, report
        self.lock = threading.Lock()
        self.finished = False
        self.proc = None
        self.guardian = None
        self.command_exit = None
        self.report_issues = []

    def finish(self, *, cancel=False):
        with self.lock:
            if self.finished: return
            complete, issues = False, list(self.report_issues)
            try:
                if not cancel and self.command_exit is None and not self.report_issues:
                    os.set_blocking(self.report, False)
                    raw = bytearray(); end = time.monotonic() + 2
                    while b"\n" not in raw and time.monotonic() < end:
                        try: chunk = os.read(self.report, 4096 - len(raw))
                        except BlockingIOError: time.sleep(.01); continue
                        if not chunk: break
                        raw.extend(chunk)
                        if len(raw) >= 4096: raise ValueError("Job report exceeds bounds")
                    value = json.loads(raw)
                    if set(value) != {"command_exit"} or type(value["command_exit"]) is not int:
                        raise ValueError("Job report is invalid")
                    self.command_exit = value["command_exit"]
            except (OSError, ValueError, TypeError):
                self.report_issues = ["job_report_invalid"]
                issues.extend(self.report_issues)
            try:
                _check(self.api.TerminateJobObject(self.job, 137))
                end = time.monotonic() + 5
                while time.monotonic() < end:
                    report = Accounting()
                    _check(self.api.QueryInformationJobObject(self.job, 1, C.byref(report), C.sizeof(report), None))
                    if report.active == 0: complete = True; break
                    time.sleep(.01)
                if not complete: issues.append("job_members_remaining")
                # Also reap a trusted trampoline whose Job assignment failed.
                # It has not received argv and cannot have launched a payload.
                if self.proc is not None:
                    if self.proc.poll() is None: self.proc.terminate()
                    self.proc.wait(timeout=10)
                if complete and self.guardian is not None:
                    if self.guardian.wait(timeout=8) != 0:
                        issues.append("lease_guardian_failed")
            except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
                complete = False
                issues.append("job_cleanup_unverified")
            if complete:
                try:
                    if self.report >= 0:
                        os.close(self.report); self.report = -1
                    _check(self.api.CloseHandle(self.job))
                    self.finished = True
                except OSError:
                    complete = False; issues.append("job_handle_close_failed")
            # Keep ownership and permit a bounded cleanup retry on unknown
            # termination/membership. Closing a handle is not a zero-member
            # proof when an outside-Job guardian still owns another handle.
            with _pending_lock:
                if self.finished: _pending_scopes.discard(self)
                else: _pending_scopes.add(self)
            if self.proc is not None:
                self.proc._gitgo_cleanup = {"complete": complete and not issues,
                    "mode": "windows-owned-job", "issues": issues}
                if cancel and self.proc.poll() is not None: self.proc.returncode = -15
                elif self.command_exit is not None: self.proc.returncode = self.command_exit


def retry_pending_cleanup():
    """Do not accumulate new commands behind unverified owned process trees."""
    with _pending_lock:
        pending = tuple(_pending_scopes)
    for scope in pending:
        scope.finish(cancel=True)
    with _pending_lock:
        if _pending_scopes:
            raise OSError("Windows owned Job cleanup remains unverified; no command was started")


def spawn(argv, **options):
    import msvcrt
    if (type(argv) not in (list, tuple) or not 1 <= len(argv) <= 1024
            or any(type(value) is not str or "\0" in value for value in argv)):
        raise ValueError("Windows scope requires bounded explicit string argv")
    if options.pop("shell", False) is not False or "executable" in options:
        # A shell/alternate executable around the trusted trampoline could
        # create that trampoline before its parent joins the Job. Callers must
        # request their shell as explicit payload argv, after assignment.
        raise ValueError("Windows scope requires an unchanged trusted trampoline and explicit payload argv")
    raw = json.dumps(argv, ensure_ascii=True).encode("ascii") + b"\n"
    if len(raw) > 1024 * 1024: raise ValueError("Windows scope request exceeds bounds")
    if options.pop("pass_fds", ()) or options.get("creationflags", 0) & 0x01000000:
        raise ValueError("Windows scope may not request breakaway or Unix descriptor inheritance")
    if "startupinfo" in options: raise ValueError("Windows scope owns its handle allowlist")
    if options.pop("close_fds", True) is not True:
        raise ValueError("Windows scope requires an explicit closed handle boundary")
    from backend.core.installation_lease import runtime_descriptors
    retained = runtime_descriptors()
    if len(retained) > 16: raise ValueError("Too many retained Windows lease descriptors")
    lease_handles = [msvcrt.get_osfhandle(fd) for fd in retained]
    retry_pending_cleanup()
    api = _api()
    job = _check(api.CreateJobObjectW(None, None))
    incoming = send = report = outgoing = -1
    proc = None
    scope = JobScope(api, job, -1)
    try:
        incoming, send = os.pipe(); report, outgoing = os.pipe()
        scope.report, report = report, -1
        limits = ExtendedLimits(); limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway.
        _check(api.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)))
        handles = [msvcrt.get_osfhandle(fd) for fd in (incoming, outgoing)]
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--gitgo-internal-role", "windows-scope", *(str(h) for h in handles + lease_handles)]
        else:
            command = [sys.executable, "-B", str(Path(__file__).with_name("windows_process_scope.py")), *(str(h) for h in handles + lease_handles)]
        startup = subprocess.STARTUPINFO(); startup.lpAttributeList = {"handle_list": handles + lease_handles}
        with _spawn_lock:
            for fd in (incoming, outgoing): os.set_inheritable(fd, True)
            for handle in lease_handles: os.set_handle_inheritable(handle, True)
            try:
                proc = subprocess.Popen(command, close_fds=True, startupinfo=startup, **options)
                scope.proc = proc
                proc._gitgo_windows_scope = scope
            finally:
                for fd in (incoming, outgoing): os.set_inheritable(fd, False)
                for handle in lease_handles: os.set_handle_inheritable(handle, False)
        # The trusted trampoline has no command yet. Failure here cannot race
        # an untrusted payload escaping the Job before assignment.
        _check(api.AssignProcessToJobObject(job, HANDLE(int(proc._handle))))
        os.close(incoming); incoming = -1
        os.close(outgoing); outgoing = -1
        if retained:
            _lease_guardian(scope, lease_handles)
        proc._gitgo_cleanup = {"complete": False, "mode": "windows-owned-job", "issues": ["cleanup_not_observed"]}
        proc.args = argv
        control_fd, send = send, -1  # Transfer once, even if write/close raises.
        with os.fdopen(control_fd, "wb") as control: control.write(raw); control.flush()
        return proc
    except BaseException as error:
        scope.finish(cancel=True)
        if not scope.finished:
            if isinstance(error, Exception):
                raise OSError("Windows command launch failed and owned Job cleanup remains unverified; "
                    "resources retained and new launches blocked") from error
            error.add_note("Windows owned Job cleanup is unverified; resources retained and new launches blocked")
        raise
    finally:
        for fd in (incoming, send, outgoing, report):
            if fd >= 0:
                try: os.close(fd)
                except OSError: pass


def _lease_guardian(scope, leases):
    """Outside the payload Job: retain sharing leases until membership is zero.

    If this parent dies, the inherited parent HANDLE is signaled. The guardian
    terminates only the inherited Job and waits for zero members before exit.
    A nested guardian may belong to an outer Job; that Job's outer guardian
    retains the enclosing lease through its complete cleanup.
    """
    api, job = scope.api, scope.job
    parent = HANDLE()
    _check(api.DuplicateHandle(api.GetCurrentProcess(), api.GetCurrentProcess(), api.GetCurrentProcess(),
                               C.byref(parent), 0x100000, False, 0))
    ready = ack = -1
    import msvcrt
    guardian = None
    try:
        ready, ack = os.pipe()
        ack_handle = msvcrt.get_osfhandle(ack)
        handles = [job, parent.value, ack_handle, *leases]
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--gitgo-internal-role", "windows-job-lease", *(str(h) for h in handles)]
        else:
            command = [sys.executable, "-B", str(Path(__file__).with_name("windows_process_scope.py")), "--lease", *(str(h) for h in handles)]
        startup = subprocess.STARTUPINFO(); startup.lpAttributeList = {"handle_list": handles}
        with _spawn_lock:
            for handle in handles: os.set_handle_inheritable(handle, True)
            try:
                guardian = subprocess.Popen(command, startupinfo=startup, close_fds=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=0x08000000)  # No console Ctrl-C before zero-member proof.
                scope.guardian = guardian  # Own it even if readiness fails.
            finally:
                for handle in handles: os.set_handle_inheritable(handle, False)
        os.close(ack); ack = -1
        os.set_blocking(ready, False)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            try: value = os.read(ready, 1)
            except BlockingIOError: time.sleep(.01); continue
            if value == b"1": return guardian
            break
        raise OSError("Windows lease guardian did not become ready")
    finally:
        api.CloseHandle(parent)
        if ready >= 0: os.close(ready)
        if ack >= 0: os.close(ack)
