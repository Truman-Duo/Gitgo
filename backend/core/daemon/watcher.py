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
        on_dirty: Callable[[list[str]], None],
        debounce_sec: float = 2.0,
    ):
        super().__init__()
        self._path = str(workspace_path)
        self._exclude = exclude_patterns
        self._on_dirty = on_dirty
        self._debounce = debounce_sec
        self._timer: threading.Timer | None = None
        self._changed: set[str] = set()
        self._lock = threading.Lock()
        self._observer = Observer()
        self._observer.schedule(self, self._path, recursive=True)
        self._started = False

    def on_any_event(self, event):
        if event.is_directory:
            return
        # Atomic writers create a sibling temporary file and then move it over
        # the requested destination.  Watchdog reports that as a moved event:
        # ``src_path`` is the now-vanished temporary file while ``dest_path`` is
        # the authoritative workspace path.  Recording only ``src_path`` makes
        # all content-aware policy checks inspect a file that no longer exists.
        candidates = [getattr(event, "src_path", "")]
        destination = getattr(event, "dest_path", "")
        if destination:
            candidates.append(destination)

        changed: set[str] = set()
        for candidate in candidates:
            if not candidate or self._is_excluded(candidate):
                continue
            try:
                rel = str(Path(candidate).relative_to(self._path)).replace("\\", "/")
            except ValueError:
                continue
            changed.add(rel)
        if not changed:
            return

        # The observer callback and the debounce timer run on different
        # threads.  Merge and drain the burst atomically so a write arriving
        # during ``_fire`` cannot be silently cleared.
        with self._lock:
            self._changed.update(changed)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._fire)
            self._timer.start()

    def _fire(self):
        with self._lock:
            changed = sorted(self._changed)
            self._changed.clear()
            self._timer = None
        # Try to pass changed files if callback accepts argument
        try:
            self._on_dirty(changed)
        except TypeError:
            self._on_dirty()

    def _is_excluded(self, path: str) -> bool:
        import fnmatch
        try:
            relative = Path(path).resolve().relative_to(Path(self._path).resolve()).as_posix()
        except (OSError, ValueError):
            relative = Path(path).as_posix()
        while relative.startswith("./"):
            relative = relative[2:]
        basename = Path(relative).name
        for raw_pattern in self._exclude:
            pattern = str(raw_pattern).replace("\\", "/")
            while pattern.startswith("./"):
                pattern = pattern[2:]
            if not pattern:
                continue
            if pattern.endswith("/"):
                directory = pattern.rstrip("/")
                relative_folded = relative.casefold()
                directory_folded = directory.casefold()
                segments = [item.casefold() for item in relative.split("/") if item]
                if (
                    relative_folded == directory_folded
                    or relative_folded.startswith(directory_folded + "/")
                    or ("/" not in directory_folded and directory_folded in segments)
                ):
                    return True
                continue
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(basename, pattern):
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
