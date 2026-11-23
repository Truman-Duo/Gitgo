"""Immutable context blobs are published once and verified before ref updates."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from backend.core.loop.context_store import ContextObjectStore


def blob(store, value):
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    return store.blobs / (digest + '.json'), digest, content


def test_existing_corrupt_blob_is_not_accepted_or_repaired(tmp_path_factory):
    store = ContextObjectStore(tmp_path_factory)
    path, digest, _ = blob(store, {'answer': 42})
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'digest': digest, 'content': 'wrong', 'metadata': {}}), encoding='utf8')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='integrity'):
        store.put('task/corrupt', {'answer': 42})
    assert path.read_bytes() == before
    assert not (store.refs_dir / 'task/corrupt.json').exists()


def test_republishing_does_not_replace_or_rewrite_an_existing_blob(tmp_path_factory, monkeypatch):
    store = ContextObjectStore(tmp_path_factory)
    first = store.put('task/first', {'answer': 42}, metadata={'producer': 'first'})
    path, _, _ = blob(store, {'answer': 42})
    before = path.stat(), path.read_bytes()
    original = os.replace

    def no_blob_replace(source, destination):
        assert Path(destination).parent != store.blobs
        return original(source, destination)

    monkeypatch.setattr(os, 'replace', no_blob_replace)
    second = store.put('task/second', {'answer': 42}, metadata={'producer': 'second'})
    assert path.stat().st_ino == before[0].st_ino and path.read_bytes() == before[1]
    assert first['digest'] == second['digest']
    assert store.reference_info(first['latest'])['producer'] == 'first'
    assert store.reference_info(second['latest'])['producer'] == 'second'


def test_cold_same_content_publishers_and_readers_share_one_object(tmp_path_factory, monkeypatch):
    store = ContextObjectStore(tmp_path_factory)
    path, _, _ = blob(store, [])
    ready = threading.Barrier(8)
    original = Path.exists

    def cold_lookup(selected):
        result = original(selected)
        # Force the old exists -> replace implementation past the same cold
        # observation. The no-replace implementation needs no such lookup.
        if selected == path:
            ready.wait(timeout=10)
            return False
        return result

    monkeypatch.setattr(Path, 'exists', cold_lookup)

    def publish(index):
        current = ContextObjectStore(tmp_path_factory)
        result = current.put(f'task/parallel-{index}', [], metadata={'producer': str(index)})
        assert current.resolve(result['pinned']).content == '[]'
        return result

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(publish, range(8)))
    assert len({item['digest'] for item in results}) == 1
    assert len(list(store.blobs.glob('*.json'))) == 1
    assert list(store.blobs.glob('.*.tmp')) == []
    assert path.stat().st_nlink == 1


@contextmanager
def windows_read_without_delete_share(path):
    kernel = C.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [C.c_wchar_p, C.c_uint32, C.c_uint32, C.c_void_p,
                       C.c_uint32, C.c_uint32, C.c_void_p]
    create.restype = C.c_void_p
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [C.c_void_p], C.c_int
    handle = create(str(path), 0x80000000, 1 | 2, None, 3, 0x80, None)
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        yield
    finally:
        if not kernel.CloseHandle(handle):
            raise C.WinError(C.get_last_error())


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Windows rename sharing')
def test_losing_cold_publisher_reuses_object_held_by_a_reader(tmp_path_factory, monkeypatch):
    store = ContextObjectStore(tmp_path_factory)
    path, _, _ = blob(store, [])
    original = Path.exists
    read_text = Path.read_text
    held = []
    publishing = False

    def publish_winner():
        nonlocal publishing
        publishing = True
        store.put('task/winner', [])
        owner = windows_read_without_delete_share(path)
        owner.__enter__()
        held.append(owner)

    def competing_publication(selected):
        if selected != path or publishing:
            return original(selected)
        assert not original(path)
        publish_winner()
        return False

    def missing_before_publication(selected, *args, **kwargs):
        if selected == path and not publishing:
            publish_winner()
            raise FileNotFoundError(path)
        return read_text(selected, *args, **kwargs)

    try:
        monkeypatch.setattr(Path, 'exists', competing_publication)
        monkeypatch.setattr(Path, 'read_text', missing_before_publication)
        result = store.put('task/loser', [])
        assert store.resolve(result['pinned']).content == '[]'
        assert len(held) == 1
    finally:
        for owner in held:
            owner.__exit__(None, None, None)


def test_same_content_processes_do_not_replace_each_others_blobs(tmp_path_factory):
    ready = tmp_path_factory / 'ready'
    ready.mkdir()
    code = r'''
from pathlib import Path
import sys, time
from backend.core.loop.context_store import ContextObjectStore
root, index = Path(sys.argv[1]), sys.argv[2]
(root / 'ready' / index).touch()
deadline = time.monotonic() + 15
while len(list((root / 'ready').iterdir())) < 4:
    assert time.monotonic() < deadline, 'publishers failed to start'
    time.sleep(.01)
store = ContextObjectStore(root)
for generation in range(20):
    result = store.put('parallel/process-' + index, {'generation': generation})
    assert store.resolve(result['pinned']).content == '{"generation":' + str(generation) + '}'
'''
    processes = [subprocess.Popen([sys.executable, '-B', '-c', code, str(tmp_path_factory), str(i)],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True) for i in range(4)]
    try:
        for process in processes:
            out, err = process.communicate(timeout=30)
            assert process.returncode == 0, (out, err)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()
    store = ContextObjectStore(tmp_path_factory)
    assert len(list(store.blobs.glob('*.json'))) == 20
    for index in range(4):
        assert store.resolve(f'context:parallel/process-{index}@latest').content == '{"generation":19}'


def test_atomic_collision_validates_winner_without_replacing_it(tmp_path_factory, monkeypatch):
    store = ContextObjectStore(tmp_path_factory)
    path, digest, content = blob(store, [])

    def winner(source, destination):
        assert Path(destination) == path
        path.write_text(json.dumps({'digest': digest, 'content': content, 'metadata': {}}), encoding='utf8')
        raise FileExistsError(path)

    monkeypatch.setattr(os, 'link', winner)
    result = store.put('task/collision', [])
    assert store.resolve(result['pinned']).content == '[]'
    assert list(store.blobs.glob('.*.tmp')) == []


def test_corrupt_collision_never_publishes_a_ref(tmp_path_factory, monkeypatch):
    store = ContextObjectStore(tmp_path_factory)
    path, digest, _ = blob(store, [])

    def corrupt_winner(source, destination):
        path.write_text(json.dumps({'digest': digest, 'content': 'wrong', 'metadata': {}}), encoding='utf8')
        raise FileExistsError(path)

    monkeypatch.setattr(os, 'link', corrupt_winner)
    with pytest.raises(ValueError, match='integrity'):
        store.put('task/collision', [])
    assert not (store.refs_dir / 'task/collision.json').exists()
    assert list(store.blobs.glob('.*.tmp')) == []


@pytest.mark.parametrize('failure', ['permission', 'disk', 'unsupported'])
def test_publication_errors_are_not_hidden_by_a_valid_winner(tmp_path_factory, monkeypatch, failure):
    import errno
    store = ContextObjectStore(tmp_path_factory)
    path, digest, content = blob(store, [])
    error = {'permission': PermissionError(errno.EACCES, 'denied'),
             'disk': OSError(errno.ENOSPC, 'full'),
             'unsupported': OSError(errno.ENOTSUP, 'no atomic publication')}[failure]

    def failed_publication(source, destination):
        path.write_text(json.dumps({'digest': digest, 'content': content, 'metadata': {}}), encoding='utf8')
        raise error

    monkeypatch.setattr(os, 'link', failed_publication)
    with pytest.raises(OSError) as caught:
        store.put('task/failure', [])
    assert caught.value is error
    assert not (store.refs_dir / 'task/failure.json').exists()
    assert list(store.blobs.glob('.*.tmp')) == []


def test_fsync_failure_publishes_neither_blob_nor_ref(tmp_path_factory, monkeypatch):
    import errno
    store = ContextObjectStore(tmp_path_factory)
    path, _, _ = blob(store, [])
    error = OSError(errno.ENOSPC, 'flush failed')
    def failed_flush(_fd):
        raise error
    monkeypatch.setattr(os, 'fsync', failed_flush)
    with pytest.raises(OSError) as caught:
        store.put('task/failure', [])
    assert caught.value is error
    assert not path.exists()
    assert not (store.refs_dir / 'task/failure.json').exists()
    assert list(store.blobs.glob('.*.tmp')) == []
