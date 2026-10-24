"""HTTPS whole-response transport with explicit hosts and bounded raw streaming."""

from __future__ import annotations

import io
import math
import re
import time
from typing import BinaryIO, Callable
from urllib.parse import unquote, urljoin, urlsplit

from .checker import UpdatePolicy
from .validation import UpdateError, https_url, number, reject, url_text


CHUNK_BYTES = 64 * 1024


class HttpsTransport:
    """No requests until explicitly called; injected HTTPX transport is for tests.

    A fresh TLS-verifying client avoids ambient application cookies/authentication.
    Environment proxy and CA settings are supported, but errors never echo them.
    No range/resume requests: incomplete responses are discarded by the stager.
    """

    def __init__(self, allowed_hosts: tuple[str, ...], *, timeout: float = 15,
                 deadline: float = 600, max_redirects: int = 3, transport=None):
        UpdatePolicy(allowed_hosts=allowed_hosts)
        for value in (timeout, deadline):
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not 0 < value <= 900):
                reject("UPDATE_TRANSPORT_INVALID", "Timeouts must be finite and bounded")
        number(max_redirects, minimum=0, maximum=5)
        self.allowed_hosts = allowed_hosts
        self.timeout = timeout
        self.deadline = deadline
        self.max_redirects = max_redirects
        self._transport = transport

    def fetch_bytes(self, url: str, maximum: int, *, cancelled: Callable[[], bool] = lambda: False) -> bytes:
        output = io.BytesIO()
        self.receive(url, maximum, output, cancelled=cancelled)
        return output.getvalue()

    def receive(self, url: str, maximum: int, output: BinaryIO, *,
                expected_size: int | None = None,
                cancelled: Callable[[], bool] = lambda: False) -> int:
        url = https_url(url, self.allowed_hosts)
        number(maximum, maximum=2 * 1024**3)
        if expected_size is not None:
            number(expected_size, maximum=maximum)
        started = time.monotonic()

        def checkpoint():
            if cancelled():
                reject("UPDATE_CANCELLED", "Update preparation was cancelled")
            if time.monotonic() - started >= self.deadline:
                reject("UPDATE_DOWNLOAD_TIMEOUT", "Response exceeded the transfer deadline")

        try:
            import httpx

            with httpx.Client(verify=True, follow_redirects=False, trust_env=True,
                              timeout=self.timeout, transport=self._transport) as client:
                for hop in range(self.max_redirects + 1):
                    checkpoint()
                    client.cookies.clear()
                    with client.stream("GET", url, headers={"Accept-Encoding": "identity",
                                                           "User-Agent": "Gitgo-Updater/0.1"}) as response:
                        checkpoint()
                        if response.status_code in (301, 302, 303, 307, 308):
                            if hop == self.max_redirects:
                                reject("UPDATE_REDIRECT_INVALID", "Too many release redirects")
                            location = response.headers.get("location", "")
                            url_text(location)
                            # Validate before urljoin can normalize away traversal.
                            path = unquote(urlsplit(location).path, errors="strict")
                            if any(part in (".", "..") for part in path.split("/")):
                                reject("UPDATE_REDIRECT_INVALID", "Unsafe redirect path")
                            # Approved CDNs issue short-lived signed query
                            # parameters. They are transport redirects only:
                            # configured/signed starting URLs stay canonical.
                            url = https_url(urljoin(url, location), self.allowed_hosts, allow_query=True)
                            continue
                        if response.status_code != 200:
                            reject("UPDATE_HTTP_FAILED", "Release server did not return a full response")
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            reject("UPDATE_HTTP_ENCODING", "Encoded HTTP responses are not supported")
                        length = response.headers.get("content-length")
                        declared = None
                        if length is not None:
                            if re.fullmatch(r"[0-9]{1,12}", length) is None:
                                reject("UPDATE_DOWNLOAD_SIZE", "Invalid HTTP response length")
                            declared = int(length)
                            if not 0 < declared <= maximum or (expected_size is not None and declared != expected_size):
                                reject("UPDATE_DOWNLOAD_SIZE", "HTTP length exceeds or differs from the permitted size")
                        received = 0
                        # Do not ask HTTPX to buffer to a fixed chunk size: a
                        # slow trickle must still reach deadline checkpoints.
                        for chunk in response.iter_raw():
                            checkpoint()
                            received += len(chunk)
                            if received > maximum or (declared is not None and received > declared):
                                reject("UPDATE_DOWNLOAD_SIZE", "Response exceeded the permitted size")
                            if output.write(chunk) != len(chunk):
                                reject("UPDATE_WRITE_FAILED", "Could not write the complete response chunk")
                        checkpoint()
                        if (received == 0 or (declared is not None and received != declared)
                                or (expected_size is not None and received != expected_size)):
                            reject("UPDATE_DOWNLOAD_SIZE", "Response was incomplete")
                        return received
        except UpdateError:
            raise
        except Exception:
            # Suppress both cause and context in formatted exceptions (proxy secrets).
            raise UpdateError("UPDATE_TRANSFER_FAILED", "Release transfer failed; current installation unchanged") from None
        reject("UPDATE_REDIRECT_INVALID", "Release redirect did not complete")
