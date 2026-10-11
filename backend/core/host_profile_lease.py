"""One native Dashboard owner per state namespace, released by the OS on exit."""
from __future__ import annotations

import os
import errno
from pathlib import Path

from backend.core.storage.maintenance import _windows_lock


class HostProfileLease:
    def __init__(self, state_home: Path):
        state_home.mkdir(parents=True, exist_ok=True)
        # Never remove/replace this stable lock inode, including on release.
        self.handle = (state_home / "dashboard-owner.lock").open("a+b")
        try:
            if os.name == "nt":
                self.overlapped = _windows_lock(self.handle, exclusive=True)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.handle.close()
            busy = getattr(error, "winerror", None) == 33 if os.name == "nt" else error.errno in (errno.EAGAIN, errno.EACCES)
            if not busy:
                raise RuntimeError(f"HOST_PROFILE_LOCK_FAILED: Cannot lock the data profile: {error}") from error
            raise RuntimeError("HOST_PROFILE_IN_USE: Gitgo is already open for this data profile. "
                               "Close that Gitgo before opening another terminal; a saved terminal preference applies on the next launch.") from error

    def close(self):
        if self.handle.closed:
            return
        try:
            if os.name == "nt":
                _windows_lock(self.handle, exclusive=False, unlock=True, overlapped=self.overlapped)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
