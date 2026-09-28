from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.core.tools.web_tools import web_fetch, web_search
from backend.core.tools.catalog import build_workspace_tools
from backend.core.loop.operation_policy import PolicyDisposition, decide_tool_operation
from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType
from backend.core.application import ApplicationServices


def test_web_search_without_provider_returns_recoverable_catalog_error(monkeypatch):
    monkeypatch.delenv("GITGO_SEARCH_ENDPOINT", raising=False)

    result = web_search({"query": "gitgo", "_web_search_mode": "searxng"})

    assert result["error"] == "WEB_SEARCH_PROVIDER_NOT_CONFIGURED"
    assert result["error_info"]["catalog_id"] == "GITGO-E3403"
    assert result["error_info"]["next_actions"][0]["action"] == "configure_search_provider"


def test_public_search_and_fetch_are_low_risk_observations(tmp_path_factory):
    catalog = build_workspace_tools(tmp_path_factory)

    for name in ("web_search", "web_fetch"):
        decision = decide_tool_operation(catalog[name], {})
        assert decision.disposition == PolicyDisposition.ALLOW
        assert catalog[name].read_only is True
        assert catalog[name].effect.value == "external_read"


def test_web_fetch_blocks_loopback_before_transport():
    result = web_fetch({"url": "http://127.0.0.1/private"})

    assert result["error"] == "WEB_FETCH_BLOCKED_URL"
    assert "private" in result["error_info"]["message"]


def test_web_fetch_returns_bounded_plain_text(monkeypatch):
    monkeypatch.setattr(
        "backend.core.tools.web_tools.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )

    class Response:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        encoding = "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_bytes(self):
            yield b"<html><script>secret()</script><body><h1>Weather</h1><p>Clear, 23 C</p></body></html>"

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, method, url):
            assert method == "GET"
            assert url == "https://example.com/weather"
            return Response()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=Client))
    result = web_fetch({"url": "https://example.com/weather"})

    assert result["provider"] == "anonymous-http"
    assert result["content"] == "Weather\nClear, 23 C"
    assert "secret" not in result["content"]


def test_web_fetch_rejects_http_error_pages_as_evidence(monkeypatch):
    monkeypatch.setattr(
        "backend.core.tools.web_tools.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )

    class Response:
        status_code = 403
        headers = {"content-type": "text/html; charset=utf-8"}
        encoding = "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, _method, _url):
            return Response()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=Client))
    result = web_fetch({"url": "https://example.com/challenge"})

    assert result["error"] == "WEB_FETCH_FAILED"
    assert result["error_info"]["details"]["failure_type"] == "_ProviderProtocolError"
    assert "HTTP 403" in result["error_info"]["details"]["diagnostic"]


def test_web_search_normalizes_bounded_provider_results(monkeypatch):
    monkeypatch.setenv("GITGO_SEARCH_ENDPOINT", "https://search.invalid/search")
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [
                {"title": "One", "url": "https://example.test/1", "content": "First",
                 "publishedDate": "2026-01-01", "engines": ["fixture"]},
                {"title": "No URL", "content": "discard"},
                {"title": "Two", "url": "https://example.test/2", "content": "Second"},
            ]}

    def get(endpoint, **kwargs):
        captured.update(endpoint=endpoint, **kwargs)
        return Response()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=get))
    result = web_search({"query": "  gitgo tools  ", "max_results": 2, "language": "zh",
                         "_web_search_mode": "searxng"})

    assert result["count"] == 2
    assert result["results"][0]["title"] == "One"
    assert result["results"][1]["title"] == "Two"
    assert captured["endpoint"] == "https://search.invalid/search"
    assert captured["params"] == {
        "q": "gitgo tools", "format": "json", "language": "zh",
        "engines": "duckduckgo",
    }
    assert captured["follow_redirects"] is True
    assert result["provider_reachable"] is True
    assert result["empty_reason"] == ""


def test_web_search_requires_a_nonempty_query(monkeypatch):
    monkeypatch.setenv("GITGO_SEARCH_ENDPOINT", "https://search.invalid/search")

    result = web_search({"query": "  ", "_web_search_mode": "searxng"})

    assert result["error"] == "WEB_SEARCH_QUERY_REQUIRED"
    assert result["error_info"]["catalog_id"] == "GITGO-E3404"


def test_web_search_prefers_host_runtime_endpoint_and_distinguishes_empty(monkeypatch):
    monkeypatch.setenv("GITGO_SEARCH_ENDPOINT", "https://wrong.invalid/search")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    captured = {}
    def get(endpoint, **kwargs):
        captured["endpoint"] = endpoint
        return Response()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=get))
    result = web_search({
        "_web_search_endpoint": "https://configured.invalid/search",
        "_web_search_mode": "searxng",
        "query": "rare query",
    })

    assert captured["endpoint"] == "https://configured.invalid/search"
    assert result["count"] == 0
    assert result["provider_reachable"] is True
    assert result["empty_reason"] == "provider_returned_no_results"


