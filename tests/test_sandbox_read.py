"""Real OS object tests; native source/frozen launch is tested separately."""
import hashlib
import os
from pathlib import Path
import sys
import threading

import pytest

from backend.core.sandbox_read import ReadBoundaryError, ReadRoot, relative_parts
from backend.core.tools.workspace_tools import read_file
from backend.core.tools.registrations import file_read

pytestmark = pytest.mark.skipif(sys.platform not in {'linux', 'win32', 'darwin'},
                                reason='No production read backend on this platform')


@pytest.fixture
def tree(tmp_path_factory):
    root = tmp_path_factory / 'read-root'
    root.mkdir()
    (root / 'dir').mkdir()
    (root / 'dir' / 'note.txt').write_bytes(b'first\nsecond\n')
    return root


def test_read_uses_one_object_and_noninheritable_descriptor(tree):
    with ReadRoot(tree) as root, root.open('dir/note.txt') as opened:
        assert root.identity != opened.identity
        assert not os.get_inheritable(opened._fd)
        assert opened.read_bytes() == b'first\nsecond\n'
    with pytest.raises(ValueError):
        opened.read_bytes()
    with pytest.raises(ValueError):
        root.open('dir/note.txt')


@pytest.mark.parametrize('handler', [read_file, file_read])
def test_both_read_handlers_use_selected_workspace(tree, handler, monkeypatch):
    def no_path_read(*a, **kw):
        pytest.fail('Reader reopened the caller path')
    monkeypatch.setattr(Path, 'read_bytes', no_path_read)
    monkeypatch.setattr(Path, 'read_text', no_path_read)
    result = handler({'_workspace': str(tree), 'path': 'dir/note.txt'})
    assert result['content'].startswith('first\nsecond')
    assert result['size'] == 13
    if handler is read_file:
        assert result['sha256'] == hashlib.sha256(b'first\nsecond\n').hexdigest()


@pytest.mark.parametrize('handler', [read_file, file_read])
def test_directory_and_missing_file_are_visible_errors(tree, handler):
    assert handler({'_workspace': str(tree), 'path': 'dir'})['error'] == 'IS_DIRECTORY'
    assert handler({'_workspace': str(tree), 'path': 'missing'})['error'] in {'READ_ERROR', 'FILE_NOT_FOUND'}


@pytest.mark.parametrize('name', ['../private', 'dir/../../private', '/absolute', '', 'bad\x00name'])
def test_relative_names_cannot_escape(tree, name):
    with ReadRoot(tree) as root, pytest.raises(ReadBoundaryError):
        root.open(name)


@pytest.mark.parametrize('handler', [read_file, file_read])
def test_external_exact_file_scope_does_not_allow_sibling(tree, handler):
    external = tree.parent / 'allowed.txt'
    external.write_text('allowed', encoding='utf8')
    arguments = {'_workspace': str(tree), '_allowed_roots': [str(external)]}
    assert handler({**arguments, 'path': str(external)})['content'] == 'allowed'
    if handler is read_file:
        with pytest.raises(PermissionError):
            handler({**arguments, 'path': str(tree.parent / 'other.txt')})
    else:
        blocked = handler({**arguments, 'path': str(tree.parent / 'other.txt')})
        assert blocked['error'] == 'READ_ERROR' and 'content' not in blocked


def test_opened_file_is_not_replaced_before_read(tree):
    target = tree / 'dir' / 'note.txt'
    replacement = tree / 'replacement.txt'
    replacement.write_bytes(b'ATTACKER')
    with ReadRoot(tree) as root, root.open('dir/note.txt') as opened:
        if sys.platform == 'win32':
            with pytest.raises(OSError):
                os.replace(replacement, target)
            assert opened.read_bytes() == b'first\nsecond\n'
        else:
            os.replace(replacement, target)
            # Unlinking the held object is detected. It cannot redirect reading
            # to the replacement, even though the original name now resolves.
            with pytest.raises(ReadBoundaryError):
                opened.read_bytes()
    assert target.read_bytes() in {b'first\nsecond\n', b'ATTACKER'}


def test_parent_move_after_open_never_redirects_read(tree):
    with ReadRoot(tree) as root, root.open('dir/note.txt') as opened:
        if sys.platform == 'win32':
            with pytest.raises(OSError):
                (tree / 'dir').rename(tree / 'moved')
        else:
            (tree / 'dir').rename(tree / 'moved')
            (tree / 'dir').mkdir()
            (tree / 'dir' / 'note.txt').write_bytes(b'ATTACKER')
        assert opened.read_bytes() == b'first\nsecond\n'


