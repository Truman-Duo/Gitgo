"""Adapter fault injection; actual cgroup kernel evidence runs on delegated Linux."""
from types import SimpleNamespace

import pytest

from backend.core import native_sandbox
from backend.core import sandbox_linux as linux
from backend.core.sandbox import SandboxDenied


class Library:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def call(self, **request):
        self.calls.append(request)
        result = self.replies.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def error(*, unknown=False, **response):
    result = native_sandbox.NativeBoundaryError('Injected native failure')
    result.outcome_unknown, result.response = unknown, response
    return result


@pytest.fixture
def boundary(monkeypatch, tmp_path_factory):
    assert not linux._PENDING_CGROUPS
    monkeypatch.setenv('GITGO_SANDBOX_CGROUP_ROOT', str(tmp_path_factory))
    def install(*replies):
        library = Library([{'linux_cgroup_version': 2}, *replies])
        monkeypatch.setattr(native_sandbox, 'get_native', lambda: library)
        return library
    yield install
    linux._PENDING_CGROUPS.clear()


def policy():
    return SimpleNamespace(recovery_id='a' * 32, memory_bytes=1024, process_limit=4)


def test_partially_configured_allocation_is_retired_through_its_owned_token(boundary):
    library = boundary(error(token=123), {'complete': True})
    with pytest.raises(SandboxDenied) as denied:
        linux.LinuxCgroup(policy())
    assert denied.value.cleanup['complete'] is True
    assert library.calls[-1] == {'op': 'linux_cgroup_retire', 'token': 123}
    assert not linux._PENDING_CGROUPS


def test_unknown_allocation_is_never_reported_clean_or_replayed(boundary):
    library = boundary(error(unknown=True))
    with pytest.raises(SandboxDenied) as denied:
        linux.LinuxCgroup(policy())
    assert denied.value.cleanup['complete'] is False
    assert len(library.calls) == 2
    group, = linux._PENDING_CGROUPS.values()
    assert not group.close()['complete'] and len(library.calls) == 2
    with pytest.raises(SandboxDenied) as pending:
        linux.LinuxCgroup(policy())
    assert pending.value.code == 'SANDBOX_CLEANUP_PENDING'


def test_native_still_active_keeps_owner_until_explicit_confirmed_retirement(boundary):
    library = boundary({'token': 123}, {'complete': False}, {'complete': True})
    group = linux.LinuxCgroup(policy())
    assert not group.close()['complete']
    assert linux._PENDING_CGROUPS[id(group)] is group
    assert group.close()['complete']
    calls = len(library.calls)
    assert group.close()['complete'] and len(library.calls) == calls
    assert not linux._PENDING_CGROUPS


def test_unknown_retirement_cannot_become_success_on_retry(boundary):
    library = boundary({'token': 123}, error(unknown=True))
    group = linux.LinuxCgroup(policy())
    assert not group.close()['complete']
    assert not group.close()['complete']
    assert len(library.calls) == 3
    assert linux._PENDING_CGROUPS[id(group)] is group


def test_cold_recovery_reuses_the_exact_native_resource_token(boundary, tmp_path_factory):
    library = boundary({'absent': False, 'token': 77}, {'complete': True})
    assert linux.recover_cgroup(tmp_path_factory, 'b' * 32)
    assert library.calls[1] == {'op': 'linux_cgroup_recover',
        'root': list(bytes(tmp_path_factory)), 'recovery_id': 'b' * 32}
    assert library.calls[-1] == {'op': 'linux_cgroup_retire', 'token': 77}
    assert not linux._PENDING_CGROUPS


def test_interrupted_allocation_preserves_unknown_resource_owner(boundary):
    library = boundary(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt) as interrupted:
        linux.LinuxCgroup(policy())
    assert interrupted.value.cleanup['complete'] is False
    group, = linux._PENDING_CGROUPS.values()
    assert not group.close()['complete'] and len(library.calls) == 2


@pytest.mark.skipif(__import__('sys').platform != 'linux', reason='Actual Linux descriptor mount owner')
def test_native_git_directory_descriptor_tracks_the_pinned_object(tmp_path_factory):
    import fcntl
    import os
    from backend.core.sandbox_read import ReadRoot, ReadBoundaryError
    root_path = tmp_path_factory / 'git-metadata'
    root_path.mkdir()
    (root_path / 'config').write_text('fixture')
    with ReadRoot(root_path) as root:
        fd = root._linux_directory_descriptor()
        original = os.fstat(fd)
        assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_PATH
        with pytest.raises(OSError):
            os.read(fd, 1)
        with root.open('config') as file:
            with pytest.raises(native_sandbox.NativeBoundaryError):
                file._library.call(op='linux_directory_descriptor', token=file._token)
        moved = root_path.with_name('moved-metadata')
        root_path.rename(moved)
        root_path.mkdir()
        assert os.fstat(fd).st_ino == original.st_ino == moved.stat().st_ino
        assert os.fstat(fd).st_ino != root_path.stat().st_ino
    with pytest.raises(ReadBoundaryError):
        root._linux_directory_descriptor()
