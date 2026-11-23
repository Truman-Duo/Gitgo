"""Opt-in loopback attachment to one existing NativeHost, never a second Host."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import threading
import time
import uuid

from backend.core.application import OperationError
from backend.core.storage.runtime import ObserverSqliteError
from .files import atomic_json, private_directory, read_private_json
from .recording import Archive, Recorder, json_bytes
from .trial import Trial

READ_OPERATIONS = frozenset({"host.capabilities", "runtime.status", "runtime.summaries", "project.overview",
    "runtime.trace", "runtime.usage", "provider.status", "provider.model.options",
    "observer.status", "observer.events", "observer.body"})
WRITE_OPERATIONS = frozenset({"runtime.chat", "runtime.decision", "runtime.feedback", "runtime.stop",
    "runtime.compact", "runtime.btw", "runtime.btw.cancel", "runtime.btw.note", "runtime.btw.adopt",
    "runtime.recovery.resume", "runtime.recovery.discard", "host.cancel", "provider.switch", "provider.model.select"})


class ObserverBridge:
    def __init__(self, host, directory: Path, *, plan_path: Path | None = None):
        self.host = host
        self.directory = private_directory(directory)
        if any(directory.iterdir()):
            raise ValueError("OBSERVER_RUN_ALREADY_EXISTS: choose a new run directory; previous evidence is retained")
        self.instance = uuid.uuid4().hex
        self.read_token, self.control_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.recorder = Recorder(directory)
        self.archive = Archive(directory)
        try:
            self.trial = Trial(plan_path, directory, host) if plan_path else None
        except BaseException:
            self.recorder.close()
            raise
        self._slots = threading.BoundedSemaphore(4)
        self._poll_slots = threading.BoundedSemaphore(2)
        self._frontend_batches = 0
        self._frontend_last_ns = 0
        self._closed = False
        bridge = self

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            block_on_close = False
            allow_reuse_address = False

            def verify_request(self, request, address):
                return address[0] == "127.0.0.1"

            def process_request(self, request, address):
                if not bridge._slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, address)
                except BaseException:
                    bridge._slots.release()
                    raise

            def process_request_thread(self, request, address):
                try:
                    super().process_request_thread(request, address)
                finally:
                    bridge._slots.release()

            def handle_error(self, request, address):
                # Never dump request bodies/capabilities into terminal stderr.
                bridge.recorder.submit("bridge_error", {"code": "OBSERVER_CONNECTION_FAILED"})

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(10)
                try:
                    line = self.rfile.readline((1 << 20) + 1)
                    if len(line) > 1 << 20 or not line.endswith(b"\n"):
                        raise OperationError("OBSERVER_REQUEST_TOO_LARGE", "Send one bounded JSON request.")
                    request = json.loads(line)
                    response = bridge.execute(request)
                except OperationError as exc:
                    response = {"ok": False, "error": exc.to_dict()}
                except ObserverSqliteError as exc:
                    response = {"ok": False, "error": {"code": "OBSERVER_ARCHIVE_READ_FAILED", "message": str(exc)}}
                except (ValueError, TypeError, OSError) as exc:
                    message = str(exc)
                    candidate = message.split(":", 1)[0]
                    code = candidate if candidate.startswith("OBSERVER_") and candidate.replace("_", "").isalnum() else "OBSERVER_REQUEST_INVALID"
                    response = {"ok": False, "error": {"code": code, "message": message}}
                self.wfile.write(json_bytes(response) + b"\n")

        self.server = Server(("127.0.0.1", 0), Handler)
        common = {"version": 1, "instance_id": self.instance, "pid": os.getpid(),
                  "host": "127.0.0.1", "port": self.server.server_address[1]}
        atomic_json(directory / "attach.json", {**common, "token": self.read_token, "access": "read"})
        atomic_json(directory / "control.json", {**common, "token": self.control_token, "access": "control"})
        atomic_json(directory / "run.json", {**common, "trial": self.trial.status() if self.trial else None,
                    "privacy": "LOCAL PRIVATE - DO NOT UPLOAD", "started_unix_ns": time.time_ns()})
        self._previous_capture = os.environ.get("GITGO_OBSERVER_CAPTURE_DIR")
        os.environ["GITGO_OBSERVER_CAPTURE_DIR"] = str(directory)
        self._thread = threading.Thread(target=self.server.serve_forever, name="observer-bridge", daemon=True)
        self._thread.start()
        self.recorder.submit("observer_attached", {"instance_id": self.instance, "host_pid": os.getpid(),
            "coverage_start": "attachment; deferred Dashboard attachment is after terminal handoff and before first frame"})

    def status(self):
        archive = self.archive.status()
        with self.host._daemon_lock:
            pids = {getattr(getattr(c, "_process", None), "pid", None) for c in self.host._daemons.values()}
        missing = sorted(p for p in pids if p and p not in {s.get("pid") for s in archive["sources"]})
        if missing:
            archive = {**archive, "coverage": "degraded", "missing_runtime_pids": missing,
                       "error": "OBSERVER_RUNTIME_CAPTURE_MISSING"}
        return {"instance_id": self.instance, "host_pid": os.getpid(), "attached_to_original_host": True,
                "archive": archive, "host_capture": self.recorder.health(),
                "trial": self.trial.status() if self.trial else None,
                "frontend_metrics": "opt-in numeric frame/input batches; no raw keystrokes",
                "frontend_batches": self._frontend_batches, "frontend_last_unix_ns": self._frontend_last_ns,
                "read_operations": sorted(READ_OPERATIONS), "control_operations": sorted(WRITE_OPERATIONS)}

    def execute(self, request):
        if not isinstance(request, dict) or request.get("instance_id") != self.instance:
            raise OperationError("OBSERVER_INSTANCE_MISMATCH", "Reconnect to the current Host descriptor.")
        token = str(request.get("token") or "")
        writable = hmac.compare_digest(token, self.control_token)
        if not writable and not hmac.compare_digest(token, self.read_token):
            raise OperationError("OBSERVER_AUTH_FAILED", "The local attachment capability was rejected.")
        operation = str(request.get("operation") or "")
        if operation not in READ_OPERATIONS:
            if operation not in WRITE_OPERATIONS:
                raise OperationError("OBSERVER_OPERATION_UNAVAILABLE", "This operation is outside the control interface.")
            if not writable or request.get("confirm") is not True:
                raise OperationError("OBSERVER_CONTROL_CONFIRMATION_REQUIRED", "An explicit control capability and confirmation are required; no action was taken.")
        args = request.get("arguments") or {}
        if not isinstance(args, dict):
            raise OperationError("INVALID_ARGUMENTS", "arguments must be an object")
        if self._closed:
            raise OperationError("OBSERVER_HOST_CLOSING", "The original Host is closing.")
        if operation == "observer.status":
            return {"ok": True, "result": self.status()}
        if operation == "observer.events":
            if not self._poll_slots.acquire(blocking=False):
                raise OperationError("OBSERVER_READ_BUSY", "Two event readers are already waiting; retry this read later.")
            try:
                return self._events(args)
            finally:
                self._poll_slots.release()
        if operation == "observer.body":
            return {"ok": True, "result": self.archive.chunk(str(args.get("source_id") or ""),
                    str(args.get("ref") or ""), args.get("offset", 0), args.get("limit", 262144))}
        responses = []
        self.host._handle_request({"protocol_version": 1, "type": "request",
            "request_id": str(request.get("request_id") or uuid.uuid4()), "operation": operation, "arguments": args},
            response_callback=responses.append, origin="control")
        response = responses[0]
        # Bound the wire without silently truncating canonical status/trace bodies.
        if len(json_bytes(response)) > 8 << 20:
            raise OperationError("OBSERVER_RESPONSE_TOO_LARGE", "Use the canonical trace index/block or paginated archive body interface.")
        return response

    def _events(self, args):
        deadline = time.monotonic() + max(0, min(20000, int(args.get("wait_ms", 0)))) / 1000
        while True:
            result = self.archive.events(str(args.get("source_id") or ""), args.get("after_seq", 0), args.get("limit", 200))
            if result["events"] or time.monotonic() >= deadline or self._closed:
                result["health"] = self.archive.status()
                return {"ok": True, "result": result}
            time.sleep(0.05)

    def frontend(self, arguments):
        # Strict numeric payload contract: never accept or store raw field/input text.
        from math import isfinite
        if not isinstance(arguments, dict) or len(arguments) > 8:
            raise OperationError("OBSERVER_FRONTEND_METRICS_INVALID", "Expected a bounded numeric metrics batch.")
        allowed = {"frames", "inputs", "input_to_frame", "event_loop", "dropped", "window_ms", "sequence", "viewport"}
        def valid(value, depth=0):
            if depth > 4:
                return False
            if isinstance(value, dict):
                return len(value) <= 32 and all(isinstance(k, str) and len(k) <= 48 and valid(v, depth+1) for k,v in value.items())
            if isinstance(value, list):
                return len(value) <= 64 and all(valid(v, depth+1) for v in value)
            return type(value) in (int, float) and isfinite(value) and abs(value) <= 1e15
        if set(arguments) - allowed or not valid(arguments):
            raise OperationError("OBSERVER_FRONTEND_METRICS_INVALID", "Only bounded numeric samples are permitted.")
        captured = self.recorder.submit("frontend_metrics", arguments)
        self._frontend_batches += 1
        self._frontend_last_ns = time.time_ns()
        return {"captured": captured}

    def close(self, *, flush=True):
        if not self._closed:
            self._closed = True
            self.server.shutdown()
            self.server.server_close()
            self._thread.join(3)
            # Keep run/evidence metadata; remove only this instance's live capabilities.
            for name in ("attach.json", "control.json"):
                path = self.directory / name
                try:
                    if read_private_json(path).get("instance_id") == self.instance:
                        path.unlink()
                except FileNotFoundError:
                    pass
        if flush:
            try:
                self.recorder.close()
            finally:
                if os.environ.get("GITGO_OBSERVER_CAPTURE_DIR") == str(self.directory):
                    if self._previous_capture is None:
                        os.environ.pop("GITGO_OBSERVER_CAPTURE_DIR", None)
                    else:
                        os.environ["GITGO_OBSERVER_CAPTURE_DIR"] = self._previous_capture


class ObserverClient:
    def __init__(self, descriptor: str | Path, *, control=False):
        path = Path(descriptor).absolute()
        if control:
            path = path.with_name("control.json")
        self.descriptor = read_private_json(path)
        self.control = control
        if (self.descriptor.get("version") != 1 or self.descriptor.get("host") != "127.0.0.1"
                or type(self.descriptor.get("port")) is not int or not 0 < self.descriptor["port"] < 65536
                or not self.descriptor.get("token") or not self.descriptor.get("instance_id")):
            raise ValueError("OBSERVER_DESCRIPTOR_INVALID")

    def call(self, operation, arguments=None, *, request_id="", confirm=False, timeout=30):
        if operation in WRITE_OPERATIONS and (not self.control or not confirm):
            raise OperationError("OBSERVER_CONTROL_CONFIRMATION_REQUIRED", "Explicit control and confirmation are required.")
        data = json_bytes({"instance_id": self.descriptor["instance_id"], "token": self.descriptor["token"],
            "operation": operation, "arguments": arguments or {}, "request_id": request_id or str(uuid.uuid4()), "confirm": confirm})
        if len(data) > 1 << 20:
            raise OperationError("OBSERVER_REQUEST_TOO_LARGE", "The request exceeds the local attachment limit.")
        try:
            with socket.create_connection(("127.0.0.1", self.descriptor["port"]), timeout=min(timeout, 10)) as connection:
                connection.settimeout(timeout)
                connection.sendall(data + b"\n")
                with connection.makefile("rb") as stream:
                    line = stream.readline((8 << 20) + 1)
                    if len(line) > 8 << 20 or not line.endswith(b"\n"):
                        raise ValueError("OBSERVER_RESPONSE_INVALID")
                    return json.loads(line)
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise OperationError("OBSERVER_HOST_UNREACHABLE", "The original Host cannot be reached. Reconnect; mutations are never retried automatically.",
                                 details={"transport": type(exc).__name__}) from exc

