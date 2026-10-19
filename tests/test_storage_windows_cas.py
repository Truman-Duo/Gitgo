"""Native exact-object creation, reads, retirement and unknown-effect faults."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def binaries():
    if sys.platform != "win32": pytest.skip("Windows native CAS")
    selected = os.environ.get("GITGO_STORAGE_NATIVE_TEST_LIBRARY")
    if not selected: pytest.skip("Explicit native storage build required")
    root = Path(selected).resolve(strict=True).parent
    for name in ("cas_writer", "cas_generation"):
        assert (root/f"gitgo-storage-{name}.dll").is_file()
    return root


@pytest.mark.parametrize("case", [*[f"roundtrip_{n}" for n in (0,1,4096,4097,65536)],
    "collision", "wrong_parent", "wrong_identity", "partial_0", "partial_3", "partial_wrong",
    "wrong_content", "pinned_reader", "lifetime"])
def test_actual_native_cas(binaries, tmp_path_factory, case):
    fixture = Path(__file__).with_name("fixtures")/"storage_cas_case.py"
    result = subprocess.run([sys.executable, "-B", str(fixture), str(binaries),
        str(tmp_path_factory), case], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout+result.stderr
    assert case+" passed" in result.stdout


@pytest.mark.parametrize("kind", ["cas_writer", "cas_generation"])
def test_unknown_native_cas_effects_are_not_replayed(binaries, tmp_path_factory, kind):
    result = subprocess.run([str(binaries/("storage-"+kind.replace("_","-")+"-faults.exe")),
        str(tmp_path_factory)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout+result.stderr
    assert "quarantined=1" in result.stdout
