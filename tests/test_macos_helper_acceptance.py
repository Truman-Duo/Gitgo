"""The failed-start fixture must itself be installable on a real Mac."""
import hashlib
from pathlib import Path
import subprocess
import sys
import pytest
if sys.platform!='darwin': pytest.skip('Darwin signed system fixture',allow_module_level=True)
from scripts import check_macos_helper_lifecycle as harness
from backend.core.macos_helper_install import inventory


def test_inert_candidate_is_installable_without_system_flags(tmp_path_factory):
    destination=tmp_path_factory/'broken'
    original=hashlib.sha256(Path('/usr/bin/false').read_bytes()).digest()
    harness.prepare_inert_candidate(destination)
    executable=destination/'gitgo-host'
    assert hashlib.sha256(Path('/usr/bin/false').read_bytes()).digest()==original
    assert executable.stat().st_flags==0
    assert executable.stat().st_mode & 0o777 == 0o755
    subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(executable)],check=True,capture_output=True,timeout=10)
    result=subprocess.run([str(executable)],capture_output=True,timeout=10)
    assert result.returncode==1
    assert inventory(destination)
