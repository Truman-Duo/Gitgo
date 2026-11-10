"""Bounded explicit Host probe, with no inherited credentials/user profile.

The caller must validate signature, payload identity and ownership BEFORE
executing a candidate. This is a health probe, not an OS sandbox or installer.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from time import monotonic
import uuid

from .manifest import DataCompatibility, TARGETS
from .validation import fields, json_object, number, reject, UpdateError
from .version import Version
from .persistence import pinned_profile


PROBE_ROLE = "update-data-probe"
MAX_REPORT_BYTES = 64 * 1024
from .bootstrap_contract import CURRENT_API as MANAGED_API_VERSION


class ProbeFailure(UpdateError):
    """Safe diagnostics from the updater, without candidate output or paths."""

    def __init__(self, probe, reason, *, started=None, timeout=None, proc=None, stderr=None, cause=None):
        cleanup = getattr(proc, "_gitgo_cleanup", None)
        self.cleanup = dict(cleanup) if isinstance(cleanup, dict) else None
        self.diagnostic = {
            "probe": probe,
            "reason": reason,
            "elapsed_ms": None if started is None else round((monotonic() - started) * 1000),
            "timeout_seconds": timeout,
            "exit_code": None if proc is None else proc.returncode,
            "errno": cause.errno if isinstance(cause, OSError) else None,
            "stderr_chars": None if stderr is None else len(stderr),
            "cleanup_issues": cleanup.get("issues", []) if isinstance(cleanup, dict) else [],
        }
        super().__init__("UPDATE_HEALTH_FAILED", f"{probe}: {reason}")


def _probe_reason(error, phase):
    from backend.core.process_output import OutputLimit
    if isinstance(error, (subprocess.TimeoutExpired, TimeoutError)):
        return phase + "_timeout"
    if isinstance(error, OutputLimit):
        return "output_limit"
    return phase


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


def execute_probe(program, profile, probe, *, timeout, nonce=None, plan=None, inherited_fds=()):
    """Run one fixed probe locally or through the inherited launcher capability."""
    from types import SimpleNamespace
    from .probe_broker import PROBES, current_client
    from backend.core.process_control import spawn_managed, terminate_tree, attach_kill_job, close_job
    from backend.core.process_output import collect_output
    if probe not in PROBES:
        reject("UPDATE_PROBE_CHANNEL", "Unsupported updater probe")
    client = current_client()
    proc, job, stderr = None, None, None
    started, phase = monotonic(), "spawn"
    try:
        if client is not None:
            response = client.request(probe, program, profile, timeout=timeout, nonce=nonce, plan=plan)
            proc = SimpleNamespace(returncode=response["exit_code"], _gitgo_cleanup=response["cleanup"])
            if response["failure"] is not None:
                detail = fields(response["failure"], {"probe", "reason", "elapsed_ms", "timeout_seconds",
                    "exit_code", "errno", "stderr_chars", "cleanup_issues"})
                if detail["probe"] != probe:
                    reject("UPDATE_PROBE_CHANNEL", "Launcher probe result has the wrong identity")
                error = ProbeFailure(probe, "launcher_failure", proc=proc)
                error.diagnostic = detail
                raise error
            output, stderr = response["output"], response["stderr"]
        else:
            if probe == "dashboard-version":
                command = [str(program), "--version"]
            elif probe == "update-data-migrate":
                command = [str(program), "--gitgo-internal-role", probe, "--plan", str(plan)]
            else:
                command = [str(program), "--gitgo-internal-role", probe, "--nonce", nonce]
            options = {"pass_fds": inherited_fds} if inherited_fds else {}
            proc = spawn_managed(command, cwd=str(profile),
                stdin=subprocess.PIPE if nonce is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="strict", env=isolated_environment(profile),
                macos_forkless=sys.platform == "darwin", **options)
            job = attach_kill_job(proc)
            phase = "collect"
            output, stderr, _ = collect_output(proc, timeout=timeout,
                limit=4096 if probe == "dashboard-version" else MAX_REPORT_BYTES,
                reject_overflow=True, terminate=lambda: terminate_tree(proc, job))
        phase = "exit_code"
        if proc.returncode != 0:
            reject("UPDATE_HEALTH_FAILED", "Candidate probe exited unsuccessfully")
        phase = "cleanup"
        cleanup = cleanup_report(proc, job)
        if probe in {"update-startup-probe", "dashboard-version", "update-data-migrate"} and cleanup["complete"] is not True:
            reject("UPDATE_HEALTH_FAILED", "Candidate execution cleanup is incomplete")
        return SimpleNamespace(output=output, stderr=stderr, proc=proc, cleanup=cleanup)
    except ProbeFailure:
        raise
    except Exception as error:
        if client is None and proc is not None and (proc.poll() is None or getattr(proc, "_gitgo_scope_socket", None) is not None):
            terminate_tree(proc, job)
        raise ProbeFailure(probe, _probe_reason(error, phase), started=started, timeout=timeout,
                           proc=proc, stderr=stderr, cause=error) from error
    finally:
        close_job(job)


@pinned_profile
def _probe_host(host: Path, profile: Path, *, timeout: int, managed: bool, startup: bool = False) -> dict:
    number(timeout, minimum=1, maximum=120)
    if (not host.is_file() or host.is_symlink() or not host.is_absolute()
            or host != host.resolve(strict=True)
            or not profile.is_absolute() or profile != profile.resolve(strict=True)
            or profile.is_symlink() or not profile.is_dir() or any(profile.iterdir())):
        reject("UPDATE_HEALTH_FAILED", "Probe requires an explicit host and empty isolated profile")
    if os.name == "posix" and (profile.stat().st_uid != os.getuid() or profile.stat().st_mode & 0o077):
        reject("UPDATE_HEALTH_FAILED", "Probe profile must be private")
    if os.name == "nt":
        from .windows_storage import validate_staging
        validate_staging(profile)
    nonce = uuid.uuid4().hex
    proc, stderr = None, None
    role = "managed-capabilities" if managed else "update-startup-probe" if startup else PROBE_ROLE
    started = monotonic()
    phase = "spawn"
    try:
        result = execute_probe(host, profile, role, timeout=timeout, nonce=nonce)
        output, stderr, proc = result.output, result.stderr, result.proc
        phase = "cleanup"
        # Keep runtime-owned cleanup separate from the candidate's JSON. macOS
        # owned-session-only may return complete=False; fingerprint collection
        # is still useful, but this result CANNOT authorize update commitment.
        cleanup = result.cleanup
        if startup and cleanup["complete"] is not True:
            reject("UPDATE_HEALTH_FAILED", "Candidate execution cleanup is incomplete")
        phase = "invalid_report"
        report = json_object(output.encode("utf-8"), MAX_REPORT_BYTES)
        if managed:
            fields(report, {"schema_version", "managed_api", "runtime"})
            if (type(report["schema_version"]) is not int or report["schema_version"] != 1
                    or type(report["managed_api"]) is not int or report["managed_api"] != MANAGED_API_VERSION):
                reject("UPDATE_HEALTH_FAILED", "Managed bootstrap capability is unavailable")
            report = report["runtime"]
        fields(report, {"schema_version", "nonce", "product_id", "version", "platform", "architecture",
                        "data_compatibility", "memory_sqlite_check"} | ({"native_host_check"} if startup else set()))
        phase = "native_host_check"
        if startup and report["native_host_check"] is not True:
            reject("UPDATE_HEALTH_FAILED", "Candidate Native Host startup failed")
        phase = "runtime_identity"
        if (type(report["schema_version"]) is not int or report["schema_version"] != 1
                or report["nonce"] != nonce or report["memory_sqlite_check"] is not True):
            reject("UPDATE_HEALTH_FAILED", "Host probe identity or memory health evidence is invalid")
        DataCompatibility.parse(report["data_compatibility"])
        Version.parse(report["version"])
        if report["product_id"] != "gitgo-terminal" or (report["platform"], report["architecture"]) not in TARGETS:
            reject("UPDATE_HEALTH_FAILED", "Host target is unsupported")
        return {"runtime": report, "cleanup": cleanup}
    except ProbeFailure:
        raise
    except Exception as error:
        raise ProbeFailure(role, _probe_reason(error, phase), started=started, timeout=timeout,
                           proc=proc, stderr=stderr, cause=error) from error


@pinned_profile
def frozen_health_check(bundle: Path, profile: Path):
    """Explicit transaction driver; invoke ONLY after full candidate validation.

    It is still not a production installer/launcher. An incomplete execution
    scope cannot commit a transaction. macOS uses a forkless probe only; this
    does not confer cleanup authority over an ordinary application session.
    """
    if os.name == "nt":
        # Never pretend LockFileEx authority transfers to a child. Probe an
        # exact, private, disposable snapshot outside the active fixed layout.
        # The original remains under the updater's exclusive sharing lease;
        # parent-owned Jobs/guardians retain it through verified cleanup.
        from .tree import inventory
        from .windows_storage import validate_staging, copy_tree
        if bundle == profile or bundle.is_relative_to(profile) or profile.is_relative_to(bundle):
            reject("UPDATE_HEALTH_FAILED", "Windows probe profile must be separate from the candidate program")
        validate_staging(profile)
        before = inventory(bundle)
        snapshot = profile / "program"
        copy_tree(bundle, snapshot)
        if inventory(snapshot) != before or inventory(bundle) != before:
            reject("UPDATE_HEALTH_FAILED", "Windows candidate changed during private probe preparation")
        runtime_profile = profile / "runtime"
        runtime_profile.mkdir(mode=0o700)
        result = _frozen_health_check(snapshot, runtime_profile)
        if inventory(snapshot) != before or inventory(bundle) != before:
            reject("UPDATE_HEALTH_FAILED", "Windows health changed the verified program tree")
        return result
    return _frozen_health_check(bundle, profile)


def _frozen_health_check(bundle: Path, profile: Path):
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
            raise ProbeFailure("update-startup-probe", "package_identity")
    if DataCompatibility.parse(runtime["data_compatibility"]) != expected:
        raise ProbeFailure("update-startup-probe", "data_compatibility")
    dashboard_profile = profile / "dashboard"
    dashboard_profile.mkdir(mode=0o700)
    probe_dashboard_version(bundle, dashboard_profile, product["version"])
    return HealthReport(product["product_id"], runtime["version"], runtime["platform"],
                        runtime["architecture"], build["source"]["commit"], build["source"]["tree"], True)


def probe_dashboard_version(bundle: Path, profile: Path, version: str):
    """Probe the real frontend loader; not visible terminal acceptance."""
    Version.parse(version)
    program = bundle / ("gitgo.exe" if os.name == "nt" else "gitgo")
    result = execute_probe(program, profile, "dashboard-version", timeout=30)
    if result.output != f"Gitgo {version}\n":
        raise ProbeFailure("dashboard-version", "version_mismatch", proc=result.proc, stderr=result.stderr)


def migrate_candidate_data(bundle: Path, profile: Path, plan: Path, nonce: str):
    """Run a verified candidate on shadows under the existing data/install barriers."""
    profile.mkdir(mode=0o700)
    program = bundle / "internal/gitgo-host" / ("gitgo-host.exe" if os.name == "nt" else "gitgo-host")
    try:
        result = execute_probe(program, profile, "update-data-migrate", timeout=120, plan=plan)
        report = fields(json_object(result.output.encode("utf-8"), MAX_REPORT_BYTES), {"schema_version", "nonce", "migration_verified"})
        if (type(report["schema_version"]) is not int or report["schema_version"] != 1
                or report["nonce"] != nonce or report["migration_verified"] is not True):
            reject("UPDATE_DATA_MIGRATION_FAILED", "Candidate data migration report is invalid")
    except Exception:
        reject("UPDATE_DATA_MIGRATION_FAILED", "Data migration failed; original data retained")
