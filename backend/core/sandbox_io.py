"""Bounded pipe draining: tool output must not exhaust the Host's memory."""
from __future__ import annotations

import os
import subprocess
import threading
import time

from backend.core.sandbox import SandboxDenied


class BoundedCommunication:
    def __init__(self, proc, limit: int = 2_000_000):
        self.proc = proc
        self.limit = limit
        self.output = [bytearray(), bytearray()]
        self.exceeded = threading.Event()
        self.threads = []
        self.started = False

    def _read(self, stream, index):
        try:
            while chunk := os.read(stream.fileno(), 65536):
                remaining = self.limit - len(self.output[index])
                self.output[index].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.exceeded.set()
                    break
        except (OSError, ValueError):
            pass

    def _write(self, payload):
        try:
            self.proc.stdin.write(payload)
            self.proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def communicate(self, input=None, timeout=None):
        if not self.started:
            self.started = True
            for stream, index in ((self.proc.stdout, 0), (self.proc.stderr, 1)):
                thread = threading.Thread(target=self._read, args=(stream, index), daemon=True)
                thread.start()
                self.threads.append(thread)
            thread = threading.Thread(target=self._write, args=(input or "",), daemon=True)
            thread.start()
            self.threads.append(thread)
        deadline = time.monotonic() + timeout if timeout is not None else float("inf")
        while True:
            if self.exceeded.is_set():
                denial = SandboxDenied("SANDBOX_OUTPUT_LIMIT", "Output exceeded the Host capture budget.")
                # Code ran: never claim its side effects were rolled back.
                denial.effect_state = "ambiguous"
                raise denial
            if self.proc.poll() is not None:
                from backend.core.process_control import close_job
                close_job(getattr(self.proc, "_gitgo_job_handle", None))
                self.proc._gitgo_job_handle = None
                if all(not thread.is_alive() for thread in self.threads):
                    return tuple(bytes(data).decode("utf-8", errors="replace") for data in self.output)
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.proc.args, timeout)
            time.sleep(0.01)
