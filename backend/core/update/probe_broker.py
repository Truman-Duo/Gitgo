"""Private launcher capability for bounded probes from a forkless updater.

The admitted engine owns signature, transaction and migration policy. The
launcher owns subprocess lifetime. This inherited socket is never an Agent or
Host RPC endpoint, accepts no shell/environment, and is not inherited by probes.
"""
from __future__ import annotations

import array
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time

from .validation import fields, json_object, number, reject, text

PROBES = {"update-data-probe", "managed-capabilities", "update-startup-probe",
          "dashboard-version", "update-data-migrate"}
MAX_REQUEST = 64 * 1024
MAX_REPLY = 1024 * 1024
MAX_FDS = 16
_client = ContextVar("gitgo_update_probe_client", default=None)


def current_client():
    return _client.get()


def _frame(value):
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return struct.pack("!I", len(raw)) + raw


def _exact(stream, size):
    parts = bytearray()
    while len(parts) < size:
        chunk = stream.recv(size - len(parts))
        if not chunk:
            reject("UPDATE_PROBE_CHANNEL", "Launcher probe channel closed")
        parts.extend(chunk)
    return bytes(parts)


class ProbeClient:
    def __init__(self, stream):
        self.stream = stream

    def request(self, probe, program, profile, *, timeout, nonce=None, plan=None):
        from backend.core.installation_lease import runtime_descriptors
        descriptors = runtime_descriptors()
        if not 1 <= len(descriptors) <= MAX_FDS:
            reject("UPDATE_PROBE_LEASE", "Engine probe requires its installation lease")
        request = {"schema_version": 1, "probe": probe, "program": str(program),
                   "profile": str(profile), "timeout": timeout, "nonce": nonce,
                   "plan": None if plan is None else str(plan)}
        raw = _frame(request)
        if len(raw) > MAX_REQUEST + 4:
            reject("UPDATE_PROBE_CHANNEL", "Launcher probe request is too large")
        self.stream.settimeout(timeout + 40)
        sent = self.stream.sendmsg([raw], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                          array.array("i", descriptors))])
        if sent <= 0:
            reject("UPDATE_PROBE_CHANNEL", "Launcher probe request was not sent")
        if sent < len(raw):
            self.stream.sendall(raw[sent:])
        size = struct.unpack("!I", _exact(self.stream, 4))[0]
        if not 1 <= size <= MAX_REPLY:
            reject("UPDATE_PROBE_CHANNEL", "Launcher probe response is too large")
        value = fields(json_object(_exact(self.stream, size), MAX_REPLY),
                       {"schema_version", "output", "stderr", "exit_code", "cleanup", "failure"})
        if (type(value["schema_version"]) is not int or value["schema_version"] != 1
                or type(value["output"]) is not str or type(value["stderr"]) is not str
                or (value["exit_code"] is not None and type(value["exit_code"]) is not int)
                or type(value["cleanup"]) is not dict
                or type(value["cleanup"].get("complete")) is not bool):
            reject("UPDATE_PROBE_CHANNEL", "Launcher probe response is invalid")
        return value


@contextmanager
def engine_client(descriptor):
    if descriptor is None:
        yield
        return
    if type(descriptor) is not int or descriptor < 3:
        reject("UPDATE_PROBE_CHANNEL", "Invalid launcher probe descriptor")
    with socket.socket(fileno=descriptor) as stream:
        if stream.family != socket.AF_UNIX or stream.type != socket.SOCK_STREAM:
            reject("UPDATE_PROBE_CHANNEL", "Launcher probe requires its inherited Unix socket")
        token = _client.set(ProbeClient(stream))
        try:
            yield
        finally:
            _client.reset(token)


