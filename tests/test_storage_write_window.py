"""Isolate finite-lifetime native VFS registrations for each fault case."""
from pathlib import Path
import subprocess,sys
import pytest
from tests.test_storage_windows_sqlite import binaries

@pytest.mark.parametrize('case',['none','limit','before','after','extent'])
def test_native_write_window_failure_retains_debt(binaries,case):
    root=Path(__file__).resolve().parents[1]
    node='tests/fixtures/storage_write_window_cases.py::test_native_write_range_is_consumed_before_effects_and_never_refunded['+case+']'
    done=subprocess.run([sys.executable,'-B','-m','pytest',node,'-p','no:cacheprovider','-q','--tb=short'],
                        cwd=root,capture_output=True,text=True,timeout=45)
    assert done.returncode==0,done.stdout+done.stderr
    assert '1 passed' in done.stdout
