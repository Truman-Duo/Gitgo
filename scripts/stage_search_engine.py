"""Stage an operator-supplied ripgrep and matching third-party notices.

No network or user runtime modification. The frozen Host owns engine lookup;
the release build records the exact binary and notice hashes for review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def _version(binary):
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=10,
                            creationflags=0x08000000 if os.name == "nt" else 0)
    line = result.stdout.splitlines()[0] if result.stdout else ""
    match = re.fullmatch(r"ripgrep (\d+)\.(\d+)\.(\d+)(?:\s.*)?", line)
    if result.returncode or not match or int(match[1]) < 14:
        raise ValueError("The build requires a working, standalone ripgrep >=14")
    return line


def stage(binary: Path, notices: Path, destination: Path):
    binary, notices, destination = binary.resolve(), notices.resolve(), destination.resolve()
    if not binary.is_file() or binary.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("A native ripgrep binary of at most 64MB is required")
    if not notices.is_file() or not 1 <= notices.stat().st_size <= 1024 * 1024:
        raise ValueError("Supply complete third-party notices matching this binary (including enabled dependencies)")
    version = _version(binary)
    destination.mkdir(parents=True, exist_ok=True)
    name = "rg.exe" if os.name == "nt" else "rg"
    target = destination / name
    for path in (target, destination / "ripgrep-notices.txt", destination / "ripgrep-manifest.json"):
        path.resolve().relative_to(destination)
    descriptor, temporary = tempfile.mkstemp(prefix=".rg-", dir=destination)
    os.close(descriptor)
    try:
        shutil.copy2(binary, temporary)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if _version(target) != version:
        raise ValueError("Staged search engine did not retain its version")
    notice_bytes = notices.read_bytes()
    (destination / "ripgrep-notices.txt").write_bytes(notice_bytes)
    with target.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    manifest = {"binary": name, "version": version, "sha256": digest,
                "notices_sha256": hashlib.sha256(notice_bytes).hexdigest()}
    (destination / "ripgrep-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--notices", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(stage(args.binary, args.notices, args.destination)))
