"""Versioned connection presets and bounded, authenticated model discovery.

Provider-reported limits are declarations, not capability verification. No model
name heuristics, alternate hosts, redirects, or implicit configuration writes.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .loop.provider_policy import provider_endpoint
from .loop.provider_transport import HttpProviderTransport, ProviderTransportCancelled

CATALOG_VERSION = "2026-11-03.1"
MAX_PROVIDER_MODELS = 10000
PRESETS = (
    {"id": "custom", "name": "Custom", "base_url": "", "protocol": "openai_chat", "source": "user"},
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com", "protocol": "openai_chat", "source": "https://api-docs.deepseek.com/api/list-models/"},
    {"id": "mimo", "name": "MiMo", "base_url": "https://api.xiaomimimo.com/v1", "protocol": "openai_chat", "source": "https://mimo.mi.com/docs/zh-CN/api/model/list-models"},
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "protocol": "openai_responses", "source": "https://developers.openai.com/api/reference/resources/models/methods/list"},
    {"id": "anthropic", "name": "Anthropic", "base_url": "https://api.anthropic.com/v1", "protocol": "anthropic_messages", "source": "https://platform.claude.com/docs/en/api/models/list"},
    {"id": "openrouter", "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "protocol": "openai_chat", "source": "https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties"},
)


def presets() -> dict:
    return {"version": CATALOG_VERSION, "presets": [dict(p) for p in PRESETS]}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def safe_opener(kind="remote"):
    handlers = [NoRedirect()]
    if kind == "local":
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers).open


def models_urls(base_url: str, protocol: str) -> list[str]:
    """Preserve reverse-proxy prefixes; fallback only on 404/405, same origin."""
    parts = urllib.parse.urlsplit(base_url)
    path = parts.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/messages", "/models"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    paths = [path + "/models"]
    if protocol == "anthropic_messages" and not path.endswith("/v1"):
        paths.insert(0, path + "/v1/models")
    return list(dict.fromkeys(urllib.parse.urlunsplit((parts.scheme, parts.netloc, p, "", "")) for p in paths))


def parse_models(payload, source: str) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("MODEL_LIST_INVALID")
    rows = payload.get("data", payload.get("models"))
    if not isinstance(rows, list):
        raise ValueError("MODEL_LIST_INVALID")
    models = {}
    for row in rows[:MAX_PROVIDER_MODELS]:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id", row.get("slug"))
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 512:
            continue
        limits = {}
        for field, value in (
            ("context_window", row.get("context_window", row.get("context_length"))),
            ("max_output_capacity", row.get("max_output_tokens", (row.get("top_provider") or {}).get("max_completion_tokens") if isinstance(row.get("top_provider", {}), dict) else None)),
        ):
            if type(value) is int and 0 < value <= 100_000_000:
                limits[field] = value
        models[model_id] = {"id": model_id, "name": str(row.get("name", row.get("display_name", model_id)))[:512],
                            "limits": limits, "source": source, "state": "declared",
                            "observed_at": datetime.now(timezone.utc).isoformat()}
        metadata = {}
        effort = row.get("effort")
        if isinstance(effort, dict) and isinstance(effort.get("supported_levels"), list):
            metadata["reasoning_efforts"] = [value for value in effort["supported_levels"] if isinstance(value, str)]
            if effort.get("default") in metadata["reasoning_efforts"]:
                metadata["reasoning_effort_default"] = effort["default"]
        modalities = {kind: row[kind + "_modalities"] for kind in ("input", "output") if isinstance(row.get(kind + "_modalities"), list)}
        if modalities:
            metadata["modalities"] = {kind: [value for value in values if isinstance(value, str)] for kind, values in modalities.items()}
        if metadata:
            metadata.update(source=source, state="declared")
            models[model_id]["metadata"] = metadata
    return sorted(models.values(), key=lambda m: m["id"].casefold())


def discover(*, base_url, api_key="", protocol="openai_chat", kind="remote", auth_mode="api_key", timeout=15, cancel_event=None, open_url=None):
    base_url = provider_endpoint(base_url, kind=kind, auth_mode=auth_mode)
    headers = {"Accept": "application/json"}
    if protocol == "anthropic_messages":
        headers["anthropic-version"] = "2023-06-01"
        if auth_mode == "api_key" and api_key:
            headers["x-api-key"] = api_key
    elif auth_mode == "api_key" and api_key:
        headers["Authorization"] = "Bearer " + api_key
    deadline = time.monotonic() + max(1, min(float(timeout), 30))
    opener = open_url or safe_opener(kind)
    for url in models_urls(base_url, protocol):
        if cancel_event is not None and cancel_event.is_set():
            raise ProviderTransportCancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("MODEL_LIST_TIMEOUT")
        try:
            with opener(urllib.request.Request(url, headers=headers), timeout=remaining) as response:
                done = HttpProviderTransport._close_on_cancel(response, cancel_event)
                try:
                    raw = response.read(4 * 1024 * 1024 + 1)
                finally:
                    done.set()
                if cancel_event is not None and cancel_event.is_set():
                    raise ProviderTransportCancelled()
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError("MODEL_LIST_TOO_LARGE")
                result = json.loads(raw)
                return {"models": parse_models(result, url), "source": url,
                        "partial": bool(result.get("has_more")), "manual_allowed": True}
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in (404, 405):
                continue
            # Never echo upstream bodies, which may include credentials.
            raise ValueError(f"MODEL_LIST_HTTP_{exc.code}") from None
        except ProviderTransportCancelled:
            raise
        except (OSError, ValueError, urllib.error.URLError) as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderTransportCancelled() from None
            if isinstance(exc, ValueError) and str(exc).startswith("MODEL_LIST_"):
                raise
            raise ValueError("MODEL_LIST_CONNECTION_FAILED") from None
    raise ValueError("MODEL_LIST_UNAVAILABLE")
