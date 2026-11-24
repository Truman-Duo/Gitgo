"""Bounded transfer progress/cancellation using real HTTPX request streams."""
from types import SimpleNamespace

import httpx
import pytest

from backend.core.publish.release_engine import ReleaseCancelled
from backend.core.publish.release_transport import ReleaseTransport


def transport(monkeypatch, handler, cancelled, observations):
    clock = iter(range(10000))
    monkeypatch.setattr("backend.core.publish.release_transport.time", SimpleNamespace(monotonic=lambda: next(clock) / 4))
    class StreamingTransport(httpx.BaseTransport):
        def handle_request(self, request):
            return handler(request)
    client = SimpleNamespace(target=SimpleNamespace(kind="github", instance="https://github.com"),
                             client=httpx.Client(transport=StreamingTransport()))
    return ReleaseTransport(client, cancelled=cancelled, progress=observations.append)


def test_upload_cancels_between_chunks_and_keeps_fixed_content_length(monkeypatch):
    stopped, chunks, observations = [False], [], []
    def handler(request):
        assert request.headers["content-length"] == str(4 * 65536)
        assert "transfer-encoding" not in request.headers
        for chunk in request.stream:
            chunks.append(chunk)
            if len(chunks) == 2:
                stopped[0] = True
        pytest.fail("Cancelled upload must not reach a success response")
    remote = transport(monkeypatch, handler, lambda: stopped[0], observations)
    try:
        with pytest.raises(ReleaseCancelled):
            remote._binary("POST", "https://uploads.github.com/asset", data=b"x" * (4 * 65536), asset_name="one.zip")
        assert len(chunks) == 2
        assert observations[0]["total"] == 4 * 65536
        assert all(row["state"] == "running" and row["direction"] == "upload" for row in observations)
    finally:
        remote.client.client.close()


def test_download_cancel_closes_the_response_and_never_reports_complete(monkeypatch):
    stopped, observations = [False], []
    class Stream(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            yield b"a" * 65536
            stopped[0] = True
            yield b"b" * 65536
            pytest.fail("The remaining response must not be consumed")
        def close(self):
            self.closed = True
    stream = Stream()
    remote = transport(monkeypatch, lambda _: httpx.Response(200, stream=stream, headers={"content-length": str(3 * 65536)}), lambda: stopped[0], observations)
    try:
        with pytest.raises(ReleaseCancelled):
            remote._binary("GET", "https://api.github.com/asset", asset_name="one.zip")
        assert stream.closed
        assert all(row["state"] != "completed" for row in observations)
    finally:
        remote.client.client.close()


def test_completed_transfer_reports_actual_bytes_and_absence_is_not_success(monkeypatch):
    observations = []
    remote = transport(monkeypatch, lambda request: httpx.Response(404 if request.url.path == "/missing" else 200, content=b"fixed"), lambda: False, observations)
    try:
        assert remote._binary("GET", "https://api.github.com/one", asset_name="one.zip") == b"fixed"
        assert observations[-1]["state"] == "completed" and observations[-1]["bytes"] == 5
        assert remote._binary("GET", "https://api.github.com/missing", allow_absent=True) is None
        assert observations[-1]["state"] == "absent"
    finally:
        remote.client.client.close()
