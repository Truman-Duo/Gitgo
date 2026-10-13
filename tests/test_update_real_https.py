"""Actual TLS sockets and HTTPX, isolated from public DNS/network and user CAs.

Only the socket destination of the fixed test publisher is redirected to a
loopback ephemeral port. URL validation, TLS hostname/chain verification,
HTTPX streaming and production transport error handling remain real.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import select
import socket
import ssl
import threading
import time
import traceback

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from backend.core.update import HttpsTransport, UpdateError

HOST = "release.example.test"
URL = f"https://{HOST}/package"


def certificate(root):
    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Disposable test CA")])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256()))
    server_key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)]))
        .issuer_name(name).public_key(server_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256()))
    paths = [root / item for item in ("ca.pem", "server.pem", "server-key.pem")]
    for path, data in zip(paths, (ca.public_bytes(serialization.Encoding.PEM),
        cert.public_bytes(serialization.Encoding.PEM), server_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))):
        path.write_bytes(data); path.chmod(0o600)
    return paths


@pytest.fixture
def real_tls(tmp_path_factory, monkeypatch):
    ca, cert, key = certificate(tmp_path_factory)
    for key_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
                     "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(key_name, raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    records = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            records.append((self.path, dict(self.headers)))
            if self.path == "/redirect":
                self.send_response(302); self.send_header("Location", "/package")
                self.send_header("Set-Cookie", "disposable-cookie=not-forwarded"); self.end_headers()
                return
            if self.path == "/foreign":
                self.send_response(302); self.send_header("Location", "https://unapproved.example.test/package")
                self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Length", "10" if self.path == "/cut" else "4")
            self.end_headers()
            try:
                self.wfile.write(b"ab"); self.wfile.flush()
                if self.path == "/cut": return
                if self.path == "/slow": time.sleep(.15)
                self.wfile.write(b"cd"); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                pass  # Deadline/cancellation closes the client's actual socket.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    worker.start()
    create_connection = socket.create_connection
    destinations = []
    proxies = set()

    def connect(address, *args, **kwargs):
        if address in proxies:
            return create_connection(address, *args, **kwargs)
        destinations.append(address)
        assert address in ((HOST, 443), ("alternate.example.test", 443)), "Test must not make public or unapproved connections"
        return create_connection(("127.0.0.1", server.server_port), *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", connect)

    @contextmanager
    def proxy(*, reject=False):
        connections = []
        class Proxy(BaseHTTPRequestHandler):
            rbufsize = 0
            def log_message(self, *_): pass
            def do_CONNECT(self):
                connections.append((self.path, self.headers.get("Proxy-Authorization")))
                assert self.path == HOST + ":443"
                if reject:
                    self.send_response(407, "private proxy diagnostic")
                    self.end_headers(); return
                upstream = create_connection(("127.0.0.1", server.server_port), timeout=2)
                try:
                    self.send_response(200); self.end_headers()
                    end = time.monotonic() + 3
                    peers = (self.connection, upstream)
                    while time.monotonic() < end:
                        for source in select.select(peers, [], [], .05)[0]:
                            data = source.recv(8192)
                            if not data: return
                            (upstream if source is self.connection else self.connection).sendall(data)
                except (OSError, ssl.SSLError):
                    pass
                finally:
                    upstream.close(); self.close_connection = True
        local_proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        local_proxy.daemon_threads = True
        address = ("127.0.0.1", local_proxy.server_port)
        proxies.add(address)
        thread = threading.Thread(target=local_proxy.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        try:
            yield f"http://test-user:disposable-password@127.0.0.1:{local_proxy.server_port}", connections
        finally:
            local_proxy.shutdown(); local_proxy.server_close(); thread.join(timeout=2)
            proxies.remove(address)
            assert not thread.is_alive()
    try:
        yield records, destinations, ca, proxy
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=2)
        assert not worker.is_alive()


def test_actual_tls_chain_hostname_and_raw_streaming(real_tls):
    records, destinations, _, _ = real_tls
    assert HttpsTransport((HOST,)).fetch_bytes(URL, 4) == b"abcd"
    assert destinations == [(HOST, 443)]
    headers = {key.lower(): value for key, value in records[0][1].items()}
    assert headers["accept-encoding"] == "identity"
    assert "authorization" not in headers and "cookie" not in headers


def test_actual_https_redirect_clears_cookie(real_tls):
    records, _, _, _ = real_tls
    assert HttpsTransport((HOST,)).fetch_bytes(f"https://{HOST}/redirect", 4) == b"abcd"
    assert [path for path, _ in records] == ["/redirect", "/package"]
    assert all("cookie" not in {name.lower() for name in headers} for _, headers in records)


def test_unapproved_https_redirect_never_connects(real_tls):
    _, destinations, _, _ = real_tls
    with pytest.raises(UpdateError, match="UPDATE_URL_INVALID"):
        HttpsTransport((HOST,)).fetch_bytes(f"https://{HOST}/foreign", 4)
    assert destinations == [(HOST, 443)]


def test_actual_untrusted_tls_certificate_is_not_accepted(real_tls, monkeypatch):
    records, _, _, _ = real_tls
    monkeypatch.delenv("SSL_CERT_FILE")
    with pytest.raises(UpdateError, match="UPDATE_TRANSFER_FAILED") as raised:
        HttpsTransport((HOST,)).fetch_bytes(URL, 4)
    assert not records  # TLS handshake failed before any HTTP request.
    assert str(real_tls[2]) not in "".join(traceback.format_exception(raised.value))


def test_actual_tls_hostname_mismatch_is_not_accepted(real_tls):
    records, destinations, _, _ = real_tls
    with pytest.raises(UpdateError, match="UPDATE_TRANSFER_FAILED"):
        HttpsTransport(("alternate.example.test",)).fetch_bytes("https://alternate.example.test/package", 4)
    assert destinations == [("alternate.example.test", 443)] and not records


def test_actual_socket_interruption_never_returns_partial_success(real_tls):
    output = io.BytesIO()
    with pytest.raises(UpdateError, match="UPDATE_TRANSFER_FAILED"):
        HttpsTransport((HOST,)).receive(f"https://{HOST}/cut", 10, output)
    assert output.getvalue() == b"ab"


def test_actual_slow_socket_obeys_total_deadline(real_tls):
    with pytest.raises(UpdateError, match="UPDATE_DOWNLOAD_TIMEOUT"):
        HttpsTransport((HOST,), deadline=.05, timeout=1).fetch_bytes(f"https://{HOST}/slow", 4)


def test_actual_stream_cancellation_does_not_return_success(real_tls):
    checks = iter((False, False, True))
    with pytest.raises(UpdateError, match="UPDATE_CANCELLED"):
        HttpsTransport((HOST,)).fetch_bytes(URL, 4, cancelled=lambda: next(checks))


def test_actual_https_proxy_connect_keeps_credentials_out_of_release_request(real_tls, monkeypatch):
    records, _, _, proxy = real_tls
    with proxy() as (url, connections):
        monkeypatch.setenv("HTTPS_PROXY", url)
        assert HttpsTransport((HOST,)).fetch_bytes(URL, 4) == b"abcd"
        assert len(connections) == 1 and connections[0][1].startswith("Basic ")
        assert "proxy-authorization" not in {key.lower() for key in records[0][1]}


def test_actual_proxy_rejection_does_not_expose_credentials(real_tls, monkeypatch):
    records, _, _, proxy = real_tls
    with proxy(reject=True) as (url, connections):
        monkeypatch.setenv("HTTPS_PROXY", url)
        with pytest.raises(UpdateError, match="UPDATE_TRANSFER_FAILED") as raised:
            HttpsTransport((HOST,)).fetch_bytes(URL, 4)
        message = "".join(traceback.format_exception(raised.value))
        assert "disposable-password" not in message and "private proxy diagnostic" not in message
        assert connections and not records
