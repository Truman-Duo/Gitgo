"""Stdin command reader — reads line-delimited JSON from stdin in a thread."""

from __future__ import annotations

import json
import queue
import sys
import threading


class CommandReader:
    """Read line-delimited JSON commands from stdin in a background thread.

    Each non-empty line is parsed as JSON and pushed onto the event queue.
    A "shutdown" command stops the reader and signals the main loop to exit.
    """

    def __init__(self, event_queue: queue.Queue):
        self._queue = event_queue
        self._stopped = threading.Event()

    def run(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                self._queue.put({
                    "event": "error",
                    "message": f"Invalid JSON: {line[:80]}",
                })
                continue
            self._queue.put({"event": "stdin_command", "cmd": cmd})
            if cmd.get("cmd") == "shutdown":
                self._stopped.set()
                return
        # stdin closed (EOF) — treat as shutdown
        self._queue.put({"event": "shutdown"})
        self._stopped.set()

    def stop(self):
        self._stopped.set()
