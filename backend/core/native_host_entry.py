"""Frozen Gitgo Host entry point and private child-role multiplexer."""

from __future__ import annotations

import argparse
import runpy
import sys

from backend.core.child_process import (
    DAEMON_ROLE,
    INTERNAL_ROLE_FLAG,
    PYTHON_ROLE,
    TOOL_RUNNER_ROLE,
)


def _role_arguments(argv: list[str]) -> tuple[str, list[str]] | None:
    try:
        index = argv.index(INTERNAL_ROLE_FLAG)
    except ValueError:
        return None
    if index + 1 >= len(argv):
        raise SystemExit(f"{INTERNAL_ROLE_FLAG} requires a role")
    return argv[index + 1], argv[index + 2:]


def _run_daemon_role(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project", required=True)
    parser.add_argument("--trial-interval", type=float, default=9999.0)
    parser.add_argument("--debounce", type=float, default=2.0)
    args = parser.parse_args(argv)

    from backend.core.config import ConfigManager
    from backend.core.daemon import run_daemon

    config = ConfigManager.load(strict=True)
    project = next((item for item in config.projects if item.name == args.project), None)
    if project is None:
        parser.error(f"unknown project: {args.project}")
    run_daemon(
        config,
        project,
        trial_interval=args.trial_interval,
        debounce_sec=args.debounce,
    )
    return 0


def _run_python_role(argv: list[str]) -> int:
    """Provide the bounded Python CLI forms used by approved workspace tools."""
    values = list(argv)
    while values:
        flag = values[0]
        if flag == "-X" and len(values) >= 2:
            values = values[2:]
            continue
        if flag in {"-B", "-E", "-I", "-s", "-S", "-u"}:
            if flag == "-B":
                sys.dont_write_bytecode = True
            values = values[1:]
            continue
        break
    if not values:
        raise SystemExit("interactive Python is not available in the packaged runtime")
    if values[0] == "-c" and len(values) >= 2:
        sys.argv = ["-c", *values[2:]]
        namespace = {"__name__": "__main__", "__package__": None}
        exec(compile(values[1], "<gitgo-python>", "exec"), namespace, namespace)
        return 0
    if values[0] == "-m" and len(values) >= 2:
        sys.argv = [values[1], *values[2:]]
        runpy.run_module(values[1], run_name="__main__", alter_sys=True)
        return 0
    if not values[0].startswith("-"):
        sys.argv = [values[0], *values[1:]]
        runpy.run_path(values[0], run_name="__main__")
        return 0
    raise SystemExit(f"unsupported packaged Python arguments: {values!r}")


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    role = _role_arguments(values)
    if role is None:
        from backend.core.native_host import main as native_host_main
        return int(native_host_main() or 0)
    name, role_argv = role
    if name == DAEMON_ROLE:
        return _run_daemon_role(role_argv)
    if name == TOOL_RUNNER_ROLE:
        from backend.core.tools.runner import main as tool_runner_main
        tool_runner_main()
        return 0
    if name == PYTHON_ROLE:
        return _run_python_role(role_argv)
    raise SystemExit(f"unknown Gitgo internal role: {name}")


if __name__ == "__main__":
    raise SystemExit(main())
