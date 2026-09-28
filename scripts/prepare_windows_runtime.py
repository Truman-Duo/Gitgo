"""Build an isolated Windows embedded Python with a verified SQLite WAL fix.

Never patches a shared/system Python. Requires an existing embedded Python and
matching dependency directory; fails instead of overwriting an installation.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import uuid
import zipfile


SQLITE_URL = "https://www.sqlite.org/2026/sqlite-dll-win-x64-3530400.zip"
SQLITE_SHA3 = "deddee963c810d1eeac3ce5e15c7c41da21a1c54d7a39cf54fbf577d2f50de3a"


def prepare(source: Path, packages: Path, destination: Path) -> dict:
    if os.name != "nt":
        raise RuntimeError("This installer is for Windows x64 only")
    source, packages, destination = source.resolve(), packages.resolve(), destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite {destination}")
    pth_files = list(source.glob("python*._pth"))
    if len(pth_files) != 1 or not (source / "python.exe").is_file() or not packages.is_dir():
        raise ValueError("An embedded Python and matching packages directory are required")
    architecture = subprocess.check_output(
        [str(source / "python.exe"), "-c", "import struct; print(struct.calcsize('P')*8)"],
        text=True, timeout=15,
    ).strip()
    if architecture != "64":
        raise ValueError("The pinned SQLite library requires x64 Python")
    with urllib.request.urlopen(SQLITE_URL, timeout=60) as response:
        archive = response.read(8 * 1024 * 1024 + 1)
    if hashlib.sha3_256(archive).hexdigest() != SQLITE_SHA3:
        raise ValueError("SQLite archive checksum mismatch; nothing installed")
    staging = destination.with_name(destination.name + ".staging-" + uuid.uuid4().hex)
    # On failure leave the staging directory available for inspection. Do not
    # publish an incomplete runtime or recursively delete caller-provided paths.
    shutil.copytree(source, staging)
    shutil.copytree(packages, staging / "packages", ignore=shutil.ignore_patterns("__pycache__"))
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        (staging / "sqlite3.dll").write_bytes(zipped.read("sqlite3.dll"))
    version_name = pth_files[0].stem
    repository = Path(__file__).resolve().parents[1]
    (staging / pth_files[0].name).write_text(
        f"{version_name}.zip\n.\npackages\n{repository}\nimport site\n", encoding="utf-8",
    )
    version = subprocess.check_output(
        [str(staging / "python.exe"), "-B", "-c",
         "import sqlite3,httpx,pytest,pypdf,docx,pptx,openpyxl,xlrd,yaml,rich,paramiko,watchdog; "
         "assert callable(pytest.main) and callable(httpx.AsyncClient); "
         "assert callable(yaml.safe_load); "
         "from backend.core.storage.runtime import validate_sqlite_runtime; "
         "validate_sqlite_runtime(); print(sqlite3.sqlite_version)"],
        text=True, timeout=30,
    ).strip()
    manifest = {"sqlite_version": version, "sqlite_url": SQLITE_URL,
                "sqlite_archive_sha3_256": SQLITE_SHA3, "source_python": str(source)}
    (staging / "gitgo-runtime.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    staging.rename(destination)
    return {**manifest, "python": str(destination / "python.exe")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("packages", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(prepare(arguments.source, arguments.packages, arguments.destination), indent=2))
