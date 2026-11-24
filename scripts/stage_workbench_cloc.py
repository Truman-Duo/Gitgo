"""Stage an operator-supplied cloc and Perl bundle without downloading dependencies."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core.workbench.code_size import CLOC_VERSION, cloc_command, _run_cloc
from backend.core.tools.execution_profile import safe_environment


def _digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def stage(bundle, destination):
    bundle = bundle.resolve(strict=True)
    destination = destination.resolve(strict=True) / "_internal" / "backend" / "resources" / "cloc"
    if destination.exists() or destination.is_relative_to(bundle) or bundle.is_relative_to(destination):
        raise ValueError("Choose a fresh, independent Host staging directory")
    command = cloc_command(bundle)
    perl_version = (bundle / "PERL-VERSION").read_text(encoding="utf-8").strip()
    if not perl_version or not (bundle / "PERL-NOTICES").stat().st_size:
        raise ValueError("Provide the pinned Perl version and its matching notices")
    files = {}
    for path in sorted(bundle.rglob("*")):
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("The bundle must contain ordinary files and directories")
        if stat.S_ISREG(info.st_mode):
            files[path.relative_to(bundle).as_posix()] = _digest(path)
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("The bundle contains a non-regular input")
    env = {k: v for k, v in safe_environment().items() if not k.upper().startswith("PERL")}
    env["PATH"] = str(Path(command[0]).parent)
    result = subprocess.run([command[0], "-e", "print $^V"], cwd=bundle, input=b"",
                            capture_output=True, timeout=10, env=env)
    if result.returncode or result.stdout.decode("ascii").strip() != perl_version:
        raise ValueError("Bundled Perl does not match PERL-VERSION")
    _run_cloc(command, bundle, [])
    shutil.copytree(bundle, destination)
    if any(_digest(destination / name) != digest for name, digest in files.items()):
        raise ValueError("A cloc bundle input changed during staging")
    _run_cloc(cloc_command(destination), destination, [])
    manifest = {"cloc": CLOC_VERSION, "cloc_license": "GPL-2.0-or-later", "perl": perl_version, "sha256": files}
    (destination / "workbench-cloc-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    stage(args.bundle, args.destination)
