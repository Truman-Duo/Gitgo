"""Opt-in managed-install/Dashboard acceptance in a disposable SIP-enabled VM.

Uses a fresh fixture project and loopback provider. No publisher trust is inferred
from an ad-hoc signature. The package is verified by its frozen backend, and the
public managed command is the only Dashboard entry. Root management from the
user-managed app is explicitly rejected; the verified independent release package
provides the administrator entry. Logs and private fixture state are retained;
only this run's uniquely named helper is uninstalled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os

# Keep report/fixture verification importable without native PTY support.
if os.name == 'posix':
    import fcntl

from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def verify_package(package, commit, tree):
    """Called only by the selected package's frozen Host, never source backend."""
    require(getattr(sys, "frozen", False), "Package validation requires the frozen Host")
    require(Path(sys.executable).resolve() == package / "internal/gitgo-host/gitgo-host",
            "Validator runtime does not belong to the selected package")
    from backend.core.update.contracts import verify_bundle_contract
    from backend.core.update.manifest import DataCompatibility
    from backend.core.update.validation import json_object
    build = json_object((package / "build-manifest.json").read_bytes(), 4 * 1024 * 1024)
    require(build.get("source") == {"commit": commit, "tree": tree, "dirty": False},
            "Package must match the requested clean source commit and tree")
    product = json_object((package / "product.json").read_bytes(), 128 * 1024)
    require(product.get("platform") == "macos" and product.get("architecture") == os.uname().machine,
            "Package target differs from this macOS VM")
    contract = json_object((package / "update-data-contract.json").read_bytes(), 1024 * 1024)
    identity = SimpleNamespace(**{key: product[key] for key in ("product_id", "version", "platform", "architecture")},
                               source_commit=commit, source_tree=tree,
                               data_compatibility=DataCompatibility.parse(contract["data_compatibility"]))
    verify_bundle_contract(package, identity)
    return {"source": build["source"], "version": product["version"],
            "platform": product["platform"], "architecture": product["architecture"],
            "build_manifest_sha256": digest(package / "build-manifest.json"),
            "inventory_verified": True, "frozen_validator": True}


def private_artifacts(value):
    if value is None:
        return Path(tempfile.mkdtemp(prefix="gitgo-managed-dashboard-reports-", dir="/private/var/tmp"))
    path = Path(value).absolute()
    require(not path.is_symlink(), "Artifact directory cannot be a symlink")
    if path.exists():
        require(path.is_dir() and not any(path.iterdir()), "Artifact directory must be new or empty")
    else:
        path.mkdir(mode=0o700)
    path = path.resolve(strict=True)
    info = path.stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o077,
            "Artifact directory must be private to the current user")
    return path


def command_env(profile, temporary):
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
            "HOME": str(profile), "TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "GITGO_STATE_HOME": str(profile / "state"), "GITGO_CONFIG_PATH": str(profile / "config.json"),
            "GITGO_LLM_CONFIG_PATH": str(profile / "llm.json"),
            "GITGO_LLM_SECRET_PATH": str(profile / "secrets.json")}


def output_json(path):
    # Frozen Host and helper entry points should return a single JSON document.
    require(path.stat().st_size <= 2 * 1024 * 1024, "JSON report exceeds acceptance budget")
    return json.loads(path.read_text())


