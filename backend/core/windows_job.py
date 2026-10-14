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

    def finish(self, *, cancel=False):
        with self.lock:
            if self.finished: return
            self.finished = True
            complete, issues, code = False, [], None
            try:
                if not cancel:
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
                    code = value["command_exit"]
                _check(self.api.TerminateJobObject(self.job, 137))
                end = time.monotonic() + 5
                while time.monotonic() < end:
                    report = Accounting()
                    _check(self.api.QueryInformationJobObject(self.job, 1, C.byref(report), C.sizeof(report), None))
                    if report.active == 0: complete = True; break
                    time.sleep(.01)
                if not complete: issues.append("job_members_remaining")
                if self.guardian is not None:
                    if self.guardian.wait(timeout=8) != 0:
                        complete = False; issues.append("lease_guardian_failed")
            except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
                complete = False
                issues.append("job_cleanup_unverified")
            finally:
                # Even an unverified/failed scope kills its OWN Job only.
                self.api.TerminateJobObject(self.job, 137)
                self.api.CloseHandle(self.job); os.close(self.report)
                self.proc._gitgo_cleanup = {"complete": complete, "mode": "windows-owned-job", "issues": issues}
                if cancel: self.proc.returncode = -15
                elif code is not None: self.proc.returncode = code


def spawn(argv, **options):
    import msvcrt
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
    api = _api()
    job = _check(api.CreateJobObjectW(None, None))
    incoming, send = os.pipe(); report, outgoing = os.pipe()
    from backend.core.installation_lease import runtime_descriptors
    retained = runtime_descriptors()
    lease_handles = [msvcrt.get_osfhandle(fd) for fd in retained]
    proc = None
    try:
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
            try: proc = subprocess.Popen(command, close_fds=True, startupinfo=startup, **options)
            finally:
                for fd in (incoming, outgoing): os.set_inheritable(fd, False)
                for handle in lease_handles: os.set_handle_inheritable(handle, False)
        # The trusted trampoline has no command yet. Failure here cannot race
        # an untrusted payload escaping the Job before assignment.
        _check(api.AssignProcessToJobObject(job, HANDLE(int(proc._handle))))
        os.close(incoming); incoming = -1
        os.close(outgoing); outgoing = -1
        scope = JobScope(api, job, report); scope.proc = proc
        if retained:
            scope.guardian = _lease_guardian(api, job, lease_handles)
        proc._gitgo_windows_scope = scope
        proc._gitgo_cleanup = {"complete": False, "mode": "windows-owned-job", "issues": ["cleanup_not_observed"]}
        proc.args = argv
        with os.fdopen(send, "wb") as control: control.write(raw); control.flush()
        send = -1
        return proc
    except BaseException:
        api.CloseHandle(job)
        if proc is not None:
            if proc.poll() is None: proc.terminate()
            proc.wait(timeout=10)
        os.close(report)
        raise
    finally:
        for fd in (incoming, send, outgoing):
            if fd >= 0:
                try: os.close(fd)
                except OSError: pass


def _lease_guardian(api, job, leases):
    """Outside the payload Job: retain sharing leases until membership is zero.

    If this parent dies, the inherited parent HANDLE is signaled. The guardian
    terminates only the inherited Job and waits for zero members before exit.
    A nested guardian may belong to an outer Job; that Job's outer guardian
    retains the enclosing lease through its complete cleanup.
    """
    parent = HANDLE()
    _check(api.DuplicateHandle(api.GetCurrentProcess(), api.GetCurrentProcess(), api.GetCurrentProcess(),
                               C.byref(parent), 0x100000, False, 0))
    ready, ack = os.pipe()
    import msvcrt
    ack_handle = msvcrt.get_osfhandle(ack)
    handles = [job, parent.value, ack_handle, *leases]
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--gitgo-internal-role", "windows-job-lease", *(str(h) for h in handles)]
    else:
        command = [sys.executable, "-B", str(Path(__file__).with_name("windows_process_scope.py")), "--lease", *(str(h) for h in handles)]
    startup = subprocess.STARTUPINFO(); startup.lpAttributeList = {"handle_list": handles}
    guardian = None
    try:
        with _spawn_lock:
            for handle in handles: os.set_handle_inheritable(handle, True)
            try: guardian = subprocess.Popen(command, startupinfo=startup, close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000000)  # No console Ctrl-C before zero-member proof.
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
        os.close(ready)
        if ack >= 0: os.close(ack)
