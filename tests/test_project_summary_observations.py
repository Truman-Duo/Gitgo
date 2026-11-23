"""Host observations retain their project owner and latest failure evidence."""
from concurrent.futures import Future
import io
import json
import threading

import pytest

from backend.core.native_host import NativeHost


class PendingReads:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        future = Future()
        self.calls.append((future, fn, args))
        return future

    def finish(self, index):
        future, fn, args = self.calls[index]
        future.set_result(fn(*args))

    def shutdown(self, **kwargs):
        for future, _, _ in self.calls:
            future.cancel()


@pytest.fixture
def projection(monkeypatch):
    host = NativeHost(stdout=io.StringIO())
    host._summary_pool.shutdown(wait=True)
    reads = PendingReads()
    host._summary_pool = reads
    monkeypatch.setattr('backend.core.native_host.wait', lambda futures, **kwargs: (
        {f for f in futures if f.done()}, {f for f in futures if not f.done()}))
    monkeypatch.setattr('backend.core.storage.read_project_list_status', lambda workspace: {
        'status': 'completed', 'process_statuses': {'a': 'completed'} if workspace == '/old' else {}})
    try:
        yield host, reads
    finally:
        host.close()


def events(host):
    return [json.loads(line)['payload'] for line in host.stdout.getvalue().splitlines()
            if json.loads(line).get('payload', {}).get('event') == 'project_summary_updated']


@pytest.mark.parametrize('identity', ['uuid', 'workspace'])
def test_replacement_does_not_reuse_or_publish_old_inflight_observation(projection, identity):
    host, reads = projection
    old = {'name': 'same', 'workspace': '/old', 'project_id': 'old' if identity == 'uuid' else ''}
    new = {'name': 'same', 'workspace': '/new', 'project_id': 'new' if identity == 'uuid' else ''}
    host._runtime_summaries([old])
    result = host._runtime_summaries([new])
    assert len(reads.calls) == 2
    assert result['projects']['same']['state_available'] is False
    reads.finish(1)
    published = events(host)
    assert published[-1]['project_id'] == new['project_id']
    assert published[-1]['summary']['finished_process_count'] == 0
    reads.finish(0)
    assert events(host) == published
    assert host._summary_cache['same']['finished_process_count'] == 0


def test_deleted_project_late_read_cannot_repopulate_cache(projection):
    host, reads = projection
    host._runtime_summaries([{'name': 'gone', 'workspace': '/old', 'project_id': 'old'}])
    host._runtime_summaries([])
    reads.finish(0)
    assert 'gone' not in host._summary_cache
    assert events(host) == []


def test_same_name_replacement_discards_warm_cache_even_during_refresh(projection):
    host, reads = projection
    host._runtime_summaries([{'name': 'same', 'workspace': '/old', 'project_id': 'old'}])
    reads.finish(0)
    host._summary_refresh_lock.acquire()
    try:
        result = host._runtime_summaries([{'name': 'same', 'workspace': '/new', 'project_id': 'new'}])
    finally:
        host._summary_refresh_lock.release()
    assert result['projects']['same']['state_available'] is False
    assert result['projects']['same']['finished_process_count'] == 0


def test_failed_read_remains_visible_on_next_poll_until_fresh_success(projection, monkeypatch):
    host, reads = projection
    project = {'name': 'same', 'workspace': '/old', 'project_id': 'owner'}
    host._runtime_summaries([project])
    reads.finish(0)
    healthy_time = host._summary_cache['same']['observed_at']
    def fail(_workspace):
        raise OSError('database unavailable')
    monkeypatch.setattr('backend.core.storage.read_project_list_status', fail)
    host._runtime_summaries([project])
    reads.finish(1)
    failed = events(host)[-1]
    assert failed['project_id'] == 'owner'
    assert failed['summary']['state_available'] is False
    next_poll = host._runtime_summaries([project])['projects']['same']
    assert next_poll['state_available'] is False
    assert next_poll['summary_error'] == 'database unavailable'
    assert next_poll['finished_process_count'] == 1
    assert next_poll['last_known_at'] == str(healthy_time)
    monkeypatch.setattr('backend.core.storage.read_project_list_status', lambda _: {'status': 'completed'})
    reads.finish(2)
    assert events(host)[-1]['summary']['state_available'] is True
    assert host._summary_cache['same']['summary_error'] == ''


def test_replacement_coalesced_before_old_schedule_cannot_attach_old_read_to_new_metadata(projection, monkeypatch):
    host, reads = projection
    old = {'name': 'same', 'workspace': '/old', 'project_id': 'old'}
    new = {'name': 'same', 'workspace': '/new', 'project_id': 'new'}
    taken, resume = threading.Event(), threading.Event()
    class PausedFirstAcquire:
        def __init__(self):
            self.lock, self.first = threading.Lock(), True
        def acquire(self, blocking=False):
            acquired = self.lock.acquire(blocking=blocking)
            if acquired and self.first:
                self.first = False
                taken.set()
                assert resume.wait(10)
            return acquired
        def release(self):
            self.lock.release()
    host._summary_refresh_lock = PausedFirstAcquire()
    old_results = []
    thread = threading.Thread(target=lambda: old_results.append(host._runtime_summaries([old])))
    thread.start()
    try:
        assert taken.wait(10)
        replacement = host._runtime_summaries([new])
        assert replacement['refresh_coalesced'] is True
        resume.set(); thread.join(10); assert not thread.is_alive()
        host.services.project_list = lambda: [new]
        def finish_old_during_wait(futures, **kwargs):
            for index, (future, _, _) in enumerate(reads.calls):
                if not future.done():
                    reads.finish(index)
            return {f for f in futures if f.done()}, {f for f in futures if not f.done()}
        monkeypatch.setattr('backend.core.native_host.wait', finish_old_during_wait)
        result = host._project_overview()['projects'][0]
        print('replacement overview:', result)
        assert result['project_id'] == 'new'
        assert result['runtime_summary']['finished_process_count'] == 0
    finally:
        resume.set(); thread.join(10)
