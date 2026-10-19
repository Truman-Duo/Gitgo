"""Commands for Gitgo-owned child processes in source and frozen runtimes.

``sys.executable`` is a Python interpreter in source checkouts, but it is the
PyInstaller Native Host executable in a terminal distribution.  Every owned
child must go through this resolver instead of assuming those are equivalent.
"""

from __future__ import annotations

import sys
import re
from pathlib import Path


INTERNAL_ROLE_FLAG = "--gitgo-internal-role"
DAEMON_ROLE = "daemon"
TOOL_RUNNER_ROLE = "tool-runner"
PYTHON_ROLE = "python"
CREDENTIAL_CLEANUP_ROLE = "credential-cleanup"


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


def tool_runner_command(source_root: Path | None = None) -> list[str]:
    if is_frozen_runtime():
        return [sys.executable, INTERNAL_ROLE_FLAG, TOOL_RUNNER_ROLE]
    root = source_root or Path(__file__).resolve().parents[2]
    # Embedded Python may ignore cwd and PYTHONPATH, and its ._pth can name
    # another checkout. Bind this trusted source tree as an argv value rather
    # than changing the user's interpreter configuration or interpolating code.
    bootstrap = ("import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
                 "runpy.run_module('backend.core.tools.runner',run_name='__main__')")
    return [sys.executable, "-c", bootstrap, str(root.resolve())]


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


def workspace_python_command(argv: list[str], cwd: Path) -> list[str]:
    """Make managed/embedded Python behave like a normal project interpreter.

    The Windows embeddable distribution uses ``pythonXY._pth`` and therefore
    starts with ``safe_path=True``: neither a script's directory nor the current
    working directory is importable.  Agents used to burn many model turns
    diagnosing ``ModuleNotFoundError`` for a sibling test module.  The Host
    knows the workspace cwd exactly, so it supplies that deterministic runtime
    fact while preserving argv semantics and avoiding a shell.
    """
    launcher = str(argv[0]).casefold()
    remaining = [str(item) for item in argv[1:]]
    if (
        launcher == "py"
        and remaining
        and re.fullmatch(r"-\d+(?:\.\d+)?", remaining[0])
    ):
        remaining = remaining[1:]

    interpreter_flags: list[str] = []
    while remaining:
        item = remaining[0]
        if item == "-X" and len(remaining) >= 2:
            interpreter_flags.extend(remaining[:2])
            remaining = remaining[2:]
            continue
        if item in {"-B", "-E", "-I", "-O", "-OO", "-s", "-S", "-u"}:
            interpreter_flags.append(item)
            remaining = remaining[1:]
            continue
        break

    if not remaining:
        return python_command(interpreter_flags)

    bootstrap = f"import sys;sys.path.insert(0,{str(cwd)!r});"
    mode = remaining[0]
    if mode == "-c" and len(remaining) >= 2:
        return python_command([
            *interpreter_flags, "-c", bootstrap + remaining[1], *remaining[2:],
        ])
    if mode == "-m" and len(remaining) >= 2:
        module = remaining[1]
        module_args = remaining[2:]
        code = (
            bootstrap
            + "import runpy;"
            + f"sys.argv={[module, *module_args]!r};"
            + f"runpy.run_module({module!r},run_name='__main__',alter_sys=True)"
        )
        return python_command([*interpreter_flags, "-c", code])
    if not mode.startswith("-"):
        script = str((cwd / mode).resolve(strict=False)) if not Path(mode).is_absolute() else mode
        script_args = remaining[1:]
        code = (
            bootstrap
            + "import runpy;"
            + f"sys.argv={[script, *script_args]!r};"
            + f"runpy.run_path({script!r},run_name='__main__')"
        )
        return python_command([*interpreter_flags, "-c", code])
    return python_command([*interpreter_flags, *remaining])
