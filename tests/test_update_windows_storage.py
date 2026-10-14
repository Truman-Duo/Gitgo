"""Actual NTFS/ACL/LockFileEx tests; never reported passed on non-Windows."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.core.update import windows_storage as win
from backend.core.update.validation import UpdateError

pytestmark = pytest.mark.skipif(not win.supported(), reason="Requires actual Windows local NTFS")


@pytest.fixture
def private(tmp_path_factory):
    root = tmp_path_factory.resolve() / "private"
    win.create_private_directory(root)
    yield root


def test_private_root_and_file_use_same_stable_volume(private):
    path = private / "control.json"
    fd = win.open_file(path, create=True, write=True)
    try:
        os.write(fd, b"{}"); os.fsync(fd)
        identity = win.descriptor_identity(fd)
        assert identity == win.filesystem_identity(path)
        assert identity["device"] == win.filesystem_identity(private)["device"]
        assert identity["inode"] > 0
    finally: os.close(fd)
    try: win.validate_staging(private)
    except UpdateError:
        for index, path in enumerate((private, *private.parents)):
            result = subprocess.run(["pwsh.exe", "-NoProfile", "-Command",
                "(Get-Acl -LiteralPath '" + str(path).replace("'", "''") + "').Sddl"], capture_output=True, text=True)
            print("Ancestor", index, path.name, result.stdout, result.stderr)
        raise


def test_multiple_shared_locks_block_exclusive_in_another_process(private):
    path = private / "runtime.lock"
    os.close(win.open_file(path, create=True, write=True))
    identity = win.filesystem_identity(path)
    child = """import sys
from pathlib import Path
from backend.core.update.windows_storage import lock_file
from backend.core.update.validation import UpdateError
try:
 with lock_file(Path(sys.argv[1])): print('unlocked')
except UpdateError as error: print(error.code)
"""
    with win.lock_file(path, shared=True, expected_identity=identity):
        with win.lock_file(path, shared=True, expected_identity=identity):
            result = subprocess.run([sys.executable, "-B", "-c", child, str(path)],
                capture_output=True, text=True, timeout=20)
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == "UPDATE_BUSY"
    with win.lock_file(path, expected_identity=identity): pass


def test_lock_cannot_be_replaced_while_open_and_new_inode_is_rejected(private):
    path = private / "runtime.lock"
    os.close(win.open_file(path, create=True, write=True))
    identity = win.filesystem_identity(path)
    replacement = private / "replacement"
    os.close(win.open_file(replacement, create=True, write=True))
    with win.lock_file(path, expected_identity=identity):
        with pytest.raises(OSError): win.move(replacement, path, replace=True)
    win.move(replacement, path, replace=True)
    with pytest.raises(UpdateError, match="UPDATE_LOCK_INVALID"):
        with win.lock_file(path, expected_identity=identity): pass


def test_dacl_grant_to_everyone_is_not_a_private_installation(private):
    result = subprocess.run(["icacls", str(private), "/grant", "*S-1-1-0:(OI)(CI)F"],
        capture_output=True, timeout=20)
    assert result.returncode == 0
    with pytest.raises(UpdateError, match="UPDATE_ACL_INVALID"): win.filesystem_identity(private)


def test_junction_is_rejected_even_when_destination_is_owned(private):
    target = private / "target"; win.create_private_directory(target)
    link = private / "junction"
    result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, timeout=20)
    assert result.returncode == 0
    with pytest.raises(UpdateError, match="UPDATE_REPARSE_INVALID"): win.filesystem_identity(link)
    assert target.is_dir()


def test_write_through_move_and_existing_destination_rejection(private):
    source, destination = private / "one", private / "two"
    for path in (source, destination):
        fd = win.open_file(path, create=True, write=True)
        try: os.write(fd, path.name.encode()); os.fsync(fd)
        finally: os.close(fd)
    with pytest.raises(OSError): win.move(source, destination)
    assert source.read_bytes() == b"one" and destination.read_bytes() == b"two"
    win.move(source, destination, replace=True)
    assert not source.exists() and destination.read_bytes() == b"one"
