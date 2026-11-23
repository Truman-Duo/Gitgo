"""Adapter fault injection; actual cgroup kernel evidence runs on delegated Linux."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core import native_sandbox
from backend.core import sandbox_linux as linux
from backend.core.sandbox import SandboxDenied, SandboxPolicy, sandbox_popen


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
        library = Library([{'linux_cgroup_version': 3}, *replies])
        monkeypatch.setattr(native_sandbox, 'get_native', lambda: library)
        return library
    yield install
    linux._PENDING_CGROUPS.clear()


def policy():
    return SimpleNamespace(recovery_id='a' * 32, memory_bytes=1024, process_limit=4)


def test_partially_configured_allocation_is_retired_through_its_owned_token(boundary):
    library = boundary(error(token=123), {'complete': True, 'failure': 0})
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
    library = boundary({'token': 123}, {'complete': False}, {'complete': True, 'failure': 0})
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
    library = boundary({'absent': False, 'token': 77}, {'complete': True, 'failure': 0})
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


@pytest.mark.parametrize("failure, detail", [(1, "CPU budget"), (2, "accounting became unavailable"),
                                             (None, "accounting became unavailable")])
def test_output_requires_final_cpu_verdict(boundary, failure, detail):
    from backend.core.sandbox_io import BoundedCommunication

    library = boundary({'token': 123}, {'failure': 0}, {'complete': True, 'failure': failure})
    group = linux.LinuxCgroup(policy())
    proc = linux.LinuxSandboxProcess.__new__(linux.LinuxSandboxProcess)
    proc._gitgo_cgroup = group
    proc.poll = lambda: 0
    group._launcher = proc
    capture = BoundedCommunication(proc)
    capture.started = True
    capture.output[0].extend(b'{"success":true}')

    with pytest.raises(SandboxDenied, match=detail) as denied:
        capture.communicate(timeout=1)
    assert denied.value.effect_state == 'ambiguous'
    assert denied.value.cleanup['complete'] is True
    assert [call['op'] for call in library.calls[-2:]] == ['linux_cgroup_query', 'linux_cgroup_retire']


@pytest.mark.parametrize("wait_confirms_exit", [True, False])
def test_cgroup_kill_failure_cannot_skip_launcher_wait(boundary, monkeypatch, wait_confirms_exit):
    from backend.core.process_control import terminate_tree

    library = boundary({'token': 123}, OSError("injected cgroup kill failure"),
                       {'complete': True, 'failure': 0})
    group = linux.LinuxCgroup(policy())
    proc = linux.LinuxSandboxProcess.__new__(linux.LinuxSandboxProcess)
    proc._gitgo_cgroup, proc.args, proc.returncode = group, ['owned-launcher'], None
    group._launcher = proc
    events = []
    monkeypatch.setattr(subprocess.Popen, 'kill', lambda self: events.append('signal'))
    proc.poll = lambda: proc.returncode

    def wait(*, timeout):
        events.append('wait')
        if not wait_confirms_exit:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        proc.returncode = -9
        return proc.returncode

    proc.wait = wait
    terminate_tree(proc)
    assert events == ['signal', 'wait']
    assert proc._gitgo_cleanup['complete'] is wait_confirms_exit
    if not wait_confirms_exit:
        assert proc._gitgo_cleanup['issues'] == ['launcher_exit_unconfirmed']
        assert linux._PENDING_CGROUPS[id(group)] is group
        assert library.calls[-1]['op'] == 'linux_cgroup_kill'
        proc.returncode = -9
        assert group.close()['complete'] is True
    assert library.calls[-1]['op'] == 'linux_cgroup_retire'
    assert not linux._PENDING_CGROUPS


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux launcher capability check')
def test_old_bwrap_refuses_before_allocating_group(tmp_path_factory, monkeypatch):
    helper = Path('/usr/bin/bwrap')
    monkeypatch.setattr(linux, 'trusted_system_file', lambda *args, **kwargs: helper)
    calls = []

    def probe(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='--bind --ro-bind', stderr='')

    monkeypatch.setattr(linux.subprocess, 'run', probe)
    monkeypatch.setattr(linux, 'LinuxCgroup', lambda *_: pytest.fail("cgroup allocated before capability check"))
    with pytest.raises(SandboxDenied, match=r'0\.11\.1.*--bind-fd') as denied:
        sandbox_popen([sys.executable], SandboxPolicy(tmp_path_factory), stdin=subprocess.PIPE,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert denied.value.code == 'SANDBOX_UNAVAILABLE'
    assert calls == [[str(helper), '--help']]


@pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux descriptor mount owner')
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
