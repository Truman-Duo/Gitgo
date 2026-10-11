"""Shared Host process environment and shell identity policy."""
from __future__ import annotations
import os
import stat
import sys
import uuid
import subprocess
from pathlib import Path


def safe_environment():
    sensitive = ("API_KEY", "APIKEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL",
        "AUTHORIZATION", "PRIVATE_KEY", "ACCESS_KEY", "BEARER", "COOKIE")
    injection = {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "LD_PRELOAD", "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES", "PYTHONSTARTUP", "PYTHONPATH", "NODE_OPTIONS", "RUBYOPT", "PERL5OPT"}
    return {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in sensitive)
        and k.upper() not in injection and not k.upper().startswith("BASH_FUNC_")}


def verify_posix_bash(path):
    """Trust system ownership before executing a behavior probe; never PATH names."""
    from backend.core.executable_identity import posix_file_identity, assert_identity_unchanged
    try:
        candidate = Path(path).resolve(strict=True)
        if not candidate.is_file(): raise ValueError("Bash must be a regular executable")
        for item in (candidate, *candidate.parents):
            info = item.stat()
            if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError("Bash and its installation parents must be owned by root and not writable by other users")
        identity = {"files": [posix_file_identity(candidate)]}
        nonce = uuid.uuid4().hex
        result = subprocess.run([str(candidate), "--noprofile", "--norc", "-c",
            f'[[ -n "$BASH_VERSION" ]] && printf "gitgo:{nonce}:%s\\n" "$BASH_VERSION"'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=4, env=safe_environment())
        if result.returncode or not result.stdout.startswith(f"gitgo:{nonce}:"):
            raise ValueError("Trusted executable failed the Bash behavior probe")
        assert_identity_unchanged(identity)
        return {"path": str(candidate), "identity": identity, "shell": "bash"}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"error": "BASH_IDENTITY_UNVERIFIED", "detail": str(exc)}


def powershell_profile():
    if sys.platform != "win32":
        return {"error": "SHELL_UNAVAILABLE", "detail": "PowerShell profile is currently verified only on Windows"}
    from backend.core.executable_identity import file_identity, windows_signatures, windows_system_executable
    path = windows_system_executable("System32/WindowsPowerShell/v1.0/powershell.exe")
    try:
        import re
        identity = file_identity(path)
        signature = windows_signatures([path])[0]
        if signature.get("status") != "Valid" or not re.search(r"(?:^|,\s*)CN=Microsoft (?:Windows|Corporation)(?:,|$)", str(signature.get("subject", ""))):
            raise ValueError("PowerShell is not a verified Microsoft system image")
        return {"path": str(path), "identity": {"files": [identity]}, "shell": "powershell"}
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        return {"error": "SHELL_IDENTITY_UNVERIFIED", "detail": str(exc)}
