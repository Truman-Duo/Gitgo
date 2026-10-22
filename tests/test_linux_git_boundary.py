"""Real Linux config snapshots and mount identities; no advisory-lock fiction."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.core.loop.git_execution import GitBoundaryError, protected_git, protected_git_config

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux mount namespaces required')


def execute(command, cwd, env, *, check=True):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
        pass_fds=getattr(command, 'pass_fds', ()), timeout=20)
    if check:
        assert result.returncode == 0, result.stderr.decode(errors='replace')
    return result


@pytest.fixture
def repository(tmp_path_factory, monkeypatch):
    base = tmp_path_factory
    home = base / 'home'; home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / 'xdg'))
    for key in list(os.environ):
        if key.startswith(('GIT_', 'GCM_')):
            monkeypatch.delenv(key)
    repo = base / 'repo'; repo.mkdir()
    def raw(*args):
        return execute(['/usr/bin/git', *args], repo,
            dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1')).stdout.strip()
    raw('init'); raw('config', 'user.name', 'Fixture'); raw('config', 'user.email', 'fixture@gitgo.invalid')
    (repo / 'file').write_text('base\n')
    raw('add', 'file'); raw('commit', '-m', 'base')
    return repo, base, raw


def test_read_write_and_worktree_materialization(repository):
    repo, base, raw = repository
    (repo / 'file').write_text('changed\n')
    for args in [['git', 'add', 'file'], ['git', 'commit', '-m', 'changed']]:
        with protected_git(repo, args) as (command, env):
            execute(command, repo, env)
    target = base / 'worktrees' / 'child'; target.parent.mkdir()
    with protected_git(repo, ['git', 'worktree', 'add', '--detach', str(target), 'HEAD']) as (command, env):
        execute(command, repo, env)
    assert (target / 'file').read_text() == 'changed\n'
    with protected_git(repo, ['git', 'worktree', 'remove', '--force', str(target)]) as (command, env):
        execute(command, repo, env)
    assert not target.exists()


def test_late_config_and_include_replacements_do_not_enter_git(repository):
    repo, base, raw = repository
    include = base / 'included'; include.write_text('[core]\nfilemode = false\n')
    raw('config', 'include.path', str(include))
    marker = base / 'executed'
    with protected_git(repo, ['git', 'status', '--porcelain']) as (command, env):
        (repo / '.git/config').write_text('[core]\nfsmonitor = touch ' + str(marker) + '\n')
        include.write_text('[core]\nfsmonitor = touch ' + str(marker) + '\n')
        result = execute(command, repo, env)
        assert not result.stdout.strip() and not marker.exists()


def test_pruning_does_not_forget_other_live_worktrees_outside_current_view(repository):
    repo, base, raw = repository
    paths = [base / 'first' / 'child', base / 'second' / 'child']
    for target in paths:
        target.parent.mkdir()
        with protected_git(repo, ['git', 'worktree', 'add', '--detach', str(target), 'HEAD']) as (command, env):
            execute(command, repo, env)
    with protected_git(repo, ['git', 'worktree', 'remove', '--force', str(paths[0])]) as (command, env):
        execute(command, repo, env)
    with protected_git(repo, ['git', 'worktree', 'prune']) as (command, env):
        execute(command, repo, env)
    assert os.fsencode(paths[1]) in raw('worktree', 'list', '--porcelain')
    with protected_git(repo, ['git', 'worktree', 'remove', '--force', str(paths[1])]) as (command, env):
        execute(command, repo, env)


def test_metadata_directory_replacement_cannot_change_selected_repository(repository):
    repo, base, raw = repository
    head = raw('rev-parse', 'HEAD')
    with protected_git(repo, ['git', 'rev-parse', 'HEAD']) as (command, env):
        (repo / '.git').rename(repo / 'old-metadata')
        (repo / '.git').mkdir()
        (repo / '.git/config').write_text('[core]\nbare = true\n')
        assert execute(command, repo, env).stdout.strip() == head


def test_active_filter_refuses_instead_of_staging_unfiltered_bytes(repository):
    repo, base, raw = repository
    marker = base / 'executed'
    raw('config', 'filter.hostile.clean', 'touch ' + str(marker))
    (repo / '.gitattributes').write_text('file filter=hostile\n')
    with protected_git(repo, ['git', 'add', 'file']) as (command, env):
        result = execute(command, repo, env, check=False)
    assert result.returncode != 0 and b'filter' in result.stderr
    assert not marker.exists()


def test_narrow_config_builtin_remains_available(repository):
    repo, base, raw = repository
    with protected_git_config(repo, ['git', 'remote', 'add', 'test', str(base / 'remote.git')]) as (command, env):
        execute(command, repo, env)
    assert raw('remote', 'get-url', 'test') == os.fsencode(base / 'remote.git')


def test_namespace_retains_no_host_fds_and_restricts_writes(repository):
    from contextlib import ExitStack
    from backend.core.loop.git_linux import LinuxPins
    repo, base, raw = repository
    scratch = base / 'scratch'; scratch.mkdir()
    private = base / 'outside'; private.write_text('unchanged')
    with ExitStack() as owners:
        pins = LinuxPins(owners, scratch)
        # An outside host directory is visible read-only, below which only
        # the selected repository gets a writable mount. /tmp itself is an
        # independent writable tmpfs, not access to the Host's temp files.
        pins.directory(base)
        pins.file(repo / '.git/config')
        script = ("import os;from pathlib import Path;"
                  f"Path({str(repo / 'allowed')!r}).write_text('ok');"
                  f"exec(\"try: Path({str(private)!r}).write_text('bad')\\nexcept OSError: pass\\nelse: raise AssertionError('outside write')\");"
                  f"exec(\"for fd in {list(pins.files.values())!r}:\\n try: os.fstat(fd)\\n except OSError: pass\\n else: raise AssertionError('Host fd leaked')\")")
        command = pins.wrap([sys.executable, '-I', '-c', script], cwd=repo, writable=[repo])
        execute(command, repo, dict(os.environ))
    assert private.read_text() == 'unchanged' and (repo / 'allowed').read_text() == 'ok'


def test_old_helper_refuses_without_path_mount_fallback(repository, monkeypatch):
    from contextlib import ExitStack
    from backend.core.loop import git_linux
    repo, base, raw = repository
    scratch = base / 'scratch'; scratch.mkdir()
    monkeypatch.setattr(git_linux.subprocess, 'run',
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout='--ro-bind --bind', stderr=''))
    with ExitStack() as owners, pytest.raises(GitBoundaryError, match='0.11.1'):
        git_linux.LinuxPins(owners, scratch).wrap(['/usr/bin/git', 'status'], cwd=repo, writable=[repo])


def test_local_transfer_and_late_receiver_hook(repository):
    from backend.adapters.local_git_runner import LocalGitRunner
    repo, base, raw = repository
    remote = base / 'remote.git'
    execute(['/usr/bin/git', 'init', '--bare', str(remote)], base, dict(os.environ))
    runner = LocalGitRunner(repo)
    result = runner.run(['remote', 'add', 'origin', str(remote)])
    assert result.returncode == 0, result.stderr
    raw('config', 'push.default', 'current')
    with protected_git(repo, ['git', 'push', 'origin']) as (command, env):
        marker = repo / 'injected'
        hook = remote / 'hooks/pre-receive'
        hook.write_text('#!/bin/sh\ntouch ' + str(marker) + '\n')
        hook.chmod(0o755)
        execute(command, repo, env)
    assert not marker.exists()
    result = runner.run(['fetch', 'origin'])
    assert result.returncode == 0, result.stderr
    remote_head = execute(['/usr/bin/git', 'rev-parse', 'HEAD'], remote, dict(os.environ)).stdout.strip()
    assert remote_head == raw('rev-parse', 'HEAD')


def test_worktree_configuration_is_captured_before_mutable_boolean_lookup(repository, monkeypatch):
    from backend.core.loop import git_execution
    repo, base, raw = repository
    raw('config', 'extensions.worktreeConfig', 'true')
    config = repo / '.git/config.worktree'
    config.write_text('[filter "late"]\nclean = false\n')
    invoke = git_execution._invoke
    def refuse_mutable_extension_read(command, **kwargs):
        assert not ('--type=bool' in command), 'Extension choice must use captured graph'
        return invoke(command, **kwargs)
    monkeypatch.setattr(git_execution, '_invoke', refuse_mutable_extension_read)
    with protected_git(repo, ['git', 'status', '--porcelain']) as (command, env):
        config.write_text('[core]\nfsmonitor = false\n')
        execute(command, repo, env)
    config.unlink()
    with pytest.raises(GitBoundaryError, match='missing names'):
        with protected_git(repo, ['git', 'status']):
            pytest.fail('Missing worktree configuration entered execution')
