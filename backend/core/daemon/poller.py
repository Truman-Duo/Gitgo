"""Trial poller — periodic check for new commits on trial repository."""

from __future__ import annotations

import queue
import threading


class TrialPoller:
    """Periodically push a trial_check event onto the event queue.

    Uses threading.Event.wait() as a cancellable sleep so stop() returns promptly.
    """

    def __init__(
        self,
        event_queue: queue.Queue,
        interval_sec: float = 300.0,
    ):
        self._queue = event_queue
        self._interval = interval_sec
        self._stopped = threading.Event()

    def run(self):
        while not self._stopped.wait(self._interval):
            self._queue.put({"event": "trial_check"})

    def stop(self):
        self._stopped.set()