def test_web_search_transport_failure_is_structured_and_redacts_credentials(monkeypatch):
    class TransportError(RuntimeError):
        pass

    def get(_endpoint, **_kwargs):
        raise TransportError("network unavailable")

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=get))
    result = web_search({
        "_web_search_endpoint": "https://secret:user@example.invalid/search?token=private",  # gitgo-ignore-sensitive
        "_web_search_mode": "searxng",
        "query": "gitgo",
    })

    assert result["error"] == "WEB_SEARCH_FAILED"
    assert result["error_info"]["catalog_id"] == "GITGO-E3406"
    assert result["error_info"]["details"]["endpoint"] == "https://example.invalid/search"
    assert "private" not in str(result)


def test_web_search_identifies_html_challenge_as_provider_contract_failure(monkeypatch):
    class Response:
        headers = {"content-type": "text/html; charset=utf-8"}
        text = "<html><title>Making sure you're not a bot!</title></html>"

        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("not json")

    monkeypatch.setitem(
        sys.modules, "httpx", SimpleNamespace(get=lambda *_args, **_kwargs: Response()),
    )
    result = web_search({
        "_web_search_endpoint": "https://search.example.test/search",
        "_web_search_mode": "searxng",
        "query": "gitgo",
    })

    assert result["error"] == "WEB_SEARCH_FAILED"
    details = result["error_info"]["details"]
    assert details["failure_type"] == "_ProviderProtocolError"
    assert details["provider_contract"] == "searxng-json"
    assert details["content_type"] == "text/html; charset=utf-8"
    assert details["challenge_detected"] is True
    assert result["error_info"]["next_actions"][1]["action"] == "choose_another_search_endpoint"


def test_saved_web_search_endpoint_probe_uses_production_adapter(monkeypatch):
    monkeypatch.setattr(
        "backend.core.application.services.ConfigManager.load",
        lambda: SimpleNamespace(
            web_search_endpoint="https://search.example.test/search",
            web_search_engine="bing",
        ),
    )
    captured = {}

    def probe(arguments):
        captured.update(arguments)
        return {"provider_reachable": True, "count": 1, "results": [{"title": "ok"}]}

    monkeypatch.setattr("backend.core.tools.web_tools.web_search", probe)
    result = ApplicationServices().config_web_search_test()

    assert result["ok"] is True
    assert result["reachable"] is True
    assert captured["_web_search_endpoint"] == "https://search.example.test/search"
    assert captured["_web_search_engine"] == "bing"


def test_web_search_engine_is_host_bound_to_searxng_transport(monkeypatch):
    monkeypatch.setenv("GITGO_SEARCH_ENDPOINT", "https://search.invalid/search")
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    def get(_endpoint, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=get))
    result = web_search({
        "query": "gitgo", "_web_search_engine": "google", "_web_search_mode": "searxng",
    })

    assert captured["params"]["engines"] == "google"
    assert result["engine"] == "google"


