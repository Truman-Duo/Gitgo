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
            except (OSError, ValueError, TypeError):
                issues.append("job_cleanup_unverified")
            finally:
                # Even an unverified/failed scope kills its OWN Job only.
                self.api.CloseHandle(self.job); os.close(self.report)
                self.proc._gitgo_cleanup = {"complete": complete, "mode": "windows-owned-job", "issues": issues}
                if cancel: self.proc.returncode = -15
                elif code is not None: self.proc.returncode = code


def spawn(argv, **options):
    import msvcrt
    raw = json.dumps(argv, ensure_ascii=True).encode("ascii") + b"\n"
    if len(raw) > 1024 * 1024: raise ValueError("Windows scope request exceeds bounds")
    if options.pop("pass_fds", ()) or options.get("creationflags", 0) & 0x01000000:
        raise ValueError("Windows scope may not request breakaway or Unix descriptor inheritance")
    if "startupinfo" in options: raise ValueError("Windows scope owns its handle allowlist")
    api = _api()
    job = _check(api.CreateJobObjectW(None, None))
    incoming, send = os.pipe(); report, outgoing = os.pipe()
    proc = None
    try:
        limits = ExtendedLimits(); limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway.
        _check(api.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)))
        handles = [msvcrt.get_osfhandle(fd) for fd in (incoming, outgoing)]
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--gitgo-internal-role", "windows-scope", *(str(h) for h in handles)]
        else:
            command = [sys.executable, "-B", str(Path(__file__).with_name("windows_process_scope.py")), *(str(h) for h in handles)]
        startup = subprocess.STARTUPINFO(); startup.lpAttributeList = {"handle_list": handles}
        with _spawn_lock:
            for fd in (incoming, outgoing): os.set_inheritable(fd, True)
            try: proc = subprocess.Popen(command, close_fds=True, startupinfo=startup, **options)
            finally:
                for fd in (incoming, outgoing): os.set_inheritable(fd, False)
        # The trusted trampoline has no command yet. Failure here cannot race
        # an untrusted payload escaping the Job before assignment.
        _check(api.AssignProcessToJobObject(job, HANDLE(int(proc._handle))))
        os.close(incoming); incoming = -1
        os.close(outgoing); outgoing = -1
        scope = JobScope(api, job, report); scope.proc = proc
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
