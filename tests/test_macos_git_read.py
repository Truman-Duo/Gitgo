"""Real native captures; Git fixtures do not claim UID/Seatbelt containment."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


if sys.platform != 'darwin':
    pytest.skip('Darwin protected Git boundary', allow_module_level=True)

from backend.core import macos_git_read as git
from backend.core.loop.git_execution import GitBoundaryError, protected_git
from backend.core.sandbox_read import ReadRoot, ReadBoundaryError

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS object-view prototype')


@pytest.fixture
def repository(tmp_path_factory):
    repo = tmp_path_factory / 'repo'
    repo.mkdir()
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path_factory),
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
    def raw(*args, data=None):
        return subprocess.run(['/usr/bin/git', *args], cwd=repo, env=env, input=data,
            capture_output=True, check=True, timeout=10).stdout.strip().decode()
    raw('init', '-q')
    oid = raw('hash-object', '-w', '--stdin', data=b'object-view-test\n')
    return repo, oid, env, raw


def capture(repo):
    directory = repo / '.git/objects'
    with ReadRoot(directory) as root:
        return git.capture_objects(root, directory)


def test_private_copy_ignores_source_configuration_and_late_replacement(repository, tmp_path_factory):
    repo, oid, env, _ = repository
    marker = tmp_path_factory / 'executed'
    (repo / '.git/config').write_text('[core]\nfsmonitor = touch ' + str(marker) + '\n'
        '[include]\npath = /private/etc/passwd\n[filter "hostile"]\nprocess = touch ' + str(marker) + '\n')
    (repo / '.git/hooks/post-checkout').write_text('#!/bin/sh\ntouch ' + str(marker))
    view = capture(repo)
    assert all(name.startswith(oid[:2] + '/') for name, _ in view.files)
    destination = tmp_path_factory / 'view'
    view.materialize(destination)
    (repo / '.git').rename(repo / 'old-git')
    (repo / '.git').mkdir()
    for query, expected in [(['cat-file', '-t', oid], 'blob'), (['cat-file', '-s', oid], '17')]:
        result = subprocess.run(git.read_command(destination, query), env=env,
            cwd=destination, capture_output=True, text=True, check=True, timeout=10)
        assert result.stdout.strip() == expected
    assert not marker.exists()
    assert not (destination / 'hooks').exists()
    assert b'include' not in (destination / 'config').read_bytes()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo'])
def test_native_reader_rejects_nonexclusive_objects(repository, tmp_path_factory, kind):
    repo, oid, _, _ = repository
    target = repo / '.git/objects' / oid[:2] / oid[2:]
    outside = tmp_path_factory / 'outside'
    outside.write_bytes(target.read_bytes())
    target.unlink()
    if kind == 'symlink': target.symlink_to(outside)
    elif kind == 'hardlink': os.link(outside, target)
    else: os.mkfifo(target)
    with pytest.raises((ReadBoundaryError, OSError)):
        capture(repo)


def test_replaced_enumeration_root_is_not_authorized(repository):
    repo, _, _, _ = repository
    directory = repo / '.git/objects'
    with ReadRoot(directory) as root:
        directory.rename(repo / 'old-objects')
        directory.mkdir()
        with pytest.raises(GitBoundaryError, match='authorized native root differ'):
            git.capture_objects(root, directory)


@pytest.mark.parametrize('name', ['info/alternates', 'info/http-alternates',
    'pack/pack-' + 'a' * 40 + '.promisor', 'aa/' + 'b' * 62])
def test_unsupported_external_and_sha256_stores_refuse(repository, name):
    repo, _, _, _ = repository
    target = repo / '.git/objects' / name
    target.parent.mkdir(exist_ok=True)
    target.write_text('/outside\n')
    with pytest.raises(GitBoundaryError): capture(repo)


def test_file_byte_and_entry_budgets(repository, monkeypatch):
    repo, _, _, _ = repository
    for setting, value in [('MAX_FILES', 0), ('MAX_BYTES', 1), ('MAX_ENTRIES', 0)]:
        with monkeypatch.context() as patch:
            patch.setattr(git, setting, value)
            with pytest.raises((GitBoundaryError, ReadBoundaryError)):
                capture(repo)


def test_view_digest_and_existing_target(repository, tmp_path_factory):
    repo, _, _, raw = repository
    first = capture(repo)
    assert capture(repo).sha256 == first.sha256
    raw('hash-object', '-w', '--stdin', data=b'another object')
    assert capture(repo).sha256 != first.sha256
    destination = tmp_path_factory / 'existing'
    destination.mkdir()
    with pytest.raises(FileExistsError): first.materialize(destination)


def test_packed_object_reads(repository, tmp_path_factory):
    repo, oid, env, raw = repository
    raw('pack-objects', str(repo / '.git/objects/pack/pack'), data=(oid + '\n').encode())
    (repo / '.git/objects' / oid[:2] / oid[2:]).unlink()
    destination = tmp_path_factory / 'packed'
    view = capture(repo)
    assert any(name.endswith('.pack') for name, _ in view.files)
    view.materialize(destination)
    result = subprocess.run(git.read_command(destination, ['cat-file', '-t', oid]),
        env=env, capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == 'blob'


def test_private_view_is_readable_under_restrictive_installer_umask(repository, tmp_path_factory):
    repo, _, _, _ = repository
    destination = tmp_path_factory / 'view'
    previous = os.umask(0o077)
    try: capture(repo).materialize(destination)
    finally: os.umask(previous)
    for path in [destination, *destination.rglob('*')]:
        assert path.stat().st_mode & 0o007 == (0o005 if path.is_dir() else 0o004)


def test_exact_commit_merge_base(repository, tmp_path_factory):
    repo, _, env, raw = repository
    raw('config', 'user.name', 'Fixture'); raw('config', 'user.email', 'fixture@gitgo.invalid')
    tree = raw('mktree', data=b'')
    first = raw('commit-tree', tree, '-m', 'first')
    second = raw('commit-tree', tree, '-p', first, '-m', 'second')
    destination = tmp_path_factory / 'history'
    capture(repo).materialize(destination)
    result = subprocess.run(git.read_command(destination, ['merge-base', first, second]),
        env=env, capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == first
    for left,right,code in [(first,second,0),(second,first,1)]:
        query=['merge-base','--is-ancestor',left,right]
        answer=subprocess.run(git.read_command(destination,query),env=env,capture_output=True,text=True,timeout=10)
        assert answer.returncode==code and git.validate_output(query,answer.stdout,answer.returncode)==''


@pytest.mark.parametrize('code,output',[(True,''),(False,''),(2,''),(1,'false\n'),(0,'true\n')])
def test_ancestry_failure_or_text_cannot_masquerade_as_boolean_answer(code,output):
    with pytest.raises(GitBoundaryError):git.validate_output(['merge-base','--is-ancestor','a'*40,'b'*40],output,code)


@pytest.mark.parametrize('query', [
    ['status'], ['cat-file', '-t', 'HEAD'], ['cat-file', '--batch', 'a' * 40],
    ['cat-file', '-p', 'a' * 40], ['cat-file', '-t', 'a' * 64],
    ['merge-base', 'HEAD', 'a' * 40], ['merge-base', '--all', 'a' * 40],
    ['cat-file', '-t', 'a' * 40, '--output=/outside'], ['push', 'origin'],
    ['-c', 'core.fsmonitor=sh'], ['cat-file', '-t', 1],
])
def test_query_cannot_expand_authority(query):
    with pytest.raises(GitBoundaryError): git.read_command(Path('/private/tmp/view'), query)


@pytest.mark.parametrize('returncode,cleanup', [(0, {}), (0, {'complete': False}), (1, {'complete': True})])
def test_output_waits_for_success_and_retirement(returncode, cleanup):
    with pytest.raises(GitBoundaryError):
        git.accepted_output(SimpleNamespace(returncode=returncode, _gitgo_cleanup=cleanup), 'blob\n')


def test_success_is_not_a_production_capability(repository):
    repo, oid, _, _ = repository
    assert git.accepted_output(SimpleNamespace(returncode=0, _gitgo_cleanup={'complete': True}), 'blob\n') == 'blob\n'
    with pytest.raises(GitBoundaryError, match='unavailable'):
        with protected_git(repo, ['git', 'cat-file', '-t', oid]): pass