@pytest.mark.parametrize(
    "engine",
    ["google", "bing", "baidu", "yandex", "duckduckgo"],
)
def test_each_configured_engine_reaches_the_real_searxng_http_adapter(
    monkeypatch, engine,
):
    """Exercise transport, query encoding and response parsing over real HTTP.

    Public SearXNG instances are deliberately unsuitable for a deterministic
    test suite.  This loopback server implements the same JSON boundary so the
    acceptance covers the production httpx path rather than a mocked function.
    """
    received: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib callback name
            received.append(parse_qs(urlsplit(self.path).query))
            body = json.dumps({
                "results": [{
                    "title": f"{engine} result",
                    "url": "https://example.test/result",
                    "content": "loopback SearXNG fixture",
                    "engines": [engine],
                }],
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.delenv("GITGO_SEARCH_ENDPOINT", raising=False)
    try:
        result = web_search({
            "_web_search_endpoint": (
                f"http://127.0.0.1:{server.server_port}/search"
            ),
            "_web_search_engine": engine,
            "_web_search_mode": "searxng",
            "query": "gitgo network acceptance",
            "language": "en",
        })
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert result["provider_reachable"] is True
    assert result["count"] == 1
    assert result["engine"] == engine
    assert received == [{
        "q": ["gitgo network acceptance"],
        "format": ["json"],
        "language": ["en"],
        "engines": [engine],
    }]


def test_provider_mode_uses_hosted_search_and_returns_grounded_answer(monkeypatch):
    configured = SimpleNamespace(
        id="provider-1", base_url="https://provider.invalid", api_key="secret",
        model_id="model", protocol="openai_responses", max_output_tokens=4096,
        runtime_capabilities=lambda: {"hosted_web_search": True},
    )
    monkeypatch.setattr(
        "backend.core.llm_config.LLMConfigManager.get_providers", lambda: [configured],
    )
    monkeypatch.setattr(
        "backend.core.llm_config.LLMConfigManager.get_active", lambda: configured,
    )

    class Provider:
        def __init__(self, *_args, **_kwargs):
            pass

        def stream_events(self, *_args, **kwargs):
            assert kwargs["metadata"] == {
                "hosted_web_search": True, "web_search_mode": "provider",
            }
            yield ProviderEvent(ProviderEventType.SERVER_TOOL_STARTED, tool_call_id="ws")
            yield ProviderEvent(ProviderEventType.SERVER_TOOL_RESULT, tool_call_id="ws")
            yield ProviderEvent(
                ProviderEventType.TEXT_DELTA,
                text="Current result: 20 C. https://weather.example/current",
            )
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)

    monkeypatch.setattr("backend.core.loop.llm.LLMProvider", Provider)
    result = web_search({
        "query": "current weather", "_web_search_mode": "provider",
        "_web_search_provider_id": "provider-1",
    })
    assert result["provider"] == "provider-hosted"
    assert result["answer"].startswith("Current result")
    assert result["sources"] == ["https://weather.example/current"]


def test_provider_mode_reports_probed_capability_difference_without_network(monkeypatch):
    configured = SimpleNamespace(
        id="mimo", base_url="https://provider.invalid", api_key="secret",
        model_id="mimo-v2.5", protocol="openai_responses", max_output_tokens=4096,
        capabilities={
            "hosted_web_search": False,
            "probed_at": "2026-09-27T00:00:00Z",
        },
        runtime_capabilities=lambda: {"hosted_web_search": False},
    )
    monkeypatch.setattr(
        "backend.core.llm_config.LLMConfigManager.get_providers", lambda: [configured],
    )
    monkeypatch.setattr(
        "backend.core.llm_config.LLMConfigManager.get_active", lambda: configured,
    )

    result = web_search({
        "query": "current travel information",
        "_web_search_mode": "provider",
        "_web_search_provider_id": "mimo",
    })

    assert result["error"] == "PROVIDER_CAPABILITY_UNAVAILABLE"
    assert result["error_info"]["retryable"] is False
    assert result["error_info"]["details"]["model_id"] == "mimo-v2.5"
    assert [item["action"] for item in result["error_info"]["next_actions"]] == [
        "switch_provider", "configure_searxng", "continue_offline",
    ]


def test_deepseek_chat_provider_uses_official_anthropic_native_search(monkeypatch):
    configured = SimpleNamespace(
        id="deepseek-1", base_url="https://api.deepseek.com",
        api_key="secret", model_id="deepseek-flash", protocol="openai_chat",
        max_output_tokens=4096,
    )
    monkeypatch.setattr(
        "backend.core.llm_config.LLMConfigManager.get_providers", lambda: [configured],
    )
    monkeypatch.setattr(
        "backend.core.llm_config.LLMConfigManager.get_active", lambda: configured,
    )
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [
                {
                    "type": "text",
                    "text": "I will search the web now.",
                },
                {
                    "type": "web_search_tool_result",
                    "content": [{
                        "type": "web_search_result",
                        "url": "https://weather.example/la",
                        "title": "Los Angeles weather",
                        "page_age": "2026-09-19",
                    }],
                },
                {
                    "type": "text",
                    "text": "Los Angeles is clear and 23 C. https://weather.example/la",
                    "citations": [{
                        "url": "https://weather.example/la",
                        "cited_text": "Clear, 23 C",
                    }],
                },
            ]}

    def post(endpoint, **kwargs):
        captured.update(endpoint=endpoint, **kwargs)
        return Response()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(post=post))
    result = web_search({
        "query": "Los Angeles weather",
        "_web_search_mode": "provider",
        "_web_search_provider_id": "deepseek-1",
    })

    assert captured["endpoint"] == "https://api.deepseek.com/anthropic/v1/messages"
    assert captured["json"]["tools"] == [{
        "type": "web_search_20250305", "name": "web_search", "max_uses": 5,
    }]
    assert captured["follow_redirects"] is False
    assert result == {
        "query": "Los Angeles weather",
        "content": "Los Angeles is clear and 23 C. https://weather.example/la",
        "content_is_provider_generated": True,
        "results": [{
            "url": "https://weather.example/la",
            "title": "Los Angeles weather",
            "snippet": "Clear, 23 C",
            "published": "2026-09-19",
        }],
        "sources": ["https://weather.example/la"],
        "count": 1,
        "provider": "deepseek-official",
        "provider_id": "deepseek-1",
        "provider_reachable": True,
        "empty_reason": "",
    }
