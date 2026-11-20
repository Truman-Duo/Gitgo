"""Real macOS ACL rejection plus isolated native error/ownership tests.

Only disposable user-owned files receive ACLs. No sudo, root policy changes,
installation or interpretation of ls output is involved.
"""
import ctypes
import errno
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from backend.core import macos_file_authority as authority


@pytest.mark.skipif(sys.platform != 'darwin', reason='Real macOS ACL API')
@pytest.mark.parametrize('directory', [False, True])
def test_real_file_and_directory_without_acl_are_accepted(tmp_path_factory, directory):
    path = tmp_path_factory / 'authority'
    path.mkdir() if directory else path.write_text('policy')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        assert authority.require_no_extended_acl(fd) is None
        assert authority._stamp(os.fstat(fd)) == authority._stamp(before)
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Real macOS ACL API')
@pytest.mark.parametrize('directory,entry', [
    (False, 'everyone allow write,append'), (False, 'everyone deny delete'),
    (True, 'everyone allow add_file,add_subdirectory,delete_child'),
    (True, 'everyone deny delete'),
])
def test_real_acl_is_rejected_even_when_mode_bits_are_unchanged(tmp_path_factory, directory, entry):
    path = tmp_path_factory / 'authority'
    path.mkdir() if directory else path.write_text('policy')
    path.chmod(0o755 if directory else 0o644)
    before = path.stat()
    subprocess.run(['/bin/chmod', '+a', entry, str(path)], check=True, capture_output=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        assert path.stat().st_mode == before.st_mode
        with pytest.raises(authority.FileAuthorityError, match='ACL entries'):
            authority.require_no_extended_acl(fd)
        assert os.fstat(fd).st_ino == before.st_ino  # caller still owns the descriptor
    finally:
        os.close(fd)
        subprocess.run(['/bin/chmod', '-N', str(path)], check=True, capture_output=True)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Real macOS inherited ACL')
def test_private_mode_does_not_hide_an_inherited_acl(tmp_path_factory):
    parent = tmp_path_factory / 'parent'
    parent.mkdir()
    subprocess.run(['/bin/chmod', '+a',
                    'everyone allow write,append,file_inherit,directory_inherit', str(parent)],
                   check=True, capture_output=True)
    child = parent / 'private'
    child.mkdir(mode=0o700)
    fd = os.open(child, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert child.stat().st_mode & 0o777 == 0o700
        with pytest.raises(authority.FileAuthorityError, match='ACL entries'):
            authority.require_no_extended_acl(fd)
    finally:
        os.close(fd)
        for path in (child, parent):
            subprocess.run(['/bin/chmod', '-N', str(path)], check=True, capture_output=True)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Real Darwin library')
def test_closed_descriptor_is_not_treated_as_no_acl(tmp_path_factory):
    path = tmp_path_factory / 'closed'
    path.write_text('x')
    fd = os.open(path, os.O_RDONLY)
    os.close(fd)
    with pytest.raises(authority.FileAuthorityError):
        authority.require_no_extended_acl(fd)


class NativeFixture:
    """Mock only the four C functions; Python uses actual file descriptors."""
    def __init__(self, *, pointer=42, get_errno=0, valid=0, entry=-1,
                 entry_errno=errno.EINVAL, entry_pointer=None, free=0, mutate=None):
        self.pointer, self.get_errno, self.valid = pointer, get_errno, valid
        self.entry, self.entry_errno, self.entry_pointer = entry, entry_errno, entry_pointer
        self.free, self.mutate, self.freed = free, mutate, []

    def acl_get_fd_np(self, fd, kind):
        assert kind == 0x100
        os.fstat(fd)
        ctypes.set_errno(self.get_errno)
        if self.mutate: self.mutate()
        return self.pointer

    def acl_valid(self, pointer):
        assert pointer == self.pointer
        return self.valid

    def acl_get_entry(self, pointer, index, entry):
        assert pointer == self.pointer and index == 0
        if self.entry_pointer is not None:
            ctypes.cast(entry, ctypes.POINTER(ctypes.c_void_p))[0] = self.entry_pointer
        ctypes.set_errno(self.entry_errno)
        return self.entry

    def acl_free(self, pointer):
        self.freed.append(pointer)
        return self.free


@pytest.fixture
def native(tmp_path_factory, monkeypatch):
    path = tmp_path_factory / 'policy'
    path.write_text('{}')
    # A test-created regular file; native ACL syscalls are replaced below.
    fd = os.open(path, os.O_RDONLY)
    # Isolate this module's platform dependency; never replace global sys.platform.
    monkeypatch.setattr(authority, 'sys', SimpleNamespace(platform='darwin'))
    try:
        yield path, fd
    finally:
        os.close(fd)


@pytest.mark.parametrize('failure', [0, errno.EACCES, errno.EBADF, errno.EINVAL,
                                     errno.ENOTSUP, errno.ENOMEM, errno.EIO])
def test_native_query_failures_never_mean_acl_absence(native, monkeypatch, failure):
    path, fd = native
    api = NativeFixture(pointer=None, get_errno=failure)
    monkeypatch.setattr(authority, '_acl_functions', lambda: api)
    with pytest.raises(authority.FileAuthorityError, match='could not be inspected'):
        authority.require_no_extended_acl(fd)
    assert api.freed == []
    assert os.fstat(fd).st_ino == path.stat().st_ino


def test_only_darwin_missing_acl_property_counts_as_absent(native, monkeypatch):
    _, fd = native
    api = NativeFixture(pointer=None, get_errno=errno.ENOENT)
    monkeypatch.setattr(authority, '_acl_functions', lambda: api)
    assert authority.require_no_extended_acl(fd) is None
    assert api.freed == []


@pytest.mark.parametrize('changes,accepted', [
    ({}, True),  # valid allocated empty ACL: -1/EINVAL for first entry
    ({'valid': -1}, False),
    ({'entry': 0, 'entry_errno': 0, 'entry_pointer': 99}, False),
    ({'entry': 0, 'entry_errno': 0}, False),
    ({'entry': 1, 'entry_errno': 0}, False),  # must not accept Linux ABI values
    ({'entry_errno': errno.EIO}, False),
    ({'entry_errno': 0}, False),
    ({'entry_pointer': 99}, False),
    ({'free': -1}, False),
])
def test_allocated_acl_is_always_freed_and_ambiguous_results_refused(native, monkeypatch, changes, accepted):
    path, fd = native
    api = NativeFixture(**changes)
    monkeypatch.setattr(authority, '_acl_functions', lambda: api)
    if accepted:
        authority.require_no_extended_acl(fd)
    else:
        with pytest.raises(authority.FileAuthorityError):
            authority.require_no_extended_acl(fd)
    assert api.freed == [42]
    assert os.fstat(fd).st_ino == path.stat().st_ino


def test_object_mutation_during_acl_query_is_refused(native, monkeypatch):
    path, fd = native
    api = NativeFixture(pointer=None, get_errno=errno.ENOENT, mutate=lambda: path.write_text('changed'))
    monkeypatch.setattr(authority, '_acl_functions', lambda: api)
    with pytest.raises(authority.FileAuthorityError, match='changed during'):
        authority.require_no_extended_acl(fd)


@pytest.mark.parametrize('fd', [True, -1, '/root/policy', None])
def test_invalid_descriptor_rejected_before_native_api(monkeypatch, fd):
    monkeypatch.setattr(authority, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(authority, '_acl_functions', lambda: pytest.fail('Native API reached'))
    with pytest.raises(authority.FileAuthorityError): authority.require_no_extended_acl(fd)


def test_other_platform_does_not_silently_accept_acl_authority(monkeypatch):
    monkeypatch.setattr(authority, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(authority, '_acl_functions', lambda: pytest.fail('Native API reached'))
    with pytest.raises(authority.FileAuthorityError): authority.require_no_extended_acl(0)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Real macOS root-policy ACL boundary')
@pytest.mark.parametrize('where', ['leaf', 'parent'])
def test_publisher_policy_loader_rejects_real_acl_on_each_boundary(tmp_path_factory, monkeypatch, where):
    from backend.core import macos_helper_trust as trust
    from tests.test_macos_helper_trust import root_fixture_reader

    path, _ = root_fixture_reader(monkeypatch, tmp_path_factory)
    target = path if where == 'leaf' else path.parent
    entry = 'everyone allow write,append' if where == 'leaf' else 'everyone allow add_file,delete_child'
    mode = target.stat().st_mode
    subprocess.run(['/bin/chmod', '+a', entry, str(target)], check=True, capture_output=True)
    try:
        assert target.stat().st_mode == mode
        with pytest.raises(trust.PublisherTrustError) as failure:
            trust.load_root_policy()
        assert isinstance(failure.value.__cause__, authority.FileAuthorityError)
    finally:
        subprocess.run(['/bin/chmod', '-N', str(target)], check=True, capture_output=True)
