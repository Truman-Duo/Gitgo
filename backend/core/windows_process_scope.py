"""Trusted Windows trampoline: no payload runs before parent job assignment.

Control uses two explicitly inherited anonymous pipe handles, never stdout or
the model's stdin. No process census, taskkill, breakaway or PID-based killing.
The parent retains the only Job handle; closing it terminates owned members.
"""
import json
import os
import subprocess
import sys


def main(argv=None):
    import msvcrt
    values = sys.argv[1:] if argv is None else argv
    if len(values) != 2 or any(not value.isdigit() or len(value) > 20 for value in values):
        return 2
    incoming = msvcrt.open_osfhandle(int(values[0]), os.O_RDONLY | os.O_BINARY)
    outgoing = msvcrt.open_osfhandle(int(values[1]), os.O_WRONLY | os.O_BINARY)
    os.set_inheritable(incoming, False); os.set_inheritable(outgoing, False)
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
        proc = subprocess.Popen(command, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, close_fds=True)
        code = proc.wait()
        with os.fdopen(outgoing, "wb") as report:
            report.write(json.dumps({"command_exit": code}).encode("ascii") + b"\n")
            report.flush()
        outgoing = -1
        return 0
    finally:
        if outgoing >= 0: os.close(outgoing)


if __name__ == "__main__":
    raise SystemExit(main())
