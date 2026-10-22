"""Real Darwin kernel containment; never a Windows dispatch emulation."""
import os
import sys

import pytest

from backend.core.sandbox_read import ReadRoot, ReadBoundaryError
from backend.core import sandbox_read_darwin as native

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Real Darwin kernel required')


def test_beneath_flag_is_not_silently_ignored(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(native, 'O_RESOLVE_BENEATH', 0)
    with pytest.raises(ReadBoundaryError, match='does not enforce beneath'):
        ReadRoot(tmp_path_factory)


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


def test_replacement_between_metadata_and_readable_open_is_refused(tmp_path_factory, monkeypatch):
    root = tmp_path_factory
    (root / 'target').write_bytes(b'selected')
    (root / 'replacement').write_bytes(b'attacker')
    original = os.open
    with ReadRoot(root) as acquired:
        def race(path, flags, *args, **kwargs):
            if path == 'target' and not flags & native.O_EVTONLY:
                os.replace(root / 'replacement', root / 'target')
            return original(path, flags, *args, **kwargs)
        monkeypatch.setattr(native.os, 'open', race)
        monkeypatch.setattr(native.os, 'supports_dir_fd', {*os.supports_dir_fd, race})
        with pytest.raises(ReadBoundaryError, match='changed'):
            acquired.open('target')
