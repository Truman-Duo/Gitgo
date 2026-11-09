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

PTR = C.c_void_p
from backend.core.windows_sandbox_native import PENDING_JOBS as _PENDING_JOBS


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
        self.active_cpu_count = _function(self.kernel, "GetActiveProcessorCount", [W.WORD], W.DWORD)
        self.create_profile = _function(self.userenv, "CreateAppContainerProfile",
            [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, PTR, W.DWORD, C.POINTER(PTR)], C.c_long)
        self.derive_sid = _function(self.userenv, "DeriveAppContainerSidFromAppContainerName",
            [W.LPCWSTR, C.POINTER(PTR)], C.c_long)

    @staticmethod
    def check(ok):
        if not ok:
            raise C.WinError(C.get_last_error())

    def preview_profile_sid(self, name):
        """Derive the deterministic SID without creating a profile or folders."""
        sid = PTR()
        status = self.derive_sid(name, C.byref(sid))
        if status < 0:
            raise OSError(f"AppContainer SID failed (HRESULT 0x{status & 0xffffffff:08x})")
        return sid

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
        if any(getattr(job, 'retirement_started', True) for job in tuple(_PENDING_JOBS.values())):
            raise SandboxDenied("SANDBOX_CLEANUP_PENDING", "An earlier native Job still owns unconfirmed resources; inspect cleanup before another launch.")
        self._sandbox_policy = policy
        self._native_launch_issued = False
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
                while job.check_cpu(policy.cpu_seconds):
                    if self.poll() is not None:
                        # Lifecycle ownership does not depend on a caller
                        # beginning pipe reads or reaching its finally block.
                        job.close()
                        return
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
            denial.effect_state = "ambiguous" if self._native_launch_issued else "not_committed"
            if self._sandbox_job is not None:
                denial.cleanup = dict(self._sandbox_job.cleanup)
            raise denial from exc

    def close_sandbox(self):
        self._gitgo_cleanup = self._sandbox_job.close()
        return dict(self._gitgo_cleanup)

    @property
    def sandbox_failure(self):
        return self._sandbox_job.failure_reason if self._sandbox_job is not None else ""

    def _execute_child(self, *args, **kwargs):
        from backend.core.windows_sandbox_native import execute_child
        return execute_child(self, *args, **kwargs)
