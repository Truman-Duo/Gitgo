"""Prepare an isolated, attributable clean source snapshot and pinned build tools."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))


def assets(root):
    from scripts.provision_ci_ripgrep import VERSION, ARCHIVES, NOTICE_FILES, archive_notices
    from scripts.ci_downloads import fetch_verified
    tools = root / "build-tools"
    tools.mkdir(exist_ok=True)
    suffix, expected = ARCHIVES["win32"]
    url = f"https://github.com/BurntSushi/ripgrep/releases/download/{VERSION}/ripgrep-{VERSION}-{suffix}"
    raw = fetch_verified(url, expected, limit=64 * 1024 * 1024)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        executable = [n for n in archive.namelist() if Path(n).name == "rg.exe"]
        if len(executable) != 1:
            raise ValueError("Ambiguous official ripgrep archive")
        (tools / "rg.exe").write_bytes(archive.read(executable[0]))
        (tools / "ripgrep-notices.txt").write_bytes(archive_notices([(n, archive.read(n)) for n in archive.namelist() if Path(n).name in NOTICE_FILES]))
    requested = {"altgraph", "packaging", "pyinstaller", "pyinstaller-hooks-contrib", "pefile", "pywin32-ctypes", "setuptools"}
    lock = (SOURCE / "packaging/requirements-build.lock").read_text(encoding="utf8")
    blocks = re.split(r"(?m)(?=^[A-Za-z][A-Za-z0-9_.-]*==)", lock)
    selected = [block for block in blocks if block.split("==", 1)[0].lower() in requested]
    if len(selected) != len(requested):
        raise ValueError("Pinned build dependency set is incomplete")
    (tools / "requirements-pyinstaller.lock").write_text("".join(selected), encoding="utf8")
    (tools / "provenance.json").write_text(json.dumps({"ripgrep": VERSION, "archive_url": url, "archive_sha256": expected,
        "requirements_source": "packaging/requirements-build.lock", "requirements_sha256": hashlib.sha256(lock.encode()).hexdigest()}, indent=2) + "\n")
    print(json.dumps({"build_tools": str(tools), "requirements": str(tools / "requirements-pyinstaller.lock")}), flush=True)


def snapshot(root):
    def git(*args, cwd=SOURCE):
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True).stdout
    checkout = Path(tempfile.mkdtemp(prefix="clean-build-", dir=root))
    base = git("rev-parse", "HEAD").decode().strip()
    patch = git("diff", "HEAD", "--binary")
    with zipfile.ZipFile(io.BytesIO(git("archive", "--format=zip", "HEAD"))) as archive:
        for member in archive.infolist():
            path = checkout / member.filename
            if not path.resolve().is_relative_to(checkout):
                raise ValueError("Invalid source archive path")
            if member.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(member))
    if patch:
        subprocess.run(["git", "-C", str(checkout), "apply", "--binary", "-"], input=patch, check=True, capture_output=True)
    new = git("ls-files", "--others", "--exclude-standard", "--", "backend", "cli", "docs", "packaging", "scripts", "tests").decode().splitlines()
    for name in new:
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / name, target)
    provenance = {"base_commit": base, "patch_sha256": hashlib.sha256(patch).hexdigest(), "new_files": new,
        "source_tree": str(SOURCE), "purpose": "local clean build acceptance snapshot; not a product release commit"}
    (checkout / "publish-build-source.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf8")
    git("init", cwd=checkout)
    git("config", "user.name", "Gitgo Build Acceptance", cwd=checkout)
    git("config", "user.email", "build@gitgo.invalid", cwd=checkout)
    git("add", "--all", cwd=checkout)
    git("commit", "-m", "build: local Issue 40 acceptance snapshot", cwd=checkout)
    if git("status", "--porcelain", cwd=checkout):
        raise ValueError("Reconstructed source checkout is not clean")
    (root / "clean-build-latest.txt").write_text(str(checkout), encoding="utf8")
    print(json.dumps({"checkout": str(checkout), "base": base, "snapshot_commit": git("rev-parse", "HEAD", cwd=checkout).decode().strip()}), flush=True)


def runtime(root):
    """Copy tool binaries and dependencies, never an older product checkout."""
    checkout = Path((root / "clean-build-latest.txt").read_text(encoding="utf8")).resolve()
    if checkout.parent != root:
        raise ValueError("Build checkout must be inside the approved root")
    tools = root / "build-tools"
    destination = tools / "python"
    dependencies = tools / "runtime-dependencies"
    if not destination.exists():
        shutil.copytree(Path(sys.executable).parent, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if not dependencies.exists():
        original = SOURCE / "alpha3-work/.venv/Lib/site-packages"
        shutil.copytree(original, dependencies, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    paths = ["python314.zip", ".", str(checkout), str(dependencies), str(dependencies / "win32"), str(dependencies / "win32/lib")]
    (destination / "python314._pth").write_text("\n".join(paths) + "\n", encoding="utf8")
    print(json.dumps({"python": str(destination / "python.exe"), "dependencies": str(dependencies), "checkout": str(checkout)}), flush=True)


def notices(root):
    """Keep original dependency notices with the local build handoff."""
    import email.parser
    checkout = Path((root / "clean-build-latest.txt").read_text(encoding="utf8")).resolve()
    if checkout.parent != root:
        raise ValueError("Build checkout must be inside the approved root")
    output = root / "dependency-notices"
    output.mkdir(exist_ok=True)
    rows = []
    def collect(label, directory, metadata):
        label = re.sub(r"[^a-zA-Z0-9_.-]", "_", label)
        target = output / label
        target.mkdir(exist_ok=True)
        paths = []
        for path in directory.rglob("*"):
            if not path.is_file() or path.is_symlink() or "node_modules" in path.relative_to(directory).parts:
                continue
            if not path.name.upper().startswith(("LICENSE", "LICENCE", "NOTICE", "COPYING", "UNLICENSE")):
                continue
            if path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("Oversized license input")
            relative = path.relative_to(directory)
            copied = target / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, copied)
            paths.append(str(copied.relative_to(output)))
        rows.append({**metadata, "notices": paths})
    for folder in (root / "build-tools/runtime-dependencies", root / "build-tools/packages"):
        for directory in sorted(folder.glob("*.dist-info")):
            source = directory / "METADATA"
            if not source.exists():
                continue
            data = email.parser.Parser().parsestr(source.read_text(encoding="utf8"))
            collect(folder.name + "-" + directory.name, directory, {"ecosystem": "python", "name": data.get("Name"),
                "version": data.get("Version"), "license": data.get("License-Expression") or data.get("License"),
                "classifiers": data.get_all("Classifier", []), "scope": folder.name})
    modules = checkout / "cli/dashboard/node_modules"
    packages = list(modules.glob("*/package.json")) + list(modules.glob("@*/*/package.json"))
    for path in sorted(packages):
        data = json.loads(path.read_text(encoding="utf8"))
        collect("javascript-" + data.get("name", path.parent.name), path.parent,
                {"ecosystem": "javascript", "name": data.get("name"), "version": data.get("version"), "license": data.get("license")})
    shutil.copyfile(checkout / "LICENSE.md", output / "Gitgo-LICENSE.md")
    shutil.copyfile(root / "build-tools/python/LICENSE.txt", output / "Python-LICENSE.txt")
    shutil.copyfile(root / "build-tools/ripgrep-notices.txt", output / "ripgrep-notices.txt")
    (output / "inventory.json").write_text(json.dumps(rows, indent=2, ensure_ascii=True) + "\n", encoding="utf8")
    print(json.dumps({"notices": str(output), "packages": len(rows), "missing_notice_files": [row["name"] for row in rows if not row["notices"]]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["assets", "snapshot", "runtime", "notices"])
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    destination = Path(args.root).resolve()
    if not destination.is_dir() or not (destination / "targets.json").is_file():
        raise SystemExit("Use the existing Desktop acceptance root")
    {"assets": assets, "snapshot": snapshot, "runtime": runtime, "notices": notices}[args.phase](destination)
