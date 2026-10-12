"""Transient transport retries do not weaken pinned asset verification."""
import hashlib
import io
import urllib.error
import pytest
from scripts import ci_downloads as downloads


def test_504_retries_then_verifies_the_same_pinned_asset(monkeypatch, capsys):
    calls = []
    def open_asset(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise urllib.error.HTTPError(args[0], 504, 'fixture timeout', {}, None)
        return io.BytesIO(b'fixture')
    monkeypatch.setattr(downloads.urllib.request, 'urlopen', open_asset)
    monkeypatch.setattr(downloads.time, 'sleep', lambda _: None)
    assert downloads.fetch_verified('https://example.test/pinned', hashlib.sha256(b'fixture').hexdigest(), limit=20) == b'fixture'
    assert len(calls) == 2 and calls[0] == calls[1]
    assert 'HTTP 504; retry' in capsys.readouterr().out


@pytest.mark.parametrize('failure', ['digest', 'oversize', 'not-found', 'exhausted'])
def test_integrity_and_permanent_failures_remain_fail_closed(monkeypatch, failure):
    calls = []
    def open_asset(*args, **kwargs):
        calls.append(args)
        if failure in ('not-found', 'exhausted'):
            raise urllib.error.HTTPError(args[0], 404 if failure == 'not-found' else 504, 'fixture', {}, None)
        return io.BytesIO(b'fixture')
    monkeypatch.setattr(downloads.urllib.request, 'urlopen', open_asset)
    monkeypatch.setattr(downloads.time, 'sleep', lambda _: None)
    with pytest.raises((ValueError, urllib.error.HTTPError)):
        downloads.fetch_verified('https://example.test/pinned', '0' * 64,
                                 limit=2 if failure == 'oversize' else 20)
    assert len(calls) == (3 if failure == 'exhausted' else 1)
