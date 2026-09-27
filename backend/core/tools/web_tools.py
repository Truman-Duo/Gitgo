"""Provider-neutral public web search and bounded page retrieval."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from backend.core.errors import error_payload


class _ProviderProtocolError(ValueError):
    """The endpoint answered, but not with the configured provider contract."""

    def __init__(self, message: str, *, content_type: str = "", challenge: bool = False):
        super().__init__(message)
        self.content_type = content_type
        self.challenge = challenge


_FETCH_MAX_URL = 2048
_FETCH_MAX_BYTES = 2 * 1024 * 1024
_FETCH_MAX_CHARS = 100_000
_FETCH_MAX_REDIRECTS = 5
_FETCH_CONTENT_TYPES = {
    "application/json", "application/xml", "application/xhtml+xml",
}


class _PublicTextExtractor(HTMLParser):
    """Small dependency-free HTML-to-text boundary for model consumption."""

    _SKIP = {"script", "style", "noscript", "svg", "canvas", "template"}
    _BREAK = {
        "article", "aside", "blockquote", "br", "div", "footer", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "li", "main", "nav", "p", "pre",
        "section", "table", "td", "th", "tr", "ul", "ol",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, _attrs) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
        elif not self._skip_depth and tag in self._BREAK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif not self._skip_depth and tag in self._BREAK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data:
            self._parts.append(data)

    def text(self) -> str:
        lines = []
        for raw in "".join(self._parts).splitlines():
            line = re.sub(r"[\t\x0b\x0c\r ]+", " ", raw).strip()
            if line and (not lines or line != lines[-1]):
                lines.append(line)
        return "\n".join(lines)


def _validated_public_url(raw: str) -> str:
    value = str(raw or "").strip()
    if not value or len(value) > _FETCH_MAX_URL:
        raise ValueError("URL is empty or exceeds the public fetch length limit")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only absolute HTTP(S) URLs are supported")
    if parsed.username or parsed.password:
        raise ValueError("credentials in URLs are not allowed")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("URL contains an invalid port") from exc
    if port not in {80, 443}:
        raise ValueError("public fetch permits only ports 80 and 443")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ValueError("local hostnames are blocked")
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise ValueError("hostname did not resolve") from exc
    if not addresses:
        raise ValueError("hostname did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise ValueError("private, local, reserved and multicast addresses are blocked")
    return value


def web_fetch(args: dict) -> dict:
    """Fetch one anonymous public text page with strict transport bounds.

    This follows the shared search/fetch seam used by current agent harnesses:
    search discovers citeable URLs; fetch materializes selected evidence.  It
    sends no cookies or ambient credentials and revalidates every redirect.
    """
    requested = str(args.get("url") or "").strip()
    try:
        current = _validated_public_url(requested)
    except ValueError as exc:
        return error_payload(
            "WEB_FETCH_BLOCKED_URL", message=str(exc),
            details={"url": _public_endpoint(requested)},
            next_actions=[{"action": "choose_public_http_url"}],
        )
    try:
        import httpx
        with httpx.Client(
            follow_redirects=False, timeout=30, trust_env=False,
            headers={
                "User-Agent": "Gitgo/1.0",
                "Accept": "text/html,application/xhtml+xml,text/*;q=0.9,application/json;q=0.8",
            },
        ) as client:
            for redirect_count in range(_FETCH_MAX_REDIRECTS + 1):
                with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = str(response.headers.get("location") or "").strip()
                        if not location or redirect_count >= _FETCH_MAX_REDIRECTS:
                            raise _ProviderProtocolError("redirect limit exceeded or Location missing")
                        current = _validated_public_url(urljoin(current, location))
                        continue
                    content_type = str(response.headers.get("content-type") or "").lower()
                    if response.status_code >= 400:
                        # A CDN challenge/error document is not source
                        # evidence.  Treating its HTML as a successful fetch
                        # encouraged models to cite transport failures in the
                        # final answer and polluted downstream summaries.
                        raise _ProviderProtocolError(
                            f"public endpoint returned HTTP {response.status_code}",
                            content_type=content_type,
                            challenge=response.status_code in {401, 403, 407, 429},
                        )
                    mime = content_type.split(";", 1)[0].strip()
                    if not (mime.startswith("text/") or mime in _FETCH_CONTENT_TYPES or mime.endswith(("+json", "+xml"))):
                        raise _ProviderProtocolError(
                            f"unsupported public content type: {mime or 'unknown'}",
                            content_type=content_type,
                        )
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > _FETCH_MAX_BYTES:
                        raise _ProviderProtocolError("response exceeds the public fetch byte limit")
                    chunks: list[bytes] = []
                    total = 0
                    truncated_bytes = False
                    for chunk in response.iter_bytes():
                        remaining = _FETCH_MAX_BYTES - total
                        if len(chunk) > remaining:
                            chunks.append(chunk[:remaining])
                            truncated_bytes = True
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                    raw = b"".join(chunks)
                    encoding = response.encoding or "utf-8"
                    decoded = raw.decode(encoding, errors="replace")
                    if mime in {"text/html", "application/xhtml+xml"}:
                        parser = _PublicTextExtractor()
                        parser.feed(decoded)
                        content = parser.text()
                        kind = "html"
                    else:
                        content = decoded
                        kind = "text"
                    truncated_chars = len(content) > _FETCH_MAX_CHARS
                    if truncated_chars:
                        content = content[:_FETCH_MAX_CHARS]
                    return {
                        "url": current,
                        "status_code": response.status_code,
                        "content_type": mime,
                        "body": {"kind": kind, "content": content},
                        "content": content,
                        "truncated": truncated_bytes or truncated_chars,
                        "bytes": len(raw),
                        "provider": "anonymous-http",
                    }
        raise _ProviderProtocolError("redirect resolution did not produce a response")
    except Exception as exc:
        return error_payload(
            "WEB_FETCH_FAILED",
            message="The public page could not be retrieved safely.",
            details={
                "url": _public_endpoint(current),
                "failure_type": type(exc).__name__,
                "diagnostic": str(exc)[:400],
                "content_type": str(getattr(exc, "content_type", "") or ""),
            },
            next_actions=[
                {"action": "retry"}, {"action": "choose_another_source"},
                {"action": "continue_with_search_evidence"},
            ],
        )


def web_search(args: dict) -> dict:
    mode = str(args.get("_web_search_mode") or "auto").strip().lower()
    if mode == "disabled":
        return error_payload(
            "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
            message="Web search is disabled in Gitgo configuration.",
            details={"mode": "disabled"},
            next_actions=[{"action": "enable_web_search"}, {"action": "continue_offline"}],
        )
    if mode in {"auto", "provider"}:
        provider_result = _provider_hosted_search(args)
        if not provider_result.get("error"):
            return provider_result
        if mode == "provider":
            return provider_result

    endpoint = str(
        args.get("_web_search_endpoint")
        or os.getenv("GITGO_SEARCH_ENDPOINT", "")
    ).strip()
    if not endpoint:
        return error_payload(
            "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
            details={"provider_contract": "searxng-compatible"},
            next_actions=[{"action": "configure_search_provider"},
                          {"action": "continue_offline"}],
        )
    query = str(args.get("query") or "").strip()
    if not query:
        return error_payload(
            "WEB_SEARCH_QUERY_REQUIRED",
            next_actions=[{"action": "retry", "required": ["query"]}],
        )
    limit = max(1, min(int(args.get("max_results", 8) or 8), 20))
    engine = str(args.get("_web_search_engine") or "duckduckgo").strip().lower()
    if engine not in {"google", "bing", "baidu", "yandex", "duckduckgo"}:
        return error_payload(
            "WEB_SEARCH_ENGINE_INVALID",
            details={"engine": engine},
            next_actions=[{"action": "configure_search_provider"}],
        )
    public_endpoint = _public_endpoint(endpoint)
    try:
        import httpx
        response = httpx.get(
            endpoint,
            params={
                "q": query, "format": "json",
                "language": str(args.get("language") or "auto"),
                # Gitgo keeps one provider-neutral SearXNG transport. Engine
                # choice is a stable policy field, not a model-controlled URL.
                "engines": engine,
            },
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "Gitgo/1.0"},
        )
        response.raise_for_status()
        headers = getattr(response, "headers", {}) or {}
        content_type = str(headers.get("content-type") or "").lower()
        try:
            payload = response.json()
        except Exception as exc:
            preview = str(getattr(response, "text", "") or "")[:2048].lower()
            challenge = any(marker in preview for marker in (
                "making sure you're not a bot",
                "captcha",
                "cf-chl-",
                "cloudflare challenge",
                "access denied",
            ))
            raise _ProviderProtocolError(
                "provider returned an anti-bot challenge instead of SearXNG JSON"
                if challenge else "provider response is not SearXNG JSON",
                content_type=content_type,
                challenge=challenge,
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
            raise _ProviderProtocolError(
                "provider JSON does not contain a results list",
                content_type=content_type,
            )
    except Exception as exc:
        return error_payload(
            "WEB_SEARCH_FAILED",
            message="The configured web search provider request failed.",
            details={
                "endpoint": public_endpoint,
                "failure_type": type(exc).__name__,
                "provider_contract": "searxng-json",
                "content_type": str(getattr(exc, "content_type", "") or ""),
                "challenge_detected": bool(getattr(exc, "challenge", False)),
                "diagnostic": (
                    f"HTTP {getattr(getattr(exc, 'response', None), 'status_code', 'error')}"
                    if getattr(exc, "response", None) is not None
                    else f"{type(exc).__name__}: provider request did not produce valid results"
                ),
            },
            next_actions=[
                {"action": "retry"},
                {"action": "choose_another_search_endpoint"},
                {"action": "inspect_search_provider", "endpoint": public_endpoint},
                {"action": "continue_offline"},
            ],
        )
    results = []
    for item in list(payload.get("results") or []):
        if not isinstance(item, dict) or not item.get("url"):
            continue
        results.append({
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "snippet": str(item.get("content") or ""),
            "published": str(item.get("publishedDate") or ""),
            "engines": list(item.get("engines") or []),
        })
        if len(results) >= limit:
            break
    return {
        "query": query,
        "results": results,
        "count": len(results),
        "provider": "searxng-compatible",
        "engine": engine,
        "provider_reachable": True,
        "empty_reason": "provider_returned_no_results" if not results else "",
    }


def _provider_hosted_search(args: dict) -> dict:
    """Execute vendor-hosted search behind Gitgo's approval boundary.

    Provider server tools normally execute inside a model request and cannot be
    intercepted after tool selection. Gitgo therefore exposes one client-side
    SearchBroker function, suspends it for approval, then performs this bounded
    provider request after the grant. SearXNG remains an explicit fallback.
    """
    query = str(args.get("query") or "").strip()
    if not query:
        return error_payload(
            "WEB_SEARCH_QUERY_REQUIRED",
            next_actions=[{"action": "retry", "required": ["query"]}],
        )
    try:
        from backend.core.llm_config import LLMConfigManager
        providers = LLMConfigManager.get_providers()
        requested_id = str(args.get("_web_search_provider_id") or "")
        configured = next((item for item in providers if item.id == requested_id), None)
        configured = configured or LLMConfigManager.get_active()
        if configured is None:
            raise RuntimeError("no active LLM provider is configured")
        if _is_deepseek_official(configured):
            return _deepseek_hosted_search(configured, query, args)
        configured_capabilities = dict(getattr(configured, "capabilities", {}) or {})
        if not configured_capabilities:
            runtime_capabilities = configured.runtime_capabilities()
            configured_capabilities = (
                runtime_capabilities.to_dict()
                if hasattr(runtime_capabilities, "to_dict")
                else dict(runtime_capabilities or {})
            )
        if (
            configured_capabilities.get("probed_at")
            and not bool(configured_capabilities.get("hosted_web_search", False))
        ):
            return error_payload(
                "PROVIDER_CAPABILITY_UNAVAILABLE",
                message=(
                    f"Model {configured.model_id} does not expose hosted web search "
                    "under the currently probed API capability or plan."
                ),
                details={
                    "capability": "hosted_web_search",
                    "model_id": configured.model_id,
                    "protocol": configured.protocol,
                    "possible_provider_plan_difference": True,
                    "searxng_configured": bool(str(
                        args.get("_web_search_endpoint") or ""
                    ).strip()),
                },
                next_actions=[
                    {
                        "action": "switch_provider",
                        "effect": "Use a configured provider whose probe reports hosted search.",
                    },
                    {
                        "action": "configure_searxng",
                        "effect": "Use Gitgo's provider-neutral SearXNG fallback.",
                    },
                    {
                        "action": "continue_offline",
                        "effect": "Answer without current web evidence and disclose freshness limits.",
                    },
                ],
            )
        if configured.protocol not in {"openai_responses", "anthropic_messages"}:
            raise RuntimeError(
                f"provider protocol {configured.protocol} has no hosted-search adapter"
            )
        from backend.core.loop.llm import LLMProvider as RuntimeProvider
        from backend.core.loop.provider_protocol import ProviderEventType
        provider = RuntimeProvider(
            configured.base_url, configured.api_key, configured.model_id,
            protocol=configured.protocol,
            capabilities=configured.runtime_capabilities(),
        )
        search_schema = [{
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the public web.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }]
        text_parts: list[str] = []
        server_started = False
        server_finished = False
        for event in provider.stream_events(
            [{
                "role": "user",
                "content": (
                    "Search the public web for the following query. Return concise, "
                    "factual findings and include source URLs in the answer.\n\n" + query
                ),
            }],
            max_tokens=min(1600, configured.max_output_tokens),
            timeout=40,
            tools=search_schema,
            metadata={"hosted_web_search": True, "web_search_mode": "provider"},
        ):
            if event.type == ProviderEventType.TEXT_DELTA and event.text:
                text_parts.append(event.text)
            elif event.type == ProviderEventType.SERVER_TOOL_STARTED:
                server_started = True
            elif event.type == ProviderEventType.SERVER_TOOL_RESULT:
                server_finished = True
            elif event.type in {ProviderEventType.TOOL_CALL_STARTED, ProviderEventType.TOOL_CALL_DONE}:
                raise RuntimeError("provider returned a client function call instead of hosted search")
        answer = "".join(text_parts).strip()
        if not server_started or not server_finished or not answer:
            raise RuntimeError("provider did not produce a completed hosted-search result")
        urls = list(dict.fromkeys(re.findall(r"https?://[^\s)\]>]+", answer)))[:20]
        return {
            "query": query,
            "answer": answer,
            "sources": urls,
            "count": len(urls),
            "provider": "provider-hosted",
            "provider_id": configured.id,
            "provider_reachable": True,
        }
    except Exception as exc:
        return error_payload(
            "WEB_SEARCH_FAILED",
            message="The active model provider could not complete hosted web search.",
            details={
                "provider_contract": "provider-hosted-search",
                "failure_type": type(exc).__name__,
                "diagnostic": str(exc)[:400],
            },
            next_actions=[
                {"action": "retry"},
                {"action": "configure_searxng_fallback"},
                {"action": "continue_offline"},
            ],
        )


def _is_deepseek_official(configured) -> bool:
    """Recognize the official DeepSeek route without trusting display names."""
    try:
        host = (urlsplit(str(configured.base_url)).hostname or "").lower()
    except ValueError:
        return False
    return host == "api.deepseek.com" or host.endswith(".api.deepseek.com")


def _deepseek_search_endpoint(base_url: str) -> str:
    """Map any official DeepSeek model base to its Anthropic search endpoint."""
    parsed = urlsplit(str(base_url).strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("DeepSeek provider base URL is invalid")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return f"{origin}/anthropic/v1/messages"


def _deepseek_hosted_search(configured, query: str, args: dict) -> dict:
    """Use DeepSeek's structured native search through its Messages API.

    DeepSeek's Responses compatibility endpoint intentionally ignores built-in
    web tools.  The official Harness instead issues an auxiliary Anthropic
    Messages request with ``web_search_20250305``.  Keep that provider-private
    transport behind Gitgo's already-approved SearchBroker call.
    """
    import httpx

    endpoint = _deepseek_search_endpoint(configured.base_url)
    limit = max(1, min(int(args.get("max_results", 8) or 8), 20))
    response = httpx.post(
        endpoint,
        headers={
            "x-api-key": configured.api_key,
            "Authorization": f"Bearer {configured.api_key}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Gitgo/1.0",
        },
        json={
            "model": configured.model_id,
            "max_tokens": min(4096, max(256, int(configured.max_output_tokens or 4096))),
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": f"Perform a web search for the query: {query}",
                }],
            }],
            "tools": [{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 5,
            }],
        },
        timeout=40,
        follow_redirects=False,
    )
    response.raise_for_status()
    payload = response.json()
    blocks = payload.get("content", []) if isinstance(payload, dict) else []
    if not isinstance(blocks, list):
        raise _ProviderProtocolError("DeepSeek Messages response has no content blocks")

    snippets: dict[str, str] = {}
    last_result_index = -1
    for block_index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != "text":
            if isinstance(block, dict) and block.get("type") == "web_search_tool_result":
                last_result_index = block_index
        else:
            for citation in block.get("citations", []) or []:
                if not isinstance(citation, dict):
                    continue
                url = str(citation.get("url") or "")
                cited = str(citation.get("cited_text") or "")
                if url and cited and url not in snippets:
                    snippets[url] = cited

    results: list[dict] = []
    seen: set[str] = set()
    result_block_seen = False
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "web_search_tool_result":
            continue
        result_block_seen = True
        for item in block.get("content", []) or []:
            if not isinstance(item, dict) or item.get("type") != "web_search_result":
                continue
            url = str(item.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            results.append({
                "url": url,
                "title": str(item.get("title") or ""),
                "snippet": snippets.get(url, ""),
                "published": str(item.get("page_age") or ""),
            })
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break
    if not result_block_seen:
        raise _ProviderProtocolError(
            "DeepSeek returned no web_search_tool_result blocks"
        )
    # Keep provider prose separate from citeable evidence. DeepSeek Harness uses
    # the same rule: result cards and citation excerpts are evidence; generated
    # prose is optional context that must not silently become the source of
    # truth. Callers can fetch selected URLs when snippets are absent.
    answer_parts = [
        str(block.get("text") or "").strip()
        for block in blocks[last_result_index + 1:]
        if isinstance(block, dict)
        and block.get("type") == "text"
        and str(block.get("text") or "").strip()
    ]
    return {
        "query": query,
        "content": "\n\n".join(answer_parts),
        "content_is_provider_generated": True,
        "results": results,
        "sources": [item["url"] for item in results],
        "count": len(results),
        "provider": "deepseek-official",
        "provider_id": configured.id,
        "provider_reachable": True,
        "empty_reason": "provider_returned_no_results" if not results else "",
    }


def _public_endpoint(endpoint: str) -> str:
    """Return a diagnostic-safe endpoint without credentials or query data."""
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{parsed.scheme}://{host}{parsed.path}" if parsed.scheme and host else "configured"
    except ValueError:
        return "configured"
