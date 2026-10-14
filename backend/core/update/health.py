"""Bounded explicit Host probe, with no inherited credentials/user profile.

The caller must validate signature, payload identity and ownership BEFORE
executing a candidate. This is a health probe, not an OS sandbox or installer.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

from .manifest import DataCompatibility, TARGETS
from .validation import fields, json_object, number, reject
from .version import Version


PROBE_ROLE = "update-data-probe"
MAX_REPORT_BYTES = 64 * 1024
MANAGED_API_VERSION = 6  # Stable Darwin volume identity; older bootstrap cannot adopt it.


def isolated_environment(profile: Path) -> dict[str, str]:
    """Shared bounded child environment; never inherit user credentials."""
    env = {"PATH": "/usr/bin:/bin" if os.name == "posix" else os.environ.get("PATH", ""),
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
           "TMPDIR": str(profile), "TEMP": str(profile), "TMP": str(profile),
           "GITGO_CONFIG_PATH": str(profile / "config.json"),
           "GITGO_STATE_HOME": str(profile / "state"),
           "GITGO_LLM_CONFIG_PATH": str(profile / "providers.json"),
           "GITGO_LLM_SECRET_PATH": str(profile / "secrets.json")}
    if os.name == "nt" and "SystemRoot" in os.environ:
        env["SystemRoot"] = os.environ["SystemRoot"]
    return env


def cleanup_report(proc, job) -> dict:
    """Report parent-owned process containment, never candidate assertions."""
    cleanup = getattr(proc, "_gitgo_cleanup", None)
    if os.name == "nt" and getattr(proc, "_gitgo_windows_scope", None) is None:
        cleanup = {"complete": False, "mode": "unmanaged-root-only",
                   "issues": ["preexecution_job_scope_missing"]}
    if not isinstance(cleanup, dict) or type(cleanup.get("complete")) is not bool:
        reject("UPDATE_HEALTH_FAILED", "Host probe cleanup report is missing")
    return cleanup


def probe_host(host: Path, profile: Path, *, timeout: int = 30) -> dict:
    return _probe_host(host, profile, timeout=timeout, managed=False)


def probe_managed_host(host: Path, profile: Path, *, timeout: int = 30) -> dict:
    return _probe_host(host, profile, timeout=timeout, managed=True)


def probe_startup_host(host: Path, profile: Path, *, timeout: int = 30) -> dict:
    return _probe_host(host, profile, timeout=timeout, managed=False, startup=True)


def _probe_host(host: Path, profile: Path, *, timeout: int, managed: bool, startup: bool = False) -> dict:
    number(timeout, minimum=1, maximum=120)
    if (not host.is_file() or host.is_symlink() or not host.is_absolute()
            or host != host.resolve(strict=True)
            or not profile.is_absolute() or profile != profile.resolve(strict=True)
            or profile.is_symlink() or not profile.is_dir() or any(profile.iterdir())):
        reject("UPDATE_HEALTH_FAILED", "Probe requires an explicit host and empty isolated profile")
    if os.name == "posix" and (profile.stat().st_uid != os.getuid() or profile.stat().st_mode & 0o077):
        reject("UPDATE_HEALTH_FAILED", "Probe profile must be private")
    nonce = uuid.uuid4().hex
    # Whitelist, rather than subtracting guessed names from the parent env.
    env = isolated_environment(profile)
    # Required Windows loader environment, never an application/user secret.
    from backend.core.process_control import spawn_managed, terminate_tree, attach_kill_job, close_job
    from backend.core.process_output import collect_output
    proc, job = None, None
    try:
        role = "managed-capabilities" if managed else "update-startup-probe" if startup else PROBE_ROLE
        proc = spawn_managed([str(host), "--gitgo-internal-role", role, "--nonce", nonce],
            cwd=str(profile), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="strict", env=env,
            macos_forkless=sys.platform == "darwin")
        job = attach_kill_job(proc)
        output, _, _ = collect_output(proc, timeout=timeout, limit=MAX_REPORT_BYTES,
                                     reject_overflow=True, terminate=lambda: terminate_tree(proc, job))
        if proc.returncode != 0:
            reject("UPDATE_HEALTH_FAILED", "Host probe exited unsuccessfully")
        # Keep runtime-owned cleanup separate from the candidate's JSON. macOS
        # owned-session-only may return complete=False; fingerprint collection
        # is still useful, but this result CANNOT authorize update commitment.
        cleanup = cleanup_report(proc, job)
        report = json_object(output.encode("utf-8"), MAX_REPORT_BYTES)
        if managed:
            fields(report, {"schema_version", "managed_api", "runtime"})
            if (type(report["schema_version"]) is not int or report["schema_version"] != 1
                    or type(report["managed_api"]) is not int or report["managed_api"] != MANAGED_API_VERSION):
                reject("UPDATE_HEALTH_FAILED", "Managed bootstrap capability is unavailable")
            report = report["runtime"]
        fields(report, {"schema_version", "nonce", "product_id", "version", "platform", "architecture",
                        "data_compatibility", "memory_sqlite_check"} | ({"native_host_check"} if startup else set()))
        if startup and report["native_host_check"] is not True:
            reject("UPDATE_HEALTH_FAILED", "Candidate Native Host startup failed")
        if (type(report["schema_version"]) is not int or report["schema_version"] != 1
                or report["nonce"] != nonce or report["memory_sqlite_check"] is not True):
            reject("UPDATE_HEALTH_FAILED", "Host probe identity or memory health evidence is invalid")
        DataCompatibility.parse(report["data_compatibility"])
        Version.parse(report["version"])
        if report["product_id"] != "gitgo-terminal" or (report["platform"], report["architecture"]) not in TARGETS:
            reject("UPDATE_HEALTH_FAILED", "Host target is unsupported")
        return {"runtime": report, "cleanup": cleanup}
    except Exception:
        if proc is not None and (proc.poll() is None or getattr(proc, "_gitgo_scope_socket", None) is not None):
            terminate_tree(proc, job)
        reject("UPDATE_HEALTH_FAILED", "Host data probe failed or exceeded its bounds")
    finally:
        close_job(job)


def frozen_health_check(bundle: Path, profile: Path):
    """Explicit transaction driver; invoke ONLY after full candidate validation.

    It is still not a production installer/launcher. An incomplete execution
    scope cannot commit a transaction. macOS uses a forkless probe only; this
    does not confer cleanup authority over an ordinary application session.
    """
    from .transaction import HealthReport
    product = json_object((bundle / "product.json").read_bytes(), MAX_REPORT_BYTES)
    build = json_object((bundle / "build-manifest.json").read_bytes(), 2 * 1024**2)
    contract = json_object((bundle / "update-data-contract.json").read_bytes(), MAX_REPORT_BYTES)
    expected = DataCompatibility.parse(contract["data_compatibility"])
    name = "gitgo-host.exe" if product["platform"] == "windows" else "gitgo-host"
    result = probe_startup_host(bundle / "internal/gitgo-host" / name, profile)
    runtime = result["runtime"]
    if result["cleanup"].get("complete") is not True:
        reject("UPDATE_HEALTH_FAILED", "Candidate execution cleanup is incomplete; cannot commit")
    for key in ("product_id", "version", "platform", "architecture"):
        if runtime[key] != product[key]:
            reject("UPDATE_HEALTH_FAILED", "Candidate runtime differs from package identity")
    if DataCompatibility.parse(runtime["data_compatibility"]) != expected:
        reject("UPDATE_HEALTH_FAILED", "Candidate runtime differs from signed migration evidence")
    dashboard_profile = profile / "dashboard"
    dashboard_profile.mkdir(mode=0o700)
    probe_dashboard_version(bundle, dashboard_profile, product["version"])
    return HealthReport(product["product_id"], runtime["version"], runtime["platform"],
                        runtime["architecture"], build["source"]["commit"], build["source"]["tree"], True)


def probe_dashboard_version(bundle: Path, profile: Path, version: str):
    """Exercise the actual frontend loader/imports without user profile or UI.

    This bounded early entry is not a visible terminal rendering acceptance.
    As with Host health, cleanup is judged by the parent-owned supervisor.
    """
    from backend.core.process_control import spawn_managed, terminate_tree, attach_kill_job, close_job
    from backend.core.process_output import collect_output
    Version.parse(version)
    program = bundle / ("gitgo.exe" if os.name == "nt" else "gitgo")
    proc, job = None, None
    try:
        proc = spawn_managed([str(program), "--version"], cwd=str(profile),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="strict", env=isolated_environment(profile),
            macos_forkless=sys.platform == "darwin")
        job = attach_kill_job(proc)
        output, _, _ = collect_output(proc, timeout=30, limit=4096, reject_overflow=True,
                                     terminate=lambda: terminate_tree(proc, job))
        if (proc.returncode != 0 or output != f"Gitgo {version}\n"
                or cleanup_report(proc, job).get("complete") is not True):
            reject("UPDATE_HEALTH_FAILED", "Candidate frontend startup or cleanup failed")
    except Exception:
        if proc is not None and (proc.poll() is None or getattr(proc, "_gitgo_scope_socket", None) is not None):
            terminate_tree(proc, job)
        reject("UPDATE_HEALTH_FAILED", "Candidate frontend startup failed")
    finally:
        close_job(job)
