"""Frozen Gitgo Host entry point and private child-role multiplexer."""

from __future__ import annotations

import argparse
import runpy
import sys

from backend.core.child_process import (
    CREDENTIAL_CLEANUP_ROLE,
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


def _run_credential_cleanup_role(argv: list[str]) -> int:
    """Remove OS-backed provider credentials during a safe uninstall."""
    if argv:
        raise SystemExit(f"{CREDENTIAL_CLEANUP_ROLE} does not accept arguments")
    from backend.core.llm_config import LLMConfigManager

    LLMConfigManager._secret_store().clear()
    return 0


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    role = _role_arguments(values)
    from backend.core.update.installation import installed_runtime
    from backend.core.update.validation import UpdateError
    try:
        with installed_runtime(role[0] if role else None):
            return _main(values)
    except (UpdateError, OSError):
        from backend.core.protocol_io import dump_protocol_json
        print(dump_protocol_json({"protocol_version": 1, "type": "host_rejected",
            "code": "UPDATE_INSTALLATION_BLOCKED", "message": "Managed installation could not safely admit this operation"}), flush=True)
        return 2


def _main(values: list[str]) -> int:
    role = _role_arguments(values)
    if role is None:
        from backend.core.native_host import main as native_host_main
        return int(native_host_main() or 0)
    name, role_argv = role
    if name == "managed-install":
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--root", required=True)
        parser.add_argument("--bin-dir", required=True)
        parser.add_argument("--protected-path", action="append", default=[])
        args = parser.parse_args(role_argv)
        from backend.core.update.installation import install_from_frozen_package
        from backend.core.protocol_io import dump_protocol_json
        print(dump_protocol_json(install_from_frozen_package(args.root, args.bin_dir, args.protected_path)), flush=True)
        return 0
    if name in {"managed-launch", "managed-recover"}:
        from backend.core.update.installation import bootstrap_main
        return bootstrap_main(name, role_argv)
    if name == "managed-front-lease":
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--nonce", required=True)
        args = parser.parse_args(role_argv)
        from backend.core.update.installation import frontend_lease
        return frontend_lease(args.nonce)
    if name == "managed-capabilities":
        import os
        if os.name != "posix":
            from backend.core.update.validation import reject
            reject("UPDATE_TRANSACTION_PLATFORM", "Managed installation adapter is POSIX-only")
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--nonce", required=True)
        args = parser.parse_args(role_argv)
        from backend.core.update.runtime_contract import data_probe
        from backend.core.update.health import MANAGED_API_VERSION
        from backend.core.protocol_io import dump_protocol_json
        print(dump_protocol_json({"schema_version": 1, "managed_api": MANAGED_API_VERSION, "runtime": data_probe(args.nonce)}), flush=True)
        return 0
    if name == "update-recover-simulation":
        # No NativeHost, providers, project daemon or user config is initialized.
        # The helper reopens only explicitly registered simulation authority.
        from backend.core.update.recovery_helper import helper_main
        return helper_main(role_argv)
    if name == "update-startup-probe":
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--nonce", required=True)
        args = parser.parse_args(role_argv)
        from backend.core.update.startup_probe import startup_probe
        from backend.core.protocol_io import dump_protocol_json
        print(dump_protocol_json(startup_probe(args.nonce)), flush=True)
        return 0
    if name == "update-data-probe":
        # Before NativeHost, ConfigManager or any user's data store is opened.
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--nonce", required=True)
        args = parser.parse_args(role_argv)
        from backend.core.update.runtime_contract import data_probe
        from backend.core.protocol_io import dump_protocol_json
        print(dump_protocol_json(data_probe(args.nonce)), flush=True)
        return 0
    if name == DAEMON_ROLE:
        return _run_daemon_role(role_argv)
    if name == TOOL_RUNNER_ROLE:
        from backend.core.tools.runner import main as tool_runner_main
        tool_runner_main()
        return 0
    if name == PYTHON_ROLE:
        return _run_python_role(role_argv)
    if name == CREDENTIAL_CLEANUP_ROLE:
        return _run_credential_cleanup_role(role_argv)
    if name == "unix-scope":
        if not 1 <= len(role_argv) <= 17 or any(not value.isdigit() for value in role_argv):
            raise SystemExit("unix-scope requires one owned control descriptor")
        from backend.core.unix_process_scope import main as scope_main
        return scope_main(role_argv)
    raise SystemExit(f"unknown Gitgo internal role: {name}")


if __name__ == "__main__":
    raise SystemExit(main())
