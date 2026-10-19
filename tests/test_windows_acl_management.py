"""Real cross-process coordination of the operator-only ACL path."""
from __future__ import annotations

import subprocess
import sys

import pytest

from backend.core.windows_acl import management_lock, SecurityConflict

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows operator mutex')


def test_operator_lock_serializes_distinct_processes():
    source = (
        'from backend.core.windows_acl import management_lock;'
        'import sys\n'
        'with management_lock():\n'
        " print('locked', flush=True)\n"
        ' sys.stdin.readline()\n'
    )
    with management_lock():
        # No process is allowed to silently skip an occupied management lock.
        result = subprocess.run([sys.executable, '-B', '-c',
            'from backend.core.windows_acl import management_lock,SecurityConflict\n'
            'try:\n'
            ' with management_lock(): raise AssertionError("overlapping operator")\n'
            'except SecurityConflict: print("blocked")'],
            capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'blocked'
    # Closing the first operation releases the same lock for a new process.
    result = subprocess.run([sys.executable, '-B', '-c', source], input='done\n',
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'locked'
