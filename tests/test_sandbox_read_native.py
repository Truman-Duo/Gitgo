"""Real candidate Rust reads. No replacement of the production reader yet."""
import ctypes as C
import hashlib
import os
import sys
import threading

import pytest

from backend.core.sandbox_read import MAX_READ_BYTES, ReadBoundaryError
from backend.core.sandbox_read_native import NativeReadRoot, _call, _path

pytestmark = pytest.mark.skipif(sys.platform not in {'win32', 'linux', 'darwin'},
                                reason='Native read platform required')


@pytest.fixture
def tree(tmp_path_factory):
    # This repository supplies a concrete Path, not pytest's usual factory.
    path = tmp_path_factory / 'rust-read'
    path.mkdir()
    (path / 'nested').mkdir()
    (path / 'nested' / 'note').write_bytes(b'hello\x00\xff\n')
    return path


def test_native_contents_identity_hash_and_close(tree):
    body = (tree / 'nested' / 'note').read_bytes()
    with NativeReadRoot(tree) as root, root.open('nested/note') as opened:
        assert root.identity != opened.identity
        assert opened.read_bytes(max_bytes=len(body)) == body
        assert opened.sha256() == (hashlib.sha256(body).hexdigest(), len(body))
        with pytest.raises(ValueError):
            opened.read_bytes(max_bytes=True)
    root.close()
    opened.close()
    with pytest.raises(ValueError):
        opened.sha256()
    with pytest.raises(ValueError):
        root.open('nested/note')


