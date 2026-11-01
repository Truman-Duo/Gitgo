"""Derived-cache publication retains the existing metadata and OS lock boundary."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import subprocess
import sys

import pytest

from backend.core import dependency_graph as graphs
from backend.core.storage import metadata_files as metadata
from backend.core.storage.maintenance import StorageLease, StorageMaintenanceActive


def sources(root):
    (root / 'feature.py').write_text('VALUE = 1\n', encoding='utf-8')
    (root / 'view.py').write_text('from feature import VALUE\n', encoding='utf-8')


@pytest.mark.parametrize('through_tool', [False, True])
def test_parallel_cold_dependency_queries_share_a_valid_cache(tmp_path_factory, through_tool):
    sources(tmp_path_factory)
    if through_tool:
        from types import SimpleNamespace
        from backend.core.loop.session import AgentSession
        from backend.core.loop.context_store import build_context_tools
        tool = build_context_tools(SimpleNamespace(session=AgentSession()), str(tmp_path_factory))['dependency_query']
    barrier = threading.Barrier(8)
    def query(_):
        barrier.wait(timeout=10)
        if through_tool:
            return tool.execute({'path':'feature.py'})['dependents']
        return graphs.load_dependency_graph(tmp_path_factory).get_dependents('feature.py')
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(query, range(8)))
    assert all(any(edge['dependent'] == 'view.py' for edge in result) for result in results)
    saved = json.loads((tmp_path_factory / '.gitgo' / graphs.GRAPH_FILE).read_text(encoding='utf-8'))
    assert saved['version'] == graphs.GRAPH_VERSION


def test_registered_dependency_tool_queries_across_processes(tmp_path_factory):
    sources(tmp_path_factory)
    script = '''
import sys,json
sys.path[:0]=json.loads(sys.argv[2])
from types import SimpleNamespace
from backend.core.loop.session import AgentSession
from backend.core.loop.context_store import build_context_tools
tool=build_context_tools(SimpleNamespace(session=AgentSession()),sys.argv[1])['dependency_query']
print('ready',flush=True)
sys.stdin.readline()
print(json.dumps(tool.execute({'path':'feature.py'})),flush=True)
'''
    children = [subprocess.Popen([sys.executable,'-B','-c',script,str(tmp_path_factory),json.dumps(sys.path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8') for _ in range(4)]
    try:
        for child in children:
            assert child.stdout.readline().strip() == 'ready'
        for child in children:
            child.stdin.write('go\n'); child.stdin.flush()
        for child in children:
            output, error = child.communicate(timeout=20)
            assert child.returncode == 0, error
            result = json.loads(output)
            assert any(edge['dependent'] == 'view.py' for edge in result['dependents'])
    finally:
        for child in children:
            if child.poll() is None:
                child.kill(); child.wait(timeout=5)


def test_slow_older_builder_does_not_overwrite_newer_cache(tmp_path_factory, monkeypatch):
    sources(tmp_path_factory)
    waiting, release = threading.Event(), threading.Event()
    original = graphs._save_graph
    first_thread = []
    def save(graph, **kwargs):
        if threading.get_ident() == first_thread[0]:
            waiting.set()
            assert release.wait(10)
        return original(graph, **kwargs)
    monkeypatch.setattr(graphs, '_save_graph', save)
    def old_build():
        first_thread.append(threading.get_ident())
        return graphs.build_dependency_graph(tmp_path_factory)
    with ThreadPoolExecutor(1) as pool:
        old = pool.submit(old_build)
        try:
            assert waiting.wait(10)
            (tmp_path_factory / 'view.py').write_text('VALUE = 2\n', encoding='utf-8')
            current = graphs.build_dependency_graph(tmp_path_factory)
        finally:
            release.set()
        assert old.result().get_dependents('feature.py')
    cached = graphs.load_dependency_graph(tmp_path_factory, allow_stale=True)
    assert cached.file_fingerprints == current.file_fingerprints
    assert not cached.get_dependents('feature.py')


def test_busy_metadata_is_bounded_and_not_misreported_as_database_maintenance(tmp_path_factory):
    metadata.write_metadata_bytes(tmp_path_factory, 'cache.json', b'old')
    lock = tmp_path_factory / '.gitgo' / 'metadata-locks' / hashlib.sha256(b'cache.json').hexdigest()
    with StorageLease(lock, exclusive=True):
        with pytest.raises(metadata.MetadataFileBusy, match='cache.json'):
            metadata.write_metadata_bytes(tmp_path_factory, 'cache.json', b'new', lock_timeout=0.03)
    assert metadata.read_metadata_bytes(tmp_path_factory, 'cache.json') == b'old'
    from backend.core.errors import catalog_entry
    assert catalog_entry('METADATA_FILE_BUSY')['catalog_id'] == 'GITGO-E7433'


def test_access_denial_is_not_retried_as_lock_contention(tmp_path_factory, monkeypatch):
    calls = []
    def denied(*args, **kwargs):
        calls.append(True)
        try:
            raise PermissionError(13, 'Access denied')
        except PermissionError as cause:
            raise StorageMaintenanceActive('generic lease error') from cause
    monkeypatch.setattr(metadata, 'StorageLease', denied)
    with pytest.raises(PermissionError, match='Access denied'):
        metadata.write_metadata_bytes(tmp_path_factory, 'cache.json', b'new', lock_timeout=5)
    assert len(calls) == 1


def test_unknown_publication_is_not_retried(tmp_path_factory, monkeypatch):
    calls = []
    def failed(*args, **kwargs):
        calls.append(True)
        raise OSError('replace outcome unknown')
    monkeypatch.setattr(metadata, 'atomic_replace_bytes', failed)
    with pytest.raises(metadata.MetadataCommitUnknown):
        metadata.write_metadata_bytes(tmp_path_factory, 'cache.json', b'new', lock_timeout=5)
    assert len(calls) == 1
