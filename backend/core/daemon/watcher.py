"""File watcher — watchdog-based workspace monitoring with debounce."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class WorkspaceWatcher(FileSystemEventHandler):
    """Monitor workspace directory for file changes.

    Uses a debounce timer to avoid firing on every single file event.
    After a burst of changes, waits debounce_sec seconds of silence,
    then fires on_dirty().
    """

    def __init__(
        self,
        workspace_path: Path,
        exclude_patterns: list[str],
        on_dirty: Callable[[], None],
        debounce_sec: float = 2.0,
    ):
        super().__init__()
        self._path = str(workspace_path)
        self._exclude = exclude_patterns
        self._on_dirty = on_dirty
        self._debounce = debounce_sec
        self._timer: threading.Timer | None = None
        self._changed: set[str] = set()
        self._observer = Observer()
        self._observer.schedule(self, self._path, recursive=True)
        self._started = False

    def on_any_event(self, event):
        if event.is_directory:
            return
        src = getattr(event, "src_path", "")
        if self._is_excluded(src):
            return
        # Track changed file
        rel = str(Path(src).relative_to(self._path)).replace("\\", "/")
        self._changed.add(rel)
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self._debounce, self._fire)
        self._timer.start()

    def _fire(self):
        changed = list(self._changed)
        self._changed.clear()
        # Try to pass changed files if callback accepts argument
        try:
            self._on_dirty(changed)
        except TypeError:
            self._on_dirty()

    def _is_excluded(self, path: str) -> bool:
        import fnmatch
        for pat in self._exclude:
            if fnmatch.fnmatch(path, pat):
                return True
        return False

    def start(self):
        self._observer.start()
        self._started = True

    def stop(self):
        if self._timer:
            self._timer.cancel()
        if self._started:
            self._observer.stop()
            self._observer.join()
