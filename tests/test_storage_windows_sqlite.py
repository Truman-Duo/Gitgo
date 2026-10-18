"""Native SQLite lifetime, binding, actual WAL and protocol fault boundaries."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def binaries():
    if sys.platform != "win32":
        pytest.skip("Windows native SQLite VFS")
    selected = os.environ.get("GITGO_STORAGE_NATIVE_TEST_LIBRARY")
    if not selected:
        pytest.skip("Explicit native storage build required")
    root = Path(selected).resolve(strict=True).parent
    assert (root/"gitgo-storage-sqlite.dll").is_file(), "Compile against the qualified SQLite header"
    return root


@pytest.mark.parametrize("case", ["roundtrip","wal_ceiling","pinned_reader","outside_attach",
    "thread","mmap_disabled","registration_lifetime","oversized_sidecar","same_bytes_foreign_module", "repository_quarantine", "repository_schemas"])
def test_actual_sqlite_native_profile(binaries,tmp_path_factory,case):
    fixture = Path(__file__).with_name("fixtures")/"storage_sqlite_case.py"
    result = subprocess.run([sys.executable,"-B",str(fixture),
        str(binaries/"gitgo-storage-sqlite.dll"),str(tmp_path_factory),case],
        capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stdout+result.stderr
    assert case+" passed" in result.stdout


@pytest.mark.parametrize("case", ["write","sync","short_read","close","unmap","unregister",
    "retired","thread","null_open","foreign_methods","ceilings","leases"])
def test_native_callback_unknown_outcomes_remain_owned(binaries,tmp_path_factory,case):
    result = subprocess.run([str(binaries/"storage-sqlite-faults.exe"),case,
        str(tmp_path_factory/"state.sqlite3")],capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stdout+result.stderr
    assert "native SQLite protocol fault passed: "+case in result.stdout
