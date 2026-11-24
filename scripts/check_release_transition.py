"""Verify the real predecessor/candidate archive pair on this target's native OS."""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.update.archive import ExtractionLimits, _extract_archive
from scripts.release_control.evidence import atomic, file_fact
from scripts.release_control.transition import SUITES, passing_cases


def native_target():
    system = {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(sys.platform)
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        machine = "x86_64"
    elif machine in {"arm64", "aarch64"}:
        machine = "aarch64" if system == "linux" else "arm64"
    return f"{system}-{machine}"


def stage(path, target, base):
    fact = file_fact(path)
    archive_format = "zip" if target.startswith("windows-") else "tar.gz"
    if not path.name.endswith("." + archive_format):
        raise ValueError("Archive format differs from the update target")
    with path.open("rb") as stream:
        bundle = _extract_archive(stream, archive_format, base / "stage", base, ExtractionLimits(), lambda: False)
    if file_fact(path) != fact:
        raise ValueError("Release archive changed during extraction")
    product = json.loads((bundle / "product.json").read_bytes())
    build = json.loads((bundle / "build-manifest.json").read_bytes())
    source = build["source"]
    if product["platform"] + "-" + product["architecture"] != target or source["dirty"] is not False:
        raise ValueError("Release archive target or clean source does not match")
    identity = {"version": product["version"], "source": {k: source[k] for k in ("commit", "tree")},
                "sha256": fact["sha256"]}
    return bundle, identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--scratch-parent", required=True, type=Path)
    args = parser.parse_args()
    target = native_target()
    if target not in SUITES:
        parser.error("Use a native runner for a supported release target")
    output = args.evidence.resolve()
    output.mkdir(parents=True, exist_ok=True)
    receipt, junit = (output / (target + suffix) for suffix in (".json", ".xml"))
    if receipt.exists() or junit.exists():
        parser.error("Use a fresh evidence directory for this attempt")
    previous_archive = args.previous.resolve(strict=True)
    candidate_archive = args.candidate.resolve(strict=True)
    scratch_parent = args.scratch_parent.resolve(strict=True)
    if os.name == "nt":
        from backend.core.update.windows_storage import validate_staging
        validate_staging(scratch_parent)
    with TemporaryDirectory(prefix="gitgo-release-pair-", dir=scratch_parent) as raw:
        scratch = Path(raw)
        old, new = scratch / "old", scratch / "new"
        old.mkdir(mode=0o700)
        new.mkdir(mode=0o700)
        old_stage, previous = stage(previous_archive, target, old)
        new_stage, candidate = stage(candidate_archive, target, new)
        suite, names = SUITES[target]
        env = dict(os.environ, GITGO_UPDATE_PREVIOUS_STAGE=str(old_stage),
                   GITGO_UPDATE_CANDIDATE_STAGE=str(new_stage), TEMP=raw, TMP=raw, TMPDIR=raw)
        pytest_main = "import sys; sys.path.insert(0, '.'); import pytest; raise SystemExit(pytest.main(sys.argv[1:]))"
        command = [sys.executable, "-B", "-c", pytest_main, "-q", "-p", "no:cacheprovider",
                   f"--junitxml={junit}", *(suite + "::" + name for name in names)]
        subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=1800)
        tests = passing_cases(junit, target)
        if (file_fact(previous_archive)["sha256"] != previous["sha256"]
                or file_fact(candidate_archive)["sha256"] != candidate["sha256"]):
            raise ValueError("Release input changed during native acceptance")
        atomic(receipt, {"schema_version": 1, "target": target, "previous": previous,
                         "candidate": candidate, "tests": tests})


if __name__ == "__main__":
    main()
