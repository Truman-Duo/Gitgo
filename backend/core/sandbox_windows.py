"""Windows AppContainer + atomic Job Object assignment (Windows 10+).

No network capabilities are supplied. ACL provisioning is an explicit operator
step; process launch never changes host ACLs or retries with the user's token.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
import subprocess
import sys
import threading
import time

from backend.core.sandbox import SandboxDenied, SandboxPolicy

SIZE = C.c_size_t
PTR = C.c_void_p


class BasicLimits(C.Structure):
    _fields_ = [("process_time", C.c_longlong), ("job_time", C.c_longlong),
                ("flags", W.DWORD), ("min_ws", SIZE), ("max_ws", SIZE),
                ("processes", W.DWORD), ("affinity", SIZE),
                ("priority", W.DWORD), ("scheduling", W.DWORD)]


class IoCounters(C.Structure):
    _fields_ = [(name, C.c_ulonglong) for name in
                ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimits(C.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                ("process_memory", SIZE), ("job_memory", SIZE),
                ("peak_process_memory", SIZE), ("peak_job_memory", SIZE)]


class BasicAccounting(C.Structure):
    _fields_ = [("user_time", C.c_longlong), ("kernel_time", C.c_longlong),
                ("period_user_time", C.c_longlong), ("period_kernel_time", C.c_longlong),
                ("page_faults", W.DWORD), ("total_processes", W.DWORD),
                ("active_processes", W.DWORD), ("terminated_processes", W.DWORD)]


class NativeJobHandle(int):
    """One owned handle; synchronize accounting and close to prevent reuse races."""
    def __new__(cls, value, api):
        handle = super().__new__(cls, value)
        handle._api = api
        handle._lock = threading.Lock()
        handle._closed = False
        handle._failure_reason = ""
        return handle

    def _close_locked(self):
        if not self._closed:
            self._closed = True
            self._api.close(self)  # Sole, non-inheritable handle: kills the tree.

    def close(self):
        with self._lock:
            self._close_locked()

    @property
    def failure_reason(self):
        with self._lock:
            return self._failure_reason

    def _fail_locked(self, reason):
        self._failure_reason = reason
        self._api.terminate_job(self, 1)
        self._close_locked()  # Kill-on-close also covers a failed termination call.

    def check_cpu(self, seconds):
        with self._lock:
            if self._closed:
                return False
            accounting = BasicAccounting()
            if not self._api.query_job(self, 1, C.byref(accounting), C.sizeof(accounting), None):
                self._fail_locked("Native Job CPU accounting became unavailable.")
                return False
            if accounting.user_time + accounting.kernel_time >= seconds * 10_000_000:
                self._fail_locked("The invocation exceeded its aggregate user+kernel CPU budget.")
                return False
            return True


class SecurityCapabilities(C.Structure):
    _fields_ = [("sid", PTR), ("capabilities", PTR), ("count", W.DWORD), ("reserved", W.DWORD)]


class StartupInfo(C.Structure):
    _fields_ = [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR),
                ("title", W.LPWSTR), ("x", W.DWORD), ("y", W.DWORD),
                ("xsize", W.DWORD), ("ysize", W.DWORD), ("xchars", W.DWORD),
                ("ychars", W.DWORD), ("fill", W.DWORD), ("flags", W.DWORD),
                ("show", W.WORD), ("reserved_size", W.WORD), ("reserved2", PTR),
                ("stdin", W.HANDLE), ("stdout", W.HANDLE), ("stderr", W.HANDLE)]


class StartupInfoEx(C.Structure):
    _fields_ = [("startup", StartupInfo), ("attributes", PTR)]


class ProcessInfo(C.Structure):
    _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE), ("pid", W.DWORD), ("tid", W.DWORD)]


def _function(dll, name, args, result):
    fn = getattr(dll, name)
    fn.argtypes = args
    fn.restype = result
    return fn


class WindowsApi:
    def __init__(self):
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.userenv = C.WinDLL("userenv", use_last_error=True)
        self.advapi = C.WinDLL("advapi32", use_last_error=True)
        self.close = _function(self.kernel, "CloseHandle", [W.HANDLE], W.BOOL)
        self.terminate_job = _function(self.kernel, "TerminateJobObject", [W.HANDLE, W.DWORD], W.BOOL)
        self.query_job = _function(self.kernel, "QueryInformationJobObject",
            [W.HANDLE, C.c_int, PTR, W.DWORD, PTR], W.BOOL)
        self.free_sid = _function(self.advapi, "FreeSid", [PTR], PTR)
        self.create_profile = _function(self.userenv, "CreateAppContainerProfile",
            [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, PTR, W.DWORD, C.POINTER(PTR)], C.c_long)
        self.derive_sid = _function(self.userenv, "DeriveAppContainerSidFromAppContainerName",
            [W.LPCWSTR, C.POINTER(PTR)], C.c_long)
        self.create_job = _function(self.kernel, "CreateJobObjectW", [PTR, W.LPCWSTR], W.HANDLE)
        self.set_job = _function(self.kernel, "SetInformationJobObject",
            [W.HANDLE, C.c_int, PTR, W.DWORD], W.BOOL)
        self.init_attrs = _function(self.kernel, "InitializeProcThreadAttributeList",
            [PTR, W.DWORD, W.DWORD, C.POINTER(SIZE)], W.BOOL)
        self.update_attr = _function(self.kernel, "UpdateProcThreadAttribute",
            [PTR, W.DWORD, SIZE, PTR, SIZE, PTR, PTR], W.BOOL)
        self.delete_attrs = _function(self.kernel, "DeleteProcThreadAttributeList", [PTR], None)
        self.create_process = _function(self.kernel, "CreateProcessW",
            [W.LPCWSTR, W.LPWSTR, PTR, PTR, W.BOOL, W.DWORD, PTR, W.LPCWSTR,
             C.POINTER(StartupInfoEx), C.POINTER(ProcessInfo)], W.BOOL)

    @staticmethod
    def check(ok):
        if not ok:
            raise C.WinError(C.get_last_error())

    def profile_sid(self, name):
        sid = PTR()
        status = self.create_profile(name, name, "Gitgo isolated tools", None, 0, C.byref(sid))
        if status == -2147024713:  # HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)
            status = self.derive_sid(name, C.byref(sid))
        if status < 0:
            raise OSError(f"AppContainer profile failed (HRESULT 0x{status & 0xffffffff:08x})")
        return sid


class WindowsSandboxProcess(subprocess.Popen):
    def __init__(self, args, *, policy: SandboxPolicy, **kwargs):
        self._sandbox_policy = policy
        self._gitgo_job_handle = None
        self._sandbox_job = None
        if sys.platform != "win32":
            raise SandboxDenied("SANDBOX_UNAVAILABLE", "Windows AppContainer requires Windows.")
        try:
            super().__init__(args, **kwargs)
            job = self._gitgo_job_handle
            def guard_cpu():
                # The kernel Job time limit counts user time only. Read native
                # aggregate user+kernel counters, as Linux does with cpu.stat.
                while self.poll() is None and job.check_cpu(policy.cpu_seconds):
                    time.sleep(0.05)
            threading.Thread(target=guard_cpu, daemon=True).start()
        except BaseException as exc:
            from backend.core.process_control import close_job
            close_job(self._gitgo_job_handle)
            self._gitgo_job_handle = None
            if self._child_created:
                try:
                    self.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            for stream in (self.stdin, self.stdout, self.stderr):
                if stream is not None:
                    stream.close()
            if not isinstance(exc, (OSError, ValueError, AttributeError, RuntimeError)):
                raise
            denial = SandboxDenied("SANDBOX_LAUNCH_DENIED",
                f"AppContainer runtime or accounting monitor could not start. Check runtime ACLs, Job support and Host resources: {exc}")
            denial.effect_state = "ambiguous" if self._child_created else "not_committed"
            raise denial from exc

    @property
    def sandbox_failure(self):
        return self._sandbox_job.failure_reason if self._sandbox_job is not None else ""

    def _execute_child(self, args, executable, preexec_fn, close_fds, pass_fds,
                       cwd, env, startupinfo, creationflags, shell,
                       p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite,
                       *unused):
        # Popen owns all pipe endpoints and communication; only replace the
        # native CreateProcess call, without monkeypatching global functions.
        from subprocess import Handle
        if shell or pass_fds or startupinfo is not None or preexec_fn:
            raise ValueError("unsupported sandbox process launch option")
        if -1 in (p2cread, c2pwrite, errwrite):
            raise ValueError("sandbox requires private stdin/stdout/stderr pipes")
        api = WindowsApi()
        sid = None
        attributes = None
        initialized = False
        job = None
        try:
            sid = api.profile_sid(self._sandbox_policy.profile_name)
            job = api.create_job(None, None)  # not inheritable
            api.check(job)
            limits = ExtendedLimits()
            # KILL_ON_JOB_CLOSE, ACTIVE_PROCESS, JOB_MEMORY, JOB_TIME.
            # No BREAKAWAY flags: every descendant remains in this job.
            limits.basic.flags = 0x2000 | 0x8 | 0x200 | 0x4
            limits.basic.processes = self._sandbox_policy.process_limit
            limits.basic.job_time = self._sandbox_policy.cpu_seconds * 10_000_000
            limits.job_memory = self._sandbox_policy.memory_bytes
            api.check(api.set_job(job, 9, C.byref(limits), C.sizeof(limits)))
            length = SIZE()
            api.init_attrs(None, 3, 0, C.byref(length))
            attributes = C.create_string_buffer(length.value)
            api.check(api.init_attrs(attributes, 3, 0, C.byref(length)))
            initialized = True
            caps = SecurityCapabilities(sid, None, 0, 0)
            handles = (W.HANDLE * 3)(int(p2cread), int(c2pwrite), int(errwrite))
            jobs = (W.HANDLE * 1)(job)
            for key, value in ((0x20009, caps), (0x20002, handles), (0x2000D, jobs)):
                api.check(api.update_attr(attributes, 0, key, C.byref(value), C.sizeof(value), None, None))
            startup = StartupInfoEx()
            startup.startup.cb = C.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
            startup.startup.stdin = int(p2cread)
            startup.startup.stdout = int(c2pwrite)
            startup.startup.stderr = int(errwrite)
            startup.attributes = C.cast(attributes, PTR)
            info = ProcessInfo()
            command = C.create_unicode_buffer(subprocess.list2cmdline(args))
            environment = C.create_unicode_buffer(
                "\0".join(f"{key}={value}" for key, value in sorted((env or {}).items(), key=lambda kv: kv[0].upper())) + "\0\0"
            )
            sys.audit("subprocess.Popen", executable, args, cwd, env)
            # JOB_LIST assigns the process before any user code can execute.
            api.check(api.create_process(executable, command, None, None, True,
                creationflags | 0x80000 | 0x400, environment, os.fsdecode(cwd),
                C.byref(startup), C.byref(info)))
            self._child_created = True
            self._handle = Handle(info.process)
            self.pid = info.pid
            self._gitgo_job_handle = NativeJobHandle(job, api)
            self._sandbox_job = self._gitgo_job_handle
            job = None
            api.close(info.thread)
        finally:
            self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)
            if initialized:
                api.delete_attrs(attributes)
            if sid:
                api.free_sid(sid)
            if job:
                api.close(job)
