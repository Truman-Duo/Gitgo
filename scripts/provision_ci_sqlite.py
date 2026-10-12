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
import sysconfig
import tarfile
import urllib.request
import zipfile

# Official release archives and SHA3-256 from https://www.sqlite.org/download.html
VERSION = "3.53.4"
CPYTHON_VERSION = "3.14.7"
CPYTHON_SHA256 = "3b48dac8fb59f62eaa67ac83c1eb12bda1b7a08406dd286e252c11a66be27f81"
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


def macos_sqlite_dependency(dependencies: str) -> str | None:
    # Universal2 otool output repeats a dependency for each architecture slice.
    matches = {line.strip().split()[0] for line in dependencies.splitlines()
               if "libsqlite3" in line and "compatibility version" in line}
    if len(matches) > 1:
        raise RuntimeError("Unexpected macOS SQLite dependency layout: " + dependencies)
    return matches.pop() if matches else None


def rebuild_macos_sqlite(target: Path, scratch: Path) -> None:
    """Official macOS Python statically embeds SQLite; replace the native module."""
    if sys.version_info[:3] != tuple(map(int, CPYTHON_VERSION.split('.'))):
        raise RuntimeError("Static SQLite rebuild requires the pinned CPython version")
    url = f"https://www.python.org/ftp/python/{CPYTHON_VERSION}/Python-{CPYTHON_VERSION}.tar.xz"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(40_000_001)
    if len(data) > 40_000_000 or hashlib.sha256(data).hexdigest() != CPYTHON_SHA256:
        raise RuntimeError("CPython source failed its pinned official archive digest")
    prefix = f"Python-{CPYTHON_VERSION}"
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:xz') as archive:
        members = [member for member in archive.getmembers()
                   if member.name.startswith((prefix + '/Modules/_sqlite/', prefix + '/Include/'))]
        archive.extractall(scratch, members=members, filter='data')
    source = scratch / prefix
    compiled = scratch / target.name
    subprocess.run(['cc', '-bundle', '-undefined', 'dynamic_lookup', '-fPIC', '-O2',
        '-DSQLITE_THREADSAFE=1', '-DSQLITE_ENABLE_COLUMN_METADATA', '-DSQLITE_ENABLE_FTS5',
        '-DSQLITE_ENABLE_RTREE', '-DSQLITE_ENABLE_MATH_FUNCTIONS', '-DHAVE_LOAD_EXTENSION=1',
        '-I' + sysconfig.get_path('include'), '-I' + str(source / 'Include'),
        '-I' + str(source / 'Include/internal'), '-I' + str(scratch),
        *[str(path) for path in sorted((source / 'Modules/_sqlite').glob('*.c'))],
        str(scratch / 'sqlite3.c'), '-o', str(compiled), '-lpthread', '-ldl', '-lm'], check=True)
    subprocess.run(['codesign', '--force', '--sign', '-', str(compiled)], check=True)
    # setup-python's framework file is writable but owned by the installer;
    # preserve its metadata instead of attempting owner-only timestamp changes.
    shutil.copyfile(compiled, target)
    print(f'Rebuilt native SQLite module from CPython {CPYTHON_VERSION} SHA-256 {CPYTHON_SHA256}', flush=True)


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
    elif sys.platform in ("linux", "darwin"):
        with download("linux") as archive:
            source = scratch / "sqlite3.c"
            source.write_bytes(archive.read("sqlite-amalgamation-3530400/sqlite3.c"))
            (scratch / "sqlite3.h").write_bytes(archive.read("sqlite-amalgamation-3530400/sqlite3.h"))
        darwin = sys.platform == "darwin"
        library = scratch / ("libsqlite3.dylib" if darwin else "libsqlite3.so.0")
        link_options = (["-dynamiclib", "-Wl,-install_name," + str(library)] if darwin else
                        ["-shared", "-Wl,-soname,libsqlite3.so.0"])
        subprocess.run(["cc", *link_options, "-fPIC", "-O2", "-DSQLITE_THREADSAFE=1",
            "-DSQLITE_ENABLE_COLUMN_METADATA", "-DSQLITE_ENABLE_FTS5", "-DSQLITE_ENABLE_RTREE",
            "-DSQLITE_ENABLE_MATH_FUNCTIONS",
            str(source), "-o", str(library), "-lpthread", "-ldl", "-lm"], check=True)
        spec = importlib.util.find_spec("_sqlite3")
        if spec is None or spec.origin is None:
            raise RuntimeError("setup-python SQLite extension is unavailable")
        target = Path(spec.origin)
        shutil.copy2(target, scratch / target.name)
        # Pin the extension's dependency instead of passing an injectable
        # LD_LIBRARY_PATH through the sandbox environment allowlist.
        if darwin:
            dependencies = subprocess.check_output(["otool", "-L", str(target)], text=True)
            dependency = macos_sqlite_dependency(dependencies)
            if dependency is None:
                rebuild_macos_sqlite(target, scratch)
            else:
                subprocess.run(["install_name_tool", "-change", dependency, str(library), str(target)], check=True)
                subprocess.run(["codesign", "--force", "--sign", "-", str(target)], check=True)
            subprocess.run(["codesign", "--verify", "--strict", str(target)], check=True)
        else:
            previous = subprocess.check_output(["patchelf", "--print-rpath", str(target)], text=True).strip()
            rpath = str(scratch) + (":" + previous if previous else "")
            subprocess.run(["patchelf", "--set-rpath", rpath, str(target)], check=True)
    else:
        raise SystemExit("Unsupported hosted acceptance platform")
    probe = ("import sqlite3;from backend.core.storage.runtime import validate_sqlite_runtime;"
             f"assert sqlite3.sqlite_version == {VERSION!r};"
             "validate_sqlite_runtime();print('Validated SQLite',sqlite3.sqlite_version)")
    subprocess.run([sys.executable, "-B", "-c", probe], cwd=root, check=True)


if __name__ == "__main__":
    main()
