"""Build a pinned, WAL-safe SQLite shared library for Linux release Python."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid


SQLITE_VERSION = "3.53.4"
SQLITE_URL = "https://www.sqlite.org/2026/sqlite-autoconf-3530400.tar.gz"
SQLITE_SHA3_256 = "454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338"
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024


def prepare(prefix: Path) -> dict[str, str]:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("The pinned SQLite release runtime must be built on Linux")
    prefix = prefix.expanduser().resolve()
    if prefix.exists():
        raise FileExistsError(f"Refusing to overwrite SQLite prefix: {prefix}")
    if shutil.which("make") is None:
        raise RuntimeError("make is required to build the pinned Linux SQLite runtime")

    staging = prefix.with_name(prefix.name + ".staging-" + uuid.uuid4().hex)
    with tempfile.TemporaryDirectory(prefix="gitgo-sqlite-source-") as raw:
        source_root = Path(raw)
        archive = source_root / "sqlite.tar.gz"
        with urllib.request.urlopen(SQLITE_URL, timeout=60) as response:
            payload = response.read(MAX_ARCHIVE_BYTES + 1)
        if len(payload) > MAX_ARCHIVE_BYTES:
            raise RuntimeError("SQLite source archive exceeded the size limit")
        digest = hashlib.sha3_256(payload).hexdigest()
        if digest != SQLITE_SHA3_256:
            raise RuntimeError(f"SQLite source SHA3-256 mismatch: {digest}")
        archive.write_bytes(payload)
        extracted = source_root / "source"
        extracted.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(extracted, filter="data")
        candidates = [path for path in extracted.iterdir() if path.is_dir()]
        if len(candidates) != 1 or not (candidates[0] / "configure").is_file():
            raise RuntimeError("SQLite source archive has an unexpected layout")
        source = candidates[0]
        subprocess.run(
            [str(source / "configure"), f"--prefix={staging}", "--disable-static", "--enable-shared"],
            cwd=source,
            check=True,
        )
        subprocess.run(["make", "-j2"], cwd=source, check=True)
        subprocess.run(["make", "install"], cwd=source, check=True)

    version = subprocess.check_output(
        [str(staging / "bin" / "sqlite3"), "--version"], text=True, timeout=15,
    ).split()[0]
    if version != SQLITE_VERSION:
        raise RuntimeError(f"Built SQLite {version}, expected {SQLITE_VERSION}")
    staging.rename(prefix)
    return {
        "version": version,
        "prefix": str(prefix),
        "library_path": str(prefix / "lib"),
        "source_url": SQLITE_URL,
        "source_sha3_256": SQLITE_SHA3_256,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(prepare(arguments.prefix), indent=2))
