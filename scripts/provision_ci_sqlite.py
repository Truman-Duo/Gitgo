"""Pin a patched SQLite in disposable hosted CI; never relax runtime validation."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

# Official release archives and SHA3-256 from https://www.sqlite.org/download.html
VERSION = "3.53.4"
ARCHIVES = {
    "windows": ("sqlite-dll-win-x64-3530400.zip",
        "deddee963c810d1eeac3ce5e15c7c41da21a1c54d7a39cf54fbf577d2f50de3a"),
    "linux": ("sqlite-amalgamation-3530400.zip",
        "628a44cfe82c66aed1ccbbe85a562d2e33ebe64b3288981ed76285612227934e"),
}


def download(platform):
    name, expected = ARCHIVES[platform]
    with urllib.request.urlopen("https://www.sqlite.org/2026/" + name, timeout=60) as response:
        data = response.read(20_000_001)
    if len(data) > 20_000_000 or hashlib.sha3_256(data).hexdigest() != expected:
        raise RuntimeError("SQLite archive failed its pinned release digest")
    print(f"Verified {name}: SHA3-256 {expected}", flush=True)
    return zipfile.ZipFile(io.BytesIO(data))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    if args.download_only:
        for platform in ARCHIVES:
            with download(platform):
                pass
        return
    if (os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"):
        raise SystemExit("Only disposable GitHub-hosted CI can provision this runtime")
    root = Path(__file__).resolve().parents[1]
    scratch = root / ".gitgo" / "ci-sqlite"
    scratch.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        target = Path(sys.base_prefix) / "DLLs" / "sqlite3.dll"
        if not target.is_file():
            raise RuntimeError("Expected setup-python SQLite DLL is unavailable")
        shutil.copy2(target, scratch / "original-sqlite3.dll")
        # This process has never imported sqlite3, so it holds no DLL lock.
        with download("windows") as archive:
            target.write_bytes(archive.read("sqlite3.dll"))
    elif sys.platform == "linux":
        with download("linux") as archive:
            source = scratch / "sqlite3.c"
            source.write_bytes(archive.read("sqlite-amalgamation-3530400/sqlite3.c"))
        library = scratch / "libsqlite3.so.0"
        subprocess.run(["gcc", "-shared", "-fPIC", "-O2", "-DSQLITE_THREADSAFE=1",
            "-DSQLITE_ENABLE_COLUMN_METADATA", "-DSQLITE_ENABLE_FTS5", "-DSQLITE_ENABLE_RTREE",
            "-DSQLITE_ENABLE_MATH_FUNCTIONS", "-Wl,-soname,libsqlite3.so.0",
            str(source), "-o", str(library), "-lpthread", "-ldl", "-lm"], check=True)
        spec = importlib.util.find_spec("_sqlite3")
        if spec is None or spec.origin is None:
            raise RuntimeError("setup-python SQLite extension is unavailable")
        target = Path(spec.origin)
        shutil.copy2(target, scratch / target.name)
        # Pin the extension's dependency instead of passing an injectable
        # LD_LIBRARY_PATH through the sandbox environment allowlist.
        previous = subprocess.check_output(["patchelf", "--print-rpath", str(target)], text=True).strip()
        rpath = str(scratch) + (":" + previous if previous else "")
        subprocess.run(["patchelf", "--set-rpath", rpath, str(target)], check=True)
    else:
        raise SystemExit("This helper provisions the Windows/Linux acceptance jobs only")
    probe = ("import sqlite3;from backend.core.storage.runtime import validate_sqlite_runtime;"
             f"assert sqlite3.sqlite_version == {VERSION!r};"
             "validate_sqlite_runtime();print('Validated SQLite',sqlite3.sqlite_version)")
    subprocess.run([sys.executable, "-B", "-c", probe], cwd=root, check=True)


if __name__ == "__main__":
    main()
