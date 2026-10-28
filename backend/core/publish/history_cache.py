"""Small process-local cache of verified immutable Git query snapshots.

Callers MUST derive their currently authorized scope before looking up a key.
This stores no credentials, grants or mutable ref observations.
"""
from collections import OrderedDict
import json
import threading
import time

from .state import encoded

MAX_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 64
TTL_SECONDS = 30
_entries = OrderedDict()
_lock = threading.Lock()
_bytes = 0


def get(key):
    global _bytes
    with _lock:
        entry = _entries.get(key)
        if entry is None:
            return None
        created, raw = entry
        if time.monotonic() - created > TTL_SECONDS:
            del _entries[key]
            _bytes -= len(raw)
            return None
        _entries.move_to_end(key)
    # Each caller gets its own object; response mutation cannot poison a hit.
    return {**json.loads(raw), "cache": "verified_snapshot"}


def put(key, value):
    global _bytes
    raw = encoded(value)
    if len(raw) > MAX_BYTES:
        return
    with _lock:
        old = _entries.pop(key, None)
        if old:
            _bytes -= len(old[1])
        while _entries and (_bytes + len(raw) > MAX_BYTES or len(_entries) >= MAX_ENTRIES):
            _, discarded = _entries.popitem(last=False)
            _bytes -= len(discarded[1])
        _entries[key] = (time.monotonic(), raw)
        _bytes += len(raw)