def test_acquired_root_is_not_reselected_by_name(tree):
    moved = tree.parent / 'moved-root'
    with ReadRoot(tree) as root:
        if sys.platform == 'win32':
            with pytest.raises(OSError):
                tree.rename(moved)
        else:
            tree.rename(moved)
            (tree / 'dir').mkdir(parents=True)
            (tree / 'dir' / 'note.txt').write_bytes(b'ATTACKER')
        with root.open('dir/note.txt') as opened:
            assert opened.read_bytes() == b'first\nsecond\n'


def test_hard_link_target_is_refused(tree):
    os.link(tree / 'dir' / 'note.txt', tree / 'alias.txt')
    with ReadRoot(tree) as root, pytest.raises(ReadBoundaryError):
        root.open('alias.txt')


def _directory_link(link, target):
    if sys.platform == 'win32':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_linked_parent_and_root_are_refused(tree):
    link = tree / 'linked'
    (tree / 'dir' / 'child').mkdir()
    _directory_link(link, tree / 'dir')
    with ReadRoot(tree) as root, pytest.raises(OSError):
        root.open('linked/note.txt')
    with pytest.raises(OSError):
        ReadRoot(link)
    with pytest.raises(OSError):
        ReadRoot(link / 'child')


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux special files and openat2')
def test_linux_special_files_and_symlinks_refused_without_io(tree):
    os.mkfifo(tree / 'pipe')
    (tree / 'link').symlink_to(tree / 'dir' / 'note.txt')
    (tree / 'proc-link').symlink_to('/proc/self/status')
    with ReadRoot(tree) as root:
        for name in ('pipe', 'link', 'proc-link'):
            with pytest.raises(OSError):
                root.open(name)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows name aliases')
@pytest.mark.parametrize('name', ['NUL', 'COM1.txt', 'a:stream', 'note.txt.', 'dir /x', 'C:note.txt', '\\\\server\\share\\x'])
def test_windows_ambiguous_names_refused(name):
    with pytest.raises(ReadBoundaryError):
        relative_parts(name)


def test_read_limit_and_concurrent_growth_are_bounded(tree, monkeypatch):
    with ReadRoot(tree) as root, root.open('dir/note.txt') as opened:
        with pytest.raises(ReadBoundaryError):
            opened.read_bytes(max_bytes=2)
    original = os.read
    invoked = False
    def growing(fd, count):
        nonlocal invoked
        if not invoked:
            invoked = True
            with (tree / 'dir' / 'note.txt').open('ab') as stream:
                stream.write(b'x' * 30)
        return original(fd, count)
    with ReadRoot(tree) as root, root.open('dir/note.txt') as opened:
        monkeypatch.setattr(os, 'read', growing)
        with pytest.raises(ReadBoundaryError):
            opened.read_bytes(max_bytes=16)


def test_failed_open_releases_parent_handles(tree):
    with ReadRoot(tree) as root:
        for _ in range(30):
            with pytest.raises(OSError):
                root.open('dir/missing')
        # This succeeds only if failed operations released intermediate locks.
        (tree / 'dir').rename(tree / 'renamed')
    tree.rename(tree.parent / 'closed-root')


def test_two_threads_hold_independent_read_positions(tree):
    barrier = threading.Barrier(2)
    results, errors = [], []
    def worker():
        try:
            with ReadRoot(tree) as root, root.open('dir/note.txt') as opened:
                barrier.wait(timeout=5)
                results.append(opened.read_bytes())
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert not errors
    assert results == [b'first\nsecond\n'] * 2


def test_unknown_backend_never_uses_ordinary_open(tree, monkeypatch):
    monkeypatch.setattr('backend.core.sandbox_read.sys.platform', 'unsupported')
    with pytest.raises(ReadBoundaryError, match='No stable read backend'):
        ReadRoot(tree)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux kernel capability')
def test_linux_missing_openat2_has_no_path_fallback(tree, monkeypatch):
    import errno
    def unavailable(*a, **kw):
        raise OSError(errno.ENOSYS, 'openat2 unavailable')
    monkeypatch.setattr('backend.core.sandbox_read._linux_openat2', unavailable)
    result = read_file({'_workspace': str(tree), 'path': 'dir/note.txt'})
    assert result['error'] == 'READ_ERROR' and 'content' not in result
