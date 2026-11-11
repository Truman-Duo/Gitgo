"""Real Darwin kernel containment; never a Windows dispatch emulation."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

from backend.core.sandbox_read import ReadRoot, ReadBoundaryError

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Real Darwin kernel required')


def test_system_temp_alias_binds_root_without_allowing_project_links():
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw)
        (path / 'target').write_bytes(b'system-temp')
        (path / 'project-link').symlink_to(path, target_is_directory=True)
        with ReadRoot(path) as acquired, acquired.open('target') as opened:
            assert opened.read_bytes(max_bytes=64) == b'system-temp'
        with pytest.raises(OSError):
            ReadRoot(path / 'project-link')


# Flag-ignore and the metadata/readable-open replacement fault are injected
# inside Rust (safe_read_unix::tests), where these kernel calls now execute.
# Python monkeypatches of os.open cannot exercise the production Rust path.


def test_fifo_symlink_and_device_are_never_returned_as_readable(tmp_path_factory):
    root = tmp_path_factory
    os.mkfifo(root / 'pipe')
    (root / 'target').write_bytes(b'private')
    (root / 'link').symlink_to(root / 'target')
    (root / 'external-device').symlink_to('/dev/null')
    with ReadRoot(root) as acquired:
        for name in ['pipe', 'link', 'external-device']:
            with pytest.raises(OSError):
                acquired.open(name)