def test_metadata_pin_cannot_export_content_or_hash(tree):
    with NativeReadRoot(tree) as root, root.pin('nested/note') as pin:
        with pytest.raises(ReadBoundaryError):
            _call(pin._library, op='read_hash', token=pin._token, max_bytes=1024)
        function = pin._library._library.gitgo_native_read
        function.argtypes = [C.c_uint64, C.c_size_t, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
        function.restype = C.c_uint32
        output = C.create_string_buffer(b'UNCHANGED')
        written = C.c_size_t(99)
        assert function(pin._token, 8, output, len(output), C.byref(written)) == 3
        assert written.value == 0 and output.value == b'UNCHANGED'


@pytest.mark.parametrize('path', ['..', '../secret', '/absolute', 'nested/../../secret', '', 'bad\x00name'])
def test_no_relative_escape_even_through_raw_abi(tree, path):
    with NativeReadRoot(tree) as root, pytest.raises(OSError):
        _call(root._library, op='read_open', token=root._token, path=_path(path), metadata_only=False)


@pytest.mark.parametrize('name,exception', [('nested', IsADirectoryError), ('missing', FileNotFoundError)])
def test_typed_errors_are_preserved(tree, name, exception):
    with NativeReadRoot(tree) as root, pytest.raises(exception):
        root.open(name)


def test_limit_errors_never_export_a_prefix(tree):
    with NativeReadRoot(tree) as root, root.open('nested/note') as opened:
        with pytest.raises(ReadBoundaryError):
            opened.read_bytes(max_bytes=2)
        # A size refusal before reading does not consume the descriptor.
        assert opened.read_bytes(max_bytes=8) == b'hello\x00\xff\n'
        with pytest.raises(ReadBoundaryError):
            opened.sha256(max_bytes=2)
        for invalid in (0, -1, True, MAX_READ_BYTES + 1):
            with pytest.raises(ValueError):
                opened.read_bytes(max_bytes=invalid)


def test_content_larger_than_control_response_uses_binary_transport(tree):
    body = bytes(range(256)) * 1024
    (tree / 'large').write_bytes(body)
    with NativeReadRoot(tree) as root, root.open('large') as opened:
        assert opened.read_bytes(max_bytes=len(body)) == body


def test_exact_read_limit_and_one_byte_over(tree):
    body = b'x' * MAX_READ_BYTES
    (tree / 'large').write_bytes(body)
    with NativeReadRoot(tree) as root, root.open('large') as opened:
        assert opened.read_bytes() == body
    with (tree / 'large').open('ab') as writer:
        writer.write(b'y')
    with NativeReadRoot(tree) as root, root.open('large') as opened, pytest.raises(ReadBoundaryError):
        opened.read_bytes()


def test_link_and_hardlink_refusal(tree):
    target = tree / 'nested' / 'note'
    os.link(target, tree / 'hardlink')
    with NativeReadRoot(tree) as root, pytest.raises(ReadBoundaryError):
        root.open('hardlink')
    (tree / 'hardlink').unlink()
    if sys.platform == 'win32':
        import _winapi
        _winapi.CreateJunction(str(tree / 'nested'), str(tree / 'alias'))
    else:
        (tree / 'alias').symlink_to(tree / 'nested', target_is_directory=True)
    with NativeReadRoot(tree) as root, pytest.raises(OSError):
        root.open('alias/note')
    with pytest.raises(OSError):
        NativeReadRoot(tree / 'alias')


def test_close_root_does_not_close_already_owned_file(tree):
    with NativeReadRoot(tree) as root:
        opened = root.open('nested/note')
    with opened:
        assert opened.read_bytes() == b'hello\x00\xff\n'


def test_retired_token_never_selects_new_object(tree):
    with NativeReadRoot(tree) as root:
        with root.open('nested/note') as first:
            old = first._token
        with root.open('nested/note') as second:
            assert second._token != old
            _call(root._library, op='read_close', token=old)
            with pytest.raises(ReadBoundaryError):
                _call(root._library, op='read_hash', token=old, max_bytes=1024)
            assert second.read_bytes() == b'hello\x00\xff\n'


def test_pinned_target_cannot_be_redirected_by_replacement(tree):
    replacement = tree / 'replacement'
    replacement.write_bytes(b'ATTACKER')
    with NativeReadRoot(tree) as root, root.open('nested/note') as opened:
        if sys.platform == 'win32':
            with pytest.raises(OSError):
                os.replace(replacement, tree / 'nested' / 'note')
            assert opened.read_bytes() == b'hello\x00\xff\n'
        else:
            os.replace(replacement, tree / 'nested' / 'note')
            with pytest.raises(ReadBoundaryError):
                opened.read_bytes()


def test_pinned_root_cannot_be_redirected_by_replacement(tree):
    with NativeReadRoot(tree) as root:
        if sys.platform == 'win32':
            with pytest.raises(OSError):
                tree.rename(tree.parent / 'moved')
        else:
            tree.rename(tree.parent / 'moved')
            (tree / 'nested').mkdir(parents=True)
            (tree / 'nested' / 'note').write_bytes(b'ATTACKER')
        with root.open('nested/note') as opened:
            assert opened.read_bytes() == b'hello\x00\xff\n'


def test_failed_open_releases_intermediate_objects(tree):
    with NativeReadRoot(tree) as root:
        for _ in range(30):
            with pytest.raises(FileNotFoundError):
                root.open('nested/missing')
        (tree / 'nested').rename(tree / 'renamed')
    tree.rename(tree.parent / 'closed')


def test_separate_readers_have_independent_positions(tree):
    barrier = threading.Barrier(2)
    results, failures = [], []
    def run():
        try:
            with NativeReadRoot(tree) as root, root.open('nested/note') as opened:
                barrier.wait(timeout=5)
                results.append(opened.read_bytes())
        except BaseException as error:
            failures.append(error)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert failures == [] and results == [b'hello\x00\xff\n'] * 2


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX special files')
def test_special_file_refused_without_io(tree):
    os.mkfifo(tree / 'fifo')
    with NativeReadRoot(tree) as root, pytest.raises(OSError):
        root.open('fifo')


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux permits non-UTF8 file names; APFS does not')
def test_non_utf8_names_preserved(tree):
    raw = os.fsencode(tree) + b'/nonutf8-\xff'
    fd = os.open(raw, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    os.write(fd, b'byte-path')
    os.close(fd)
    with NativeReadRoot(tree) as root:
        with root.open(os.fsdecode(b'nonutf8-\xff')) as opened:
            assert opened.read_bytes() == b'byte-path'


def test_binary_output_capacity_checked_before_descriptor_is_consumed(tree):
    with NativeReadRoot(tree) as root, root.open('nested/note') as opened:
        function = opened._library._library.gitgo_native_read
        function.argtypes = [C.c_uint64, C.c_size_t, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
        function.restype = C.c_uint32
        output, written = C.create_string_buffer(b'no'), C.c_size_t(99)
        assert function(opened._token, 8, output, len(output), C.byref(written)) == 1
        assert output.value == b'no' and written.value == 99
        assert opened.read_bytes() == b'hello\x00\xff\n'


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows alias parsing')
@pytest.mark.parametrize('name', ['NUL', 'COM1.txt', 'a:stream', 'note.', 'nested /x', 'C:note', '\\\\server\\share\\x'])
def test_raw_native_windows_aliases_refused(tree, name):
    with NativeReadRoot(tree) as root, pytest.raises(OSError):
        _call(root._library, op='read_open', token=root._token, path=_path(name), metadata_only=False)
