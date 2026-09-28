"""Shared cancellable HTTP JSON/SSE transport for provider adapters."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Generator

from backend.core.unicode_safety import normalize_unicode_value


@dataclass
class ProviderHttpError(RuntimeError):
    status_code: int
    body: str
    headers: dict

    def __str__(self) -> str:
        return f"Provider HTTP {self.status_code}: {self.body[:500]}"


class ProviderTransportCancelled(RuntimeError):
    pass


class HttpProviderTransport:
    def post_json(
        self, url: str, body: dict, headers: dict, *, timeout: int,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        request = self._request(url, body, headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                done = self._close_on_cancel(response, cancel_event)
                try:
                    payload = response.read().decode("utf-8")
                finally:
                    done.set()
                if cancel_event is not None and cancel_event.is_set():
                    raise ProviderTransportCancelled()
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise ProviderHttpError(
                exc.code, body_text, dict(exc.headers.items()),
            ) from exc
        except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderTransportCancelled() from exc
            raise RuntimeError(f"Provider connection failed: {exc}") from exc

    def stream_sse(
        self, url: str, body: dict, headers: dict, *, timeout: int,
        cancel_event: threading.Event | None = None,
    ) -> Generator[tuple[str, dict], None, None]:
        request = self._request(url, body, headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                done = self._close_on_cancel(response, cancel_event)
                event_name = "message"
                data_lines: list[str] = []
                # Socket read timeouts are reset by transport-level heartbeats.
                # Providers can therefore keep a semantically dead stream open
                # forever with ``: keep-alive`` comments.  Only actual SSE data
                # extends the useful-idle deadline; long reasoning/tool streams
                # remain valid as long as they keep producing model events.
                last_semantic_at = time.monotonic()
                try:
                    for raw_line in response:
                        if cancel_event is not None and cancel_event.is_set():
                            raise ProviderTransportCancelled()
                        if timeout > 0 and time.monotonic() - last_semantic_at >= timeout:
                            raise socket.timeout(
                                f"provider stream produced no semantic SSE data for {timeout}s"
                            )
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                        if not line:
                            if data_lines:
                                payload = "\n".join(data_lines)
                                data_lines.clear()
                                if payload == "[DONE]":
                                    return
                                yield event_name, json.loads(payload)
                            event_name = "message"
                            continue
                        if line.startswith(":"):
                            continue
                        if line.startswith("event:"):
                            event_name = line[6:].strip() or "message"
                        elif line.startswith("data:"):
                            last_semantic_at = time.monotonic()
                            data_lines.append(line[5:].lstrip())
                    if data_lines:
                        payload = "\n".join(data_lines)
                        if payload != "[DONE]":
                            yield event_name, json.loads(payload)
                finally:
                    done.set()
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise ProviderHttpError(
                exc.code, body_text, dict(exc.headers.items()),
            ) from exc
        except ProviderTransportCancelled:
            raise
        except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderTransportCancelled() from exc
            raise RuntimeError(f"Provider stream failed: {exc}") from exc
        except Exception as exc:
            # Closing a live Windows http.client response from the cancellation
            # watcher can surface implementation-specific read errors (for
            # example AttributeError from a cleared chunked reader) rather than
            # OSError.  Only normalize them when cancellation is already an
            # observed host fact; unrelated programming errors still propagate.
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderTransportCancelled() from exc
            raise

    @staticmethod
    def _request(url: str, body: dict, headers: dict) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            data=json.dumps(
                normalize_unicode_value(body), ensure_ascii=False,
            ).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )

    @staticmethod
    def _close_on_cancel(response, cancel_event: threading.Event | None) -> threading.Event:
        done = threading.Event()
        if cancel_event is None:
            return done

        def watch() -> None:
            while not done.is_set():
                if cancel_event.wait(0.1):
                    try:
                        response.close()
                    except Exception:
                        pass
                    return

        threading.Thread(target=watch, daemon=True).start()
        return done
