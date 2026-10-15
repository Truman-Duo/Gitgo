"""Exercise the real HTTPX streaming adapter without public network requests."""

from __future__ import annotations

import io
import traceback

import httpx
import pytest

from backend.core.update import HttpsTransport, UpdateError


HOSTS = ("releases.example.test", "cdn.example.test")
URL = "https://releases.example.test/package"


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks, self.error, self.closed = chunks, error, False

    def __iter__(self):
        yield from self.chunks
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


def adapter(handler, **kwargs):
    return HttpsTransport(HOSTS, transport=httpx.MockTransport(handler), **kwargs)


def test_raw_streaming_size_limits_headers_and_cleanup():
    stream = Stream([b"ab", b"cd"])

    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        assert "authorization" not in request.headers
        assert request.extensions["timeout"]["read"] == 15
        return httpx.Response(200, headers={"Content-Length": "4"}, stream=stream)

    output = io.BytesIO()
    assert adapter(handler).receive(URL, 4, output, expected_size=4) == 4
    assert output.getvalue() == b"abcd"
    assert stream.closed


def test_metadata_fetch_accepts_chunked_response_but_never_overflows():
    assert adapter(lambda _: httpx.Response(200, stream=Stream([b"a", b"b"]))).fetch_bytes(URL, 2) == b"ab"
    output = io.BytesIO()
    with pytest.raises(UpdateError, match="UPDATE_DOWNLOAD_SIZE"):
        adapter(lambda _: httpx.Response(200, stream=Stream([b"ab", b"c"]))).receive(URL, 2, output)
    assert output.getvalue() == b"ab"


@pytest.mark.parametrize("location", [
    "https://evil.example.test/package", "http://cdn.example.test/package",
    "https://name:credential@cdn.example.test/package", "https://cdn.example.test:8443/package",
    "/package?token=secret", "/package#fragment", "../outside", "/%2e%2e/outside",
    "/%5coutside", "file:///tmp/package", "", "/bad\nheader", "/bad%",
])
def test_invalid_redirect_is_rejected_before_following(location):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": location})

    with pytest.raises(UpdateError):
        adapter(handler).fetch_bytes(URL, 100)
    assert requests == [URL]


def test_explicitly_allowed_redirect_hops_are_checked_and_no_cookies_forwarded():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert "cookie" not in request.headers
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": "/next", "Set-Cookie": "auth=secret"})
        if len(calls) == 2:
            return httpx.Response(307, headers={"Location": "https://cdn.example.test/blob"})
        return httpx.Response(200, stream=Stream([b"ok"]))

    assert adapter(handler).fetch_bytes(URL, 10) == b"ok"
    assert calls == [URL, "https://releases.example.test/next", "https://cdn.example.test/blob"]


def test_redirect_loop_is_bounded_and_response_closed():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "/package"})

    with pytest.raises(UpdateError, match="UPDATE_REDIRECT_INVALID"):
        adapter(handler, max_redirects=2).fetch_bytes(URL, 100)
    assert len(calls) == 3


@pytest.mark.parametrize("status", [201, 204, 206, 304, 401, 403, 404, 429, 500])
def test_not_a_full_success_response(status):
    with pytest.raises(UpdateError, match="UPDATE_HTTP_FAILED"):
        adapter(lambda _: httpx.Response(status)).fetch_bytes(URL, 100)


@pytest.mark.parametrize("length", ["0", "-1", "9999999999999", "4, 4", " 4", "x", "101"])
def test_response_length_is_checked_before_any_body_read(length):
    stream = Stream([b"bad"])
    with pytest.raises(UpdateError, match="UPDATE_DOWNLOAD_SIZE"):
        adapter(lambda _: httpx.Response(200, headers={"Content-Length": length}, stream=stream)).fetch_bytes(URL, 100)
    assert stream.closed


@pytest.mark.parametrize("body,length,expected", [(b"x", "2", None), (b"xxx", "2", None),
                                                 (b"x", None, 2), (b"xx", "2", 3), (b"", None, None)])
def test_short_oversized_or_signed_length_mismatch(body, length, expected):
    headers = {"Content-Length": length} if length is not None else {}
    with pytest.raises(UpdateError, match="UPDATE_DOWNLOAD_SIZE"):
        adapter(lambda _: httpx.Response(200, headers=headers, stream=Stream([body]))).receive(
            URL, 100, io.BytesIO(), expected_size=expected)


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "gzip, identity"])
def test_http_decoding_cannot_change_the_signed_artifact(encoding):
    with pytest.raises(UpdateError, match="UPDATE_HTTP_ENCODING"):
        adapter(lambda _: httpx.Response(200, headers={"Content-Encoding": encoding}, stream=Stream([b"x"]))).fetch_bytes(URL, 10)


@pytest.mark.parametrize("error", [httpx.ReadTimeout("secret proxy credential"),
                                 httpx.ConnectError("secret proxy credential"),
                                 httpx.ProxyError("secret proxy credential"),
                                 OSError("secret proxy credential")])
def test_offline_timeout_proxy_and_interrupted_stream_are_sanitized(error):
    stream = Stream([b"prefix"], error)
    with pytest.raises(UpdateError) as raised:
        adapter(lambda _: httpx.Response(200, stream=stream)).fetch_bytes(URL, 100)
    assert "secret proxy credential" not in "".join(traceback.format_exception(raised.value))
    assert stream.closed


def test_cancel_closes_stream_and_stops_next_chunk():
    checks = iter([False, False, False, True])
    stream = Stream([b"a", b"b"])
    output = io.BytesIO()
    with pytest.raises(UpdateError, match="UPDATE_CANCELLED"):
        adapter(lambda _: httpx.Response(200, stream=stream)).receive(
            URL, 100, output, cancelled=lambda: next(checks))
    assert output.getvalue() == b"a"
    assert stream.closed


def test_total_deadline_checks_even_tiny_chunks(monkeypatch):
    from backend.core.update import transport
    ticks = iter([0, 0, 0, 2, 4])
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(ticks))
    stream = Stream([b"a", b"b"])
    output = io.BytesIO()
    with pytest.raises(UpdateError, match="UPDATE_DOWNLOAD_TIMEOUT"):
        adapter(lambda _: httpx.Response(200, stream=stream), deadline=3).receive(URL, 10, output)
    assert output.getvalue() == b"a"
    assert stream.closed


def test_short_disk_write_cannot_report_success():
    class ShortWrite:
        def write(self, chunk):
            return len(chunk) - 1
    with pytest.raises(UpdateError, match="UPDATE_WRITE_FAILED"):
        adapter(lambda _: httpx.Response(200, stream=Stream([b"ok"]))).receive(URL, 10, ShortWrite())


@pytest.mark.parametrize("kwargs", [{"timeout": 0}, {"timeout": True}, {"deadline": float("inf")},
                                   {"deadline": float("nan")}, {"max_redirects": -1}, {"max_redirects": 6}])
def test_local_transport_settings_cannot_disable_limits(kwargs):
    with pytest.raises(UpdateError):
        HttpsTransport(HOSTS, **kwargs)


def test_bad_initial_url_never_reaches_transport():
    def forbidden(_):
        pytest.fail("invalid URL reached network")
    with pytest.raises(UpdateError, match="UPDATE_URL_INVALID"):
        adapter(forbidden).fetch_bytes("https://evil.example.test/file", 100)