class ProbeServer:
    """Serviced by collect_output on the launcher thread, never a new daemon."""
    def __init__(self, store, *, seconds=600):
        self.store = store
        self.parent, self.child = socket.socketpair()
        self.parent.setblocking(False)
        self.buffer = bytearray()
        self.descriptors = []
        self.calls = 0
        self.deadline = time.monotonic() + seconds
        self.cleanup_complete = True

    def __enter__(self):
        return self

    def close(self):
        self.parent.close()
        self.child.close()
        self._close_received()

    def __exit__(self, *_):
        self.close()

    def _close_received(self):
        for fd in self.descriptors:
            os.close(fd)
        self.descriptors.clear()

    def _path(self, raw, *, directory=False):
        if type(raw) is not str or len(raw) > 32767:
            reject("UPDATE_PROBE_CHANNEL", "Invalid launcher probe path")
        path = Path(raw)
        if (not path.is_absolute() or path != path.resolve(strict=True)
                or not path.is_relative_to(self.store.root) or path.is_symlink()
                or (not path.is_dir() if directory else not path.is_file())):
            reject("UPDATE_PROBE_CHANNEL", "Probe path is outside its managed installation")
        return path

    def _validate(self, value):
        fields(value, {"schema_version", "probe", "program", "profile", "timeout", "nonce", "plan"})
        if (type(value["schema_version"]) is not int or value["schema_version"] != 1
                or type(value["probe"]) is not str or value["probe"] not in PROBES):
            reject("UPDATE_PROBE_CHANNEL", "Unsupported launcher probe request")
        number(value["timeout"], minimum=1, maximum=120)
        program = self._path(value["program"])
        expected = "gitgo" if value["probe"] == "dashboard-version" else "gitgo-host"
        if (program.name != expected or (expected == "gitgo-host"
                and program.parent.parts[-2:] != ("internal", "gitgo-host"))):
            reject("UPDATE_PROBE_CHANNEL", "Probe executable has an unsupported role")
        profile = self._path(value["profile"], directory=True)
        info = profile.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or any(profile.iterdir()):
            reject("UPDATE_PROBE_CHANNEL", "Probe requires an empty private profile")
        if value["probe"] in {"dashboard-version", "update-data-migrate"}:
            if value["nonce"] is not None:
                reject("UPDATE_PROBE_CHANNEL", "Unexpected probe nonce")
        else:
            text(value["nonce"], r"[0-9a-f]{32}", 32)
        if value["probe"] == "update-data-migrate":
            plan = self._path(value["plan"])
            if plan.name != "data-migrate-plan.json":
                reject("UPDATE_PROBE_CHANNEL", "Unexpected migration plan")
        elif value["plan"] is not None:
            reject("UPDATE_PROBE_CHANNEL", "Unexpected migration plan")
        from .persistence import descriptor_identity
        identity = self.store.registration["lock_identities"]["runtime.lock"]
        if not 1 <= len(self.descriptors) <= MAX_FDS:
            reject("UPDATE_PROBE_LEASE", "Probe runtime lease is missing")
        for fd in self.descriptors:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or descriptor_identity(fd) != identity):
                reject("UPDATE_PROBE_LEASE", "Probe lease differs from the installation")
            # The inode identifies the installation, not the held reservation.
            # A separately opened fd must not masquerade as the engine's
            # exclusive open-file description if that engine dies mid-probe.
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                reject("UPDATE_PROBE_LEASE", "Probe lacks the engine's exclusive installation lease")
        return program, profile

    def poll(self):
        try:
            data, ancillary, flags, _ = self.parent.recvmsg(
                MAX_REQUEST + 5, socket.CMSG_SPACE(MAX_FDS * array.array("i").itemsize))
        except BlockingIOError:
            return
        for level, kind, raw in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                reject("UPDATE_PROBE_CHANNEL", "Unsupported probe ancillary data")
            values = array.array("i")
            values.frombytes(raw)
            for descriptor in values:
                os.set_inheritable(descriptor, False)
            self.descriptors.extend(values)
        if flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC) or len(self.descriptors) > MAX_FDS:
            reject("UPDATE_PROBE_CHANNEL", "Truncated launcher probe capability")
        if not data:
            if self.buffer or self.descriptors:
                reject("UPDATE_PROBE_CHANNEL", "Incomplete launcher probe request")
            return
        self.buffer.extend(data)
        if len(self.buffer) < 4:
            return
        size = struct.unpack("!I", self.buffer[:4])[0]
        if not 1 <= size <= MAX_REQUEST or len(self.buffer) > size + 4:
            reject("UPDATE_PROBE_CHANNEL", "Invalid launcher probe frame")
        if len(self.buffer) < size + 4:
            return
        self.calls += 1
        if self.calls > 64 or time.monotonic() >= self.deadline:
            reject("UPDATE_PROBE_CHANNEL", "Launcher probe budget exhausted")
        try:
            request = json_object(bytes(self.buffer[4:]), MAX_REQUEST)
            program, profile = self._validate(request)
            from .health import execute_probe, ProbeFailure
            try:
                result = execute_probe(program, profile, request["probe"],
                    timeout=min(request["timeout"], max(1, int(self.deadline - time.monotonic()))),
                    nonce=request["nonce"], plan=request["plan"],
                    inherited_fds=tuple(self.descriptors))
                self.cleanup_complete &= result.cleanup.get("complete") is True
                reply = {"schema_version": 1, "output": result.output, "stderr": result.stderr,
                         "exit_code": result.proc.returncode, "cleanup": result.cleanup, "failure": None}
            except ProbeFailure as error:
                cleanup = error.cleanup or {"complete": False, "issues": ["cleanup_not_observed"]}
                self.cleanup_complete &= cleanup.get("complete") is True
                reply = {"schema_version": 1, "output": "", "stderr": "",
                         "exit_code": error.diagnostic["exit_code"], "cleanup": cleanup,
                         "failure": error.diagnostic}
            raw = _frame(reply)
            if len(raw) > MAX_REPLY + 4:
                reject("UPDATE_PROBE_CHANNEL", "Launcher probe response exceeded its bound")
            self.parent.settimeout(5)
            self.parent.sendall(raw)
        finally:
            self.parent.setblocking(False)
            self.buffer.clear()
            self._close_received()