def exclusive_runtime_lock(root, timeout=30):
    """The ordinary harness process must not itself take a managed runtime lease."""
    path = root / "control/runtime.lock"
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600, "Managed runtime lock identity is invalid")
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        current = os.fstat(descriptor)
        require((current.st_dev, current.st_ino) == (info.st_dev, info.st_ino), "Runtime lock changed")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return {"exclusive": True, "device": current.st_dev, "inode": current.st_ino}
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Managed runtime lease remained busy after Dashboard exit") from None
                time.sleep(.2)
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--allow-local-adhoc", action="store_true")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--probe", type=Path, default=Path(__file__).with_name("macos_dashboard_approval_probe.py"))
    parser.add_argument("--verify-package", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    require(args.run and args.allow_local_adhoc, "Use --run --allow-local-adhoc for local VM acceptance")
    require(sys.platform == "darwin" and 501 <= os.getuid() < 200000000 and os.getuid() == os.geteuid(),
            "Run as an ordinary macOS login account, not root")
    require(all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (args.source_commit, args.source_tree)),
            "Provide full source commit and tree SHA-1 identities")
    model = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], text=True).strip()
    require(model.startswith("VirtualMac"), "This acceptance modifies only a disposable macOS VM")
    sip = subprocess.check_output(["/usr/bin/csrutil", "status"], text=True).strip()
    require(sip == "System Integrity Protection status: enabled.", "SIP must be fully enabled")
    package = args.package.resolve(strict=True)
    require(package.is_dir(), "Provide a complete extracted package")
    if args.verify_package:
        print(json.dumps(verify_package(package, args.source_commit, args.source_tree)))
        return 0
    require(not getattr(sys, "frozen", False), "Run the harness with system Python so runtime-lock checks remain independent")
    probe = args.probe.resolve(strict=True)
    require(probe.is_file(), "Dashboard approval probe is missing")
    artifacts = private_artifacts(args.artifact_dir)
    require(not artifacts.is_relative_to(package) and not package.is_relative_to(artifacts),
            "Artifact directory and package must be separate")
    base = Path(tempfile.mkdtemp(prefix="gitgo-managed-dashboard-", dir="/private/var/tmp"))
    profile, temporary, project = (base / name for name in ("profile", "tmp", "project"))
    for path in (profile, temporary, project):
        path.mkdir(mode=0o700)
    app, bins = base / "app", base / "bin"
    environment = command_env(profile, temporary)
    service = "managed-" + uuid.uuid4().hex[:20]
    label = "org.gitgo.sandbox." + service
    registry = Path("/private/var/db/gitgo-sandbox-v1") / service
    plist = Path("/Library/LaunchDaemons") / (label + ".plist")
    host = package / "internal/gitgo-host/gitgo-host"
    installed_helper = app / "active/sandbox-helper.sh"
    helper = package / "sandbox-helper.sh"
    report = {"passed": False, "production_enabled": False, "publisher_trust_verified": False,
              "virtual_machine": model, "sip": sip, "service": service,
              "helper_management_source": "verified-independent-release-package",
              "helper_bundle_source": str(package / "internal/gitgo-host"),
              "artifact_dir": str(artifacts), "fixture_root": str(base),
              "scripts": {"harness_sha256": digest(Path(__file__)), "probe_sha256": digest(probe)},
              "commands": [], "checks": {}, "cleanup": {}}
    attempted = False
    completed = False
    failure = None

    def run(name, command, *, timeout=90, allow_failure=False, cwd=base):
        outpath, errpath = artifacts / (name + ".stdout"), artifacts / (name + ".stderr")
        with outpath.open("xb") as output, errpath.open("xb") as errors:
            proc = subprocess.Popen([str(value) for value in command], cwd=cwd, env=environment,
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                    start_new_session=True)
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Only this newly created process group is eligible for cleanup.
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=10)
                report["commands"].append({"name": name, "returncode": proc.returncode, "timeout": True})
                raise RuntimeError(name + " exceeded its time budget") from None
        report["commands"].append({"name": name, "returncode": code})
        if not allow_failure:
            require(code == 0, name + " failed; inspect its retained stdout/stderr")
        return code, outpath

    try:
        run("sudo-authority", ["/usr/bin/sudo", "-n", "/usr/bin/true"])
        require(not os.path.lexists(registry) and not os.path.lexists(plist), "Generated helper name is already in use")
        run("host-signature", ["/usr/bin/codesign", "--verify", "--deep", "--strict", host])
        run("dashboard-signature", ["/usr/bin/codesign", "--verify", "--strict", package / "gitgo"])
        _, validated = run("package-contract", [host, "--gitgo-internal-role", "python", "-B", "-I",
            Path(__file__).resolve(), "--run", "--allow-local-adhoc", "--verify-package", "--package", package,
            "--source-commit", args.source_commit, "--source-tree", args.source_tree])
        report["package"] = output_json(validated)
        _, installed = run("managed-install", ["/bin/bash", package / "install.sh", "--managed", "--no-path-update",
            "--install-root", app, "--bin-dir", bins], timeout=180)
        report["installation"] = output_json(installed)
        require(report["installation"].get("managed") is True, "Installer did not create a managed installation")
        require((bins / "gitgo").is_symlink() and (bins / "gitgo").readlink() == app / "gitgo",
                "Public command does not point to the stable managed launcher")
        run("fixture-init", ["/usr/bin/git", "init", "-q"], cwd=project)
        (project / "fixture.txt").write_bytes(b"sealed fixture\n")
        run("fixture-add", ["/usr/bin/git", "add", "fixture.txt"], cwd=project)
        run("fixture-commit", ["/usr/bin/git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", "Fixture"], cwd=project)
        _, commit_file = run("fixture-head", ["/usr/bin/git", "rev-parse", "HEAD"], cwd=project)
        commit = commit_file.read_text().strip()
        require(re.fullmatch(r"[0-9a-f]{40}", commit), "Fixture commit identity is missing")
        # This is an intentional boundary test, not a failed-install fallback.
        # Root must not reuse the ordinary owner's managed application runtime.
        attempted = True
        code, _ = run("installed-helper-entry-rejected", ["/usr/bin/sudo", "-n", installed_helper,
            "install", "--name", service, "--project", project, "--uid", str(os.getuid()),
            "--lifetime", "1800", "--bundle", app / "active/internal/gitgo-host", "--allow-local-adhoc"],
            timeout=90, allow_failure=True)
        rejection = (artifacts / "installed-helper-entry-rejected.stderr").read_text()
        require(code == 2 and "MACOS_HELPER_MANAGED_LOCATION" in rejection,
                "Installed helper entry did not give the expected explicit managed-path rejection")
        require(not os.path.lexists(registry) and not os.path.lexists(plist),
                "Rejected installed helper entry created service state")
        absent_code, _ = run("installed-helper-no-service", ["/usr/bin/sudo", "-n", "/bin/launchctl",
            "print", "system/" + label], allow_failure=True)
        require(absent_code == 113, "Rejected installed helper entry left a loaded service")
        report["checks"]["installed_helper_entry_rejected_without_registration"] = True
        run("helper-install", ["/usr/bin/sudo", "-n", helper, "install", "--name", service, "--project", project,
            "--uid", str(os.getuid()), "--lifetime", "1800", "--bundle", package / "internal/gitgo-host",
            "--allow-local-adhoc"], timeout=180)
        # The packaged driver remains outside app/active so it does not hold the
        # managed lease whose release is checked after the Dashboard has exited.
        _, entry = run("dashboard-approval", [host, "--gitgo-internal-role", "python", "-B", "-I", probe,
            "--service", service, "--workspace", project, "--commit", commit,
            "--command", bins / "gitgo", "--artifact-dir", artifacts], timeout=300)
        report["dashboard"] = output_json(entry)
        require(report["dashboard"].get("passed") is True, "Dashboard approval checks did not pass")
        report["checks"]["runtime_lock_after_exit"] = exclusive_runtime_lock(app)
        _, version_file = run("public-command-reopen", [bins / "gitgo", "--version"], timeout=60)
        version = version_file.read_text().strip()
        require(report["package"]["version"] in version, "Original public command did not reopen the installed version")
        report["checks"]["reopened_version"] = version
        report["checks"]["runtime_lock_after_reopen"] = exclusive_runtime_lock(app)
        completed = True
    except (Exception, KeyboardInterrupt) as exc:
        failure = type(exc).__name__ + ": " + str(exc)
    finally:
        if attempted:
            try:
                if os.path.lexists(registry):
                    code, _ = run("helper-uninstall", ["/usr/bin/sudo", "-n", helper, "uninstall", "--name", service],
                                  timeout=180, allow_failure=True)
                    report["cleanup"]["uninstall_returncode"] = code
                else:
                    report["cleanup"]["uninstall_returncode"] = None
                    report["cleanup"]["registration_not_created"] = True
                code, _ = run("helper-unloaded", ["/usr/bin/sudo", "-n", "/bin/launchctl", "print", "system/" + label],
                              allow_failure=True)
                report["cleanup"].update(launchd_unloaded=code == 113,
                    credential_removed=not os.path.lexists(registry / "client.json"),
                    socket_removed=not os.path.lexists(registry / "broker.sock"),
                    launch_plist_removed=not os.path.lexists(plist))
                require(report["cleanup"]["uninstall_returncode"] in (None, 0) and all(report["cleanup"][key] for key in
                    ("launchd_unloaded", "credential_removed", "socket_removed", "launch_plist_removed")),
                    "Helper cleanup incomplete; retained records identify this run's service")
                report["cleanup"]["verified"] = True
            except Exception as exc:
                report["cleanup"]["error"] = type(exc).__name__ + ": " + str(exc)
                failure = failure or "Helper cleanup failed; consult retained report"
        else:
            report["cleanup"]["helper_install_not_attempted"] = True
        if failure:
            report["error"] = failure
        report["passed"] = completed and failure is None and report["cleanup"].get("verified") is True
        (artifacts / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
