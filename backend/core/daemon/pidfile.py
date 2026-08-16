"""Pidfile — daemon PID file acquisition/release.

Extracted from daemon/__init__.py (pure structural refactor).
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.core.config import ProjectConfig


def _pid_file_path(project: ProjectConfig) -> Path:
    ws_path = project.workspace.file_access.path
    return Path(ws_path) / ".gitgo" / "daemon.pid"


def _acquire_pid_file(project: ProjectConfig) -> bool:
    """Create PID file. Returns False if another daemon is already running."""
    pid_path = _pid_file_path(project)
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text().strip())
            os.kill(old_pid, 0)  # signal 0 = existence check (Unix only)
        except (OSError, ValueError, ProcessLookupError, SystemError):
            pass  # stale or Windows — overwrite
        else:
            return False  # alive

    pid_path.write_text(str(os.getpid()))
    return True


def _release_pid_file(project: ProjectConfig) -> None:
    pid_path = _pid_file_path(project)
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass
