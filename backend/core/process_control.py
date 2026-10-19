"""Cross-platform process-tree lifetime control."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import json
import socket
from pathlib import Path


def creation_flags() -> int:
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0,
    )


def attach_kill_job(proc: subprocess.Popen):
    """Attach a Windows child to a kill-on-close Job Object."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(
            job, wintypes.HANDLE(int(proc._handle)),
        ):
            kernel32.CloseHandle(job)
            return None
        return job
    except (AttributeError, OSError, ValueError):
        return None


def close_job(job_handle) -> None:
    if sys.platform != "win32" or not job_handle:
        return
    owned_close = getattr(job_handle, "close", None)
    if owned_close is not None:
        owned_close()
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(wintypes.HANDLE(job_handle))
    except (OSError, ValueError):
        pass


def spawn_managed(argv, **kwargs):
    """Establish owned lifetime BEFORE the command runs, not at cancellation."""
    if sys.platform == "win32":
        return subprocess.Popen(argv, **kwargs)
    parent, child = socket.socketpair()
    # Frozen first launch may include platform loader/signature checks. Keep a
    # separate, bounded startup limit rather than misclassifying a cold Host.
    parent.settimeout(20 if getattr(sys, "frozen", False) else 5)
    if getattr(sys, "frozen", False):
        launch = [sys.executable, "--gitgo-internal-role", "unix-scope", str(child.fileno())]
    else:
        launch = [sys.executable, str(Path(__file__).with_name("unix_process_scope.py")), str(child.fileno())]
    proc = None
    try:
        kwargs["start_new_session"] = True
        kwargs["pass_fds"] = (child.fileno(),)
        proc = subprocess.Popen(launch, **kwargs)
        child.close()
        proc._gitgo_scope_socket = parent
        proc._gitgo_cleanup = {"complete": False, "issues": ["cleanup_not_observed"]}
        parent.sendall(json.dumps(argv).encode() + b"\n")
        data = bytearray()
        while b"\n" not in data and len(data) < 4096:
            chunk = parent.recv(4096 - len(data))
            if not chunk: break
            data.extend(chunk)
        line, _, remainder = data.partition(b"\n")
        ready = json.loads(line)
        proc._gitgo_scope_buffer = remainder
        if "spawn_error" in ready:
            raise OSError(ready["spawn_error"], "Managed command spawn failed")
        if not isinstance(ready.get("spawned"), int):
            raise OSError("Managed command readiness missing")
        proc.args = argv
        return proc
    except BaseException:
        parent.close(); child.close()
        if proc is not None:
            try: proc.wait(timeout=3)  # EOF asks the owning supervisor to clean up
            except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)
        raise


def finish_scope(proc):
    control = getattr(proc, "_gitgo_scope_socket", None)
    if control is None:
        return
    try:
        control.settimeout(1)
        data = bytearray(getattr(proc, "_gitgo_scope_buffer", b""))
        while not data.endswith(b"\n") and len(data) < 4096:
            chunk = control.recv(4096 - len(data))
            if not chunk: break
            data.extend(chunk)
        report = json.loads(data)
        proc._gitgo_cleanup = report["cleanup"]
        if report.get("command_exit") is not None:
            proc.returncode = int(report["command_exit"])
    except (OSError, ValueError, KeyError, TypeError):
        proc._gitgo_cleanup = {"complete": False, "issues": ["supervisor_report_missing"]}
    finally:
        control.close()
        proc._gitgo_scope_socket = None


def terminate_tree(proc: subprocess.Popen, job_handle=None) -> None:
    """Terminate descendants and wait until the root process is reaped."""
    cgroup = getattr(proc, "_gitgo_cgroup", None)
    if cgroup is not None:
        cgroup.kill()
    if proc.poll() is not None:
        close_job(job_handle)
        if cgroup is not None:
            cgroup.close()
        finish_scope(proc)
        return
    try:
        if sys.platform == "win32":
            if job_handle:
                close_job(job_handle)
                job_handle = None
            else:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        else:
            if getattr(proc, "_gitgo_scope_socket", None) is not None:
                # Direct Popen child: no sampled PID/PGID may widen authority.
                proc.terminate()
            else:
                if cgroup is None:
                    proc._gitgo_cleanup = {"complete": False, "issues": ["unmanaged_root_only"]}
                proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    finally:
        close_job(job_handle)
        if cgroup is not None:
            cgroup.close()
        finish_scope(proc)
