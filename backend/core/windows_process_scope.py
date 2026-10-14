"""Trusted Windows trampoline: no payload runs before parent job assignment.

Control uses two explicitly inherited anonymous pipe handles, never stdout or
the model's stdin. No process census, taskkill, breakaway or PID-based killing.
The parent retains the only Job handle; closing it terminates owned members.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main(argv=None):
    import msvcrt
    values = sys.argv[1:] if argv is None else argv
    if values and values[0] == "--lease": return lease_guardian(values[1:])
    if not 2 <= len(values) <= 18 or any(not value.isdigit() or len(value) > 20 for value in values):
        return 2
    incoming = msvcrt.open_osfhandle(int(values[0]), os.O_RDONLY | os.O_BINARY)
    outgoing = msvcrt.open_osfhandle(int(values[1]), os.O_WRONLY | os.O_BINARY)
    os.set_inheritable(incoming, False); os.set_inheritable(outgoing, False)
    leases = [msvcrt.open_osfhandle(int(value), os.O_RDONLY | os.O_BINARY) for value in values[2:]]
    handles = [msvcrt.get_osfhandle(fd) for fd in leases]
    try:
        with os.fdopen(incoming, "rb") as control:
            raw = control.readline(1024 * 1024 + 1)
        if not raw.endswith(b"\n") or len(raw) > 1024 * 1024: return 2
        command = json.loads(raw)
        if (type(command) is not list or not 1 <= len(command) <= 1024
                or any(type(value) is not str or "\0" in value for value in command)):
            return 2
        # This point is reachable only after the parent assigned this trusted
        # process to its own no-breakaway Job and then sent the private command.
        startup = subprocess.STARTUPINFO(); startup.lpAttributeList = {"handle_list": handles}
        proc = subprocess.Popen(command, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
            close_fds=True, startupinfo=startup)
        code = proc.wait()
        with os.fdopen(outgoing, "wb") as report:
            report.write(json.dumps({"command_exit": code}).encode("ascii") + b"\n")
            report.flush()
        outgoing = -1
        return 0
    finally:
        if outgoing >= 0: os.close(outgoing)
        for fd in leases: os.close(fd)


def lease_guardian(values):
    import ctypes as C
    import msvcrt
    import time
    if not 4 <= len(values) <= 19 or any(not value.isdigit() or len(value) > 20 for value in values): return 2
    # Import the fixed Job API, not the application, providers or repositories.
    from backend.core.windows_job import _api, _check, Accounting
    api = _api(); job, parent, ack = (int(value) for value in values[:3])
    leases = [msvcrt.open_osfhandle(int(value), os.O_RDONLY | os.O_BINARY) for value in values[3:]]
    fd = msvcrt.open_osfhandle(ack, os.O_WRONLY | os.O_BINARY)
    ready = False
    try: os.write(fd, b"1"); ready = True
    except OSError: pass
    finally: os.close(fd)
    verified_zero = False
    try:
        stopping = False
        while True:
            try:
                if (not ready or api.WaitForSingleObject(parent, 0) == 0) and not stopping:
                    _check(api.TerminateJobObject(job, 137)); stopping = True
                report = Accounting()
                _check(api.QueryInformationJobObject(job, 1, C.byref(report), C.sizeof(report), None))
                if report.active == 0:
                    verified_zero = True
                    return 0 if ready else 2
                time.sleep(.02)
            except OSError:
                # The parent has its own bounded timeout and reports failure.
                # Unknown membership is NOT permission to release a lease.
                # Hold it fail-closed, terminate only this Job, retry proof.
                api.TerminateJobObject(job, 137)
                time.sleep(.25)
    finally:
        if verified_zero:
            api.CloseHandle(job); api.CloseHandle(parent)
            for fd in leases: os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
