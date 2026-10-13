"""Owned Unix session supervisor. Stdlib-only, also a frozen Host role.

Never upgrade sampled PIDs/PGIDs into group termination authority. The session
leader stays alive until cleanup and can signal only its own pinned group.
Linux subreaper + pidfds add bounded cleanup of adopted detached descendants.
macOS explicitly reports owned-session-only coverage, not full tree isolation.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time


def enable_subreaper():
    if not sys.platform.startswith("linux") or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError):
        return False


def adopted_children():
    # Kernel-owned parent relationship, not a ps ancestry/PGID snapshot.
    path = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
    return [int(pid) for pid in path.read_text().split()]


def signal_adopted(pid):
    fd = os.pidfd_open(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if int(stat[1]) != os.getpid():
            return False
        # Even if the numeric PID exits/recycles now, the fd still addresses
        # only the process opened above, never a later process or entire group.
        signal.pidfd_send_signal(fd, signal.SIGKILL)
        return True
    finally:
        os.close(fd)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    fd = int(args[0])
    retained = tuple(int(item) for item in args[1:])
    if len(retained) > 16 or len(set(retained)) != len(retained) or any(item < 3 or item == fd for item in retained):
        raise RuntimeError("Invalid retained descriptors")
    for item in retained:
        os.fstat(item)
    control = socket.socket(fileno=fd)
    if os.getpgrp() != os.getpid() or os.getsid(0) != os.getpid():
        raise RuntimeError("Supervisor must own a new session")
    group = os.getpgrp()
    subreaper = enable_subreaper()
    cancelled = False
    def request_cancel(*_):
        nonlocal cancelled
        cancelled = True
    signal.signal(signal.SIGTERM, request_cancel)
    signal.signal(signal.SIGINT, request_cancel)
    signal.signal(signal.SIGHUP, request_cancel)
    control.settimeout(5)
    data = bytearray()
    while not data.endswith(b"\n"):
        part = control.recv(65536)
        if not part or len(data) + len(part) > 1024 * 1024:
            raise RuntimeError("Invalid bounded scope request")
        data.extend(part)
    command = json.loads(data)
    worker = None
    exit_code = 127
    try:
        worker = subprocess.Popen(command, close_fds=True, pass_fds=retained)
        ready = {"spawned": worker.pid}
    except (OSError, ValueError) as exc:
        ready = {"spawn_error": getattr(exc, "errno", 22) or 22}
    try:
        control.sendall(json.dumps(ready).encode() + b"\n")
    except OSError:
        cancelled = True
    # Keep no extra references to the payload pipes; detached descendants must
    # not be hidden by a supervisor keeping stdout/stderr open itself.
    for stream_fd in (0, 1, 2):
        try: os.close(stream_fd)
        except OSError: pass
    control.setblocking(False)
    while worker is not None and worker.poll() is None and not cancelled:
        if select.select([control], [], [], .02)[0]:
            try:
                if not control.recv(1024): cancelled = True  # owning runner died
            except BlockingIOError: pass
    if worker is not None and worker.returncode is not None:
        exit_code = worker.returncode
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    issues = []
    # This PGID is held by this live session leader, never derived from ps.
    try: os.killpg(group, signal.SIGTERM)
    except OSError: issues.append("owned_group_signal_failed")
    complete = False
    if subreaper:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                for pid in adopted_children():
                    try: signal_adopted(pid)
                    except ProcessLookupError: pass
                    except OSError: issues.append("adopted_child_signal_failed")
                while True:
                    try:
                        if os.waitpid(-1, os.WNOHANG)[0] == 0: break
                    except ChildProcessError: break
                if not adopted_children():
                    complete = not issues
                    break
            except (OSError, ValueError):
                issues.append("adoption_inspection_failed")
                break
            time.sleep(.02)
        if not complete: issues.append("cleanup_not_confirmed")
    else:
        issues.append("detached_descendants_not_covered")
        time.sleep(.1)
    report = {"command_exit": None if cancelled else exit_code,
              "cleanup": {"mode": "linux-subreaper-pidfd" if subreaper else "owned-session-only",
                          "complete": complete, "issues": sorted(set(issues))}}
    try:
        control.settimeout(1)
        control.sendall(json.dumps(report).encode() + b"\n")
    except OSError: pass
    # Killing our OWN pinned group is safe even after the payload parent exited.
    os.killpg(group, signal.SIGKILL)
    return 1


if __name__ == "__main__":
    main()
