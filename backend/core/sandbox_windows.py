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
        if sys.platform != "win32":
            raise SandboxDenied("SANDBOX_UNAVAILABLE", "Windows AppContainer requires Windows.")
        try:
            super().__init__(args, **kwargs)
        except (OSError, ValueError, AttributeError) as exc:
            from backend.core.process_control import close_job
            close_job(self._gitgo_job_handle)
            self._gitgo_job_handle = None
            raise SandboxDenied("SANDBOX_LAUNCH_DENIED",
                f"AppContainer could not start the runtime. Check profile ACL provisioning and Windows Job support: {exc}") from exc

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
            self._gitgo_job_handle = job
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
