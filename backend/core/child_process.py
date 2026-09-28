"""Commands for Gitgo-owned child processes in source and frozen runtimes.

``sys.executable`` is a Python interpreter in source checkouts, but it is the
PyInstaller Native Host executable in a terminal distribution.  Every owned
child must go through this resolver instead of assuming those are equivalent.
"""

from __future__ import annotations

import sys
from pathlib import Path


INTERNAL_ROLE_FLAG = "--gitgo-internal-role"
DAEMON_ROLE = "daemon"
TOOL_RUNNER_ROLE = "tool-runner"
PYTHON_ROLE = "python"


def is_frozen_runtime() -> bool:
    return bool(getattr(sys, "frozen", False))


def daemon_command(project_name: str, source_root: Path) -> list[str]:
    common = [
        "--project", project_name,
        "--trial-interval", "9999",
        "--debounce", "2.0",
    ]
    if is_frozen_runtime():
        return [sys.executable, INTERNAL_ROLE_FLAG, DAEMON_ROLE, *common]
    return [
        sys.executable, str(source_root / "__main__.py"),
        "--mode", "daemon", *common,
        "--daemon-action", "start",
    ]


def tool_runner_command() -> list[str]:
    if is_frozen_runtime():
        return [sys.executable, INTERNAL_ROLE_FLAG, TOOL_RUNNER_ROLE]
    return [sys.executable, "-m", "backend.core.tools.runner"]


def python_command(arguments: list[str]) -> list[str]:
    """Run Python semantics without mistaking a frozen Host for python.exe."""
    if is_frozen_runtime():
        return [sys.executable, INTERNAL_ROLE_FLAG, PYTHON_ROLE, *arguments]
    return [sys.executable, *arguments]


def owned_child_cwd(source_root: Path) -> Path:
    """Use an installed, stable directory instead of PyInstaller's bundle dir."""
    if is_frozen_runtime():
        return Path(sys.executable).resolve().parent
    return source_root
