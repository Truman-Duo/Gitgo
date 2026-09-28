"""Install the repository's runtime packages into a prepared Gitgo Python.

The embedded runtime intentionally does not mutate a system Python. A caller
supplies any trusted bootstrap Python that already has pip; pip resolves wheels
into the embedded runtime's isolated ``packages`` directory. The operation is
explicit and suitable for release preparation, never a Dashboard-start side
effect.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone
import uuid


_PROBE_SOURCE = (
    "import httpx,pytest,pypdf,reportlab,docx,pptx,openpyxl,xlrd,yaml,rich,paramiko,watchdog,mcp; "
    "from rich.console import Console; from watchdog.observers import Observer; "
    "from mcp.server.fastmcp import FastMCP; "
    "assert callable(httpx.AsyncClient) and callable(yaml.safe_load) and "
    "callable(pytest.main) and callable(Console) and callable(Observer) and callable(FastMCP); print('ok')"
)


def _probe(runtime: Path, packages: Path | None = None) -> str:
    source = _PROBE_SOURCE
    if packages is not None:
        # ``pip --target`` correctly lays out pywin32 but Python's isolated
        # ``._pth`` mode does not process the target's pywin32.pth file.
        # Add the same two deterministic search paths explicitly for both the
        # pre-activation probe and the final runtime pointer.
        search = [str(packages), str(packages / "win32"), str(packages / "win32" / "lib")]
        source = f"import sys; sys.path[:0] = {search!r}; " + source
    return subprocess.check_output(
        [str(runtime / "python.exe"), "-B", "-c", source],
        text=True, timeout=30,
    ).strip()


def _activate_package_set(runtime: Path, directory_name: str) -> None:
    pth_files = list(runtime.glob("python*._pth"))
    if len(pth_files) != 1:
        raise ValueError("prepared runtime must contain exactly one python*._pth file")
    pth = pth_files[0]
    lines = pth.read_text(encoding="utf-8").splitlines()
    entries = [directory_name, f"{directory_name}\\win32", f"{directory_name}\\win32\\lib"]
    replaced = False
    updated = []
    for line in lines:
        if line == "packages" or line.startswith("packages-"):
            if not replaced:
                updated.extend(entries)
                replaced = True
            continue
        updated.append(line)
    if not replaced:
        insertion = next((i for i, line in enumerate(updated) if line == "import site"), len(updated))
        updated[insertion:insertion] = entries
    temporary = pth.with_suffix(pth.suffix + ".tmp")
    temporary.write_text("\n".join(updated) + "\n", encoding="utf-8")
    temporary.replace(pth)


def sync(
    bootstrap_python: Path,
    runtime: Path,
    requirements: Path,
    bootstrap_packages: Path | None = None,
) -> dict:
    bootstrap_python = bootstrap_python.resolve()
    runtime = runtime.resolve()
    requirements = requirements.resolve()
    if not bootstrap_python.is_file() or not (runtime / "python.exe").is_file():
        raise ValueError("bootstrap Python and prepared Gitgo runtime are required")
    if not requirements.is_file() or not runtime.is_dir():
        raise ValueError("requirements.txt and a prepared runtime directory are required")
    requirements_digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    manifest_path = runtime / "gitgo-runtime.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) \
            if manifest_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        manifest = {}
    active_name = ""
    try:
        probe = _probe(runtime)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        probe = ""
    if probe != "ok" or manifest.get("packages_requirements_sha256") != requirements_digest:
        # Never mutate a package directory that a live Dashboard may have
        # imported from. Build and verify a complete immutable set, then switch
        # only the startup pointer; existing processes continue on the old set.
        staging = runtime / ("packages.staging-" + uuid.uuid4().hex)
        staging.mkdir()
        pip_prefix = [str(bootstrap_python), "-m", "pip"]
        if bootstrap_packages is not None:
            bootstrap_packages = bootstrap_packages.resolve()
            if not (bootstrap_packages / "pip").is_dir():
                raise ValueError("bootstrap package path must contain pip")
            pip_prefix = [
                str(bootstrap_python), "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
                    # Some deliberately minimal Windows images expose winreg
                    # but omit Explorer's legacy Shell Folders values.  pip's
                    # vendored platformdirs then crashes before parsing
                    # --isolated.  Environment-backed known folders are the
                    # correct deterministic source for this release helper.
                    "import pip._vendor.platformdirs.windows as _win_dirs; "
                    "_win_dirs.get_win_folder=_win_dirs.get_win_folder_from_env_vars; "
                    "from pip._internal.cli.main import main; raise SystemExit(main())"
                ),
                str(bootstrap_packages),
            ]
        try:
            subprocess.run(pip_prefix + [
                # Embedded Windows runtimes can lack the registry-backed common
                # app-data folder queried by pip's normal config discovery.  The
                # release package set must be reproducible and must never inherit
                # machine/user pip configuration in any case.
                "--isolated", "install",
                "--disable-pip-version-check", "--no-warn-script-location",
                "--target", str(staging), "-r", str(requirements),
            ], check=True)
            probe = _probe(runtime, staging)
            if probe != "ok":
                raise RuntimeError("staged runtime dependency probe failed")
        except BaseException:
            # A failed package build is not a release asset.  Leaving a complete
            # pip target for every retry can silently consume gigabytes over
            # time, so remove only the UUID-owned staging directory.  Activated
            # immutable package sets are never touched here.
            shutil.rmtree(staging, ignore_errors=True)
            raise
        active_name = f"packages-{requirements_digest[:12]}-{uuid.uuid4().hex[:8]}"
        staging.rename(runtime / active_name)
        _activate_package_set(runtime, active_name)
    else:
        pth_files = list(runtime.glob("python*._pth"))
        if len(pth_files) == 1:
            active_name = next(
                (line for line in pth_files[0].read_text(encoding="utf-8").splitlines()
                 if line == "packages" or line.startswith("packages-")),
                "packages",
            )
    if probe != "ok":
        raise RuntimeError("runtime dependency probe failed")
    result = {
        "runtime": str(runtime),
        "requirements_sha256": requirements_digest,
        "active_packages": active_name,
        "verified_modules": [
            "httpx", "pypdf", "reportlab", "docx", "pptx", "openpyxl", "xlrd",
            "yaml", "rich", "paramiko", "watchdog", "pytest",
            "mcp",
        ],
    }
    manifest.update({
        "packages_requirements_sha256": result["requirements_sha256"],
        "active_packages": result["active_packages"],
        "verified_modules": result["verified_modules"],
        "packages_verified_at": datetime.now(timezone.utc).isoformat(),
    })
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.replace(manifest_path)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bootstrap_python", type=Path)
    parser.add_argument("runtime", type=Path)
    parser.add_argument(
        "--requirements", type=Path,
        default=Path(__file__).resolve().parents[1] / "requirements-portable.txt",
    )
    parser.add_argument(
        "--bootstrap-packages", type=Path,
        help="Optional isolated site-packages directory containing pip",
    )
    args = parser.parse_args()
    print(json.dumps(sync(
        args.bootstrap_python,
        args.runtime,
        args.requirements,
        args.bootstrap_packages,
    ), indent=2))
