"""Reference metadata enrichment, following cc-switch's endpoint-scoped lookup.

Public catalogue data never verifies a deployment. The actual service wins;
user overrides remain owned by the configuration form and save boundary.
"""
from __future__ import annotations
import copy
import json
import re
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request

CATALOG_URL = "https://models.dev/api.json"
_lock = threading.Lock()
_cache = ({}, 0.0)


def endpoint_key(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        return None
    if not hostname:
        return None
    if (parsed.scheme,port) in {("https",443),("http",80)}:
        port = None
    segments = [s.lower() for s in parsed.path.split("/") if s]
    while segments:
        if segments[-2:] == ["chat", "completions"]:
            del segments[-2:]
        elif segments[-1] in {"responses", "messages"} or re.fullmatch(r"v\d+(?:(?:alpha|beta)\d*)?", segments[-1]):
            segments.pop()
        else:
            break
    return hostname.lower(), port, tuple(segments)


def catalogue(cancel_event=None, timeout=8):
    global _cache
    deadline = time.monotonic() + min(timeout, 8)
    if cancel_event is not None and cancel_event.is_set():
        return {}, "cancelled"
    # Same one-hour cache as the reference. Never send connection credentials.
    if timeout <= 0 or not _lock.acquire(timeout=max(0, min(timeout, 8))):
        return {}, "unavailable"
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (cancel_event is not None and cancel_event.is_set()):
            return {}, "cancelled" if cancel_event is not None and cancel_event.is_set() else "unavailable"
        data, expires = _cache
        if time.monotonic() < expires:
            return data, "cached" if data else "unavailable"
        from .provider_catalog import safe_opener
        try:
            with safe_opener()(Request(CATALOG_URL, headers={"Accept": "application/json", "User-Agent": "Gitgo/1.0 ProviderMetadata"}), timeout=remaining) as response:
                from .loop.provider_transport import HttpProviderTransport
                done = HttpProviderTransport._close_on_cancel(response, cancel_event)
                try:
                    raw = response.read(16 * 1024 * 1024 + 1)
                finally:
                    done.set()
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("catalogue too large")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("invalid catalogue")
            _cache = (data, time.monotonic() + 3600)
            return data, "fresh"
        except (OSError, ValueError):
            _cache = ({}, time.monotonic() + 60)
            return {}, "unavailable"
    finally:
        _lock.release()


def enrich(models, base_url, data):
    """Match endpoint including plan path and model ID. No name-based limits."""
    candidates = [provider for provider in data.values() if endpoint_key(base_url) and isinstance(provider, dict)
                  and isinstance(provider.get("api"), str) and endpoint_key(provider["api"]) == endpoint_key(base_url)]
    result = copy.deepcopy(models)
    for model in result:
        found = next((provider.get("models", {}).get(model["id"]) for provider in candidates
                      if isinstance(provider.get("models"), dict) and isinstance(provider["models"].get(model["id"]), dict)), None)
        if not found:
            continue
        provenance = model.setdefault("field_sources", {})
        for name in model["limits"]:
            provenance.setdefault(name, model["source"])
        limit = found.get("limit") if isinstance(found.get("limit"), dict) else {}
        for name, value in (("context_window", limit.get("context")), ("max_output_capacity", limit.get("output"))):
            if name not in model["limits"] and type(value) is int and 0 < value <= 100_000_000:
                model["limits"][name] = value
                provenance[name] = CATALOG_URL
        metadata = model.setdefault("metadata", {})
        for name in ("reasoning", "tool_call", "temperature"):
            if type(found.get(name)) is bool:
                metadata.setdefault(name, found[name])
        modalities = found.get("modalities")
        if isinstance(modalities, dict):
            metadata.setdefault("modalities", {kind: [v for v in modalities.get(kind, []) if isinstance(v, str)]
                                       for kind in ("input", "output") if isinstance(modalities.get(kind), list)})
        options = found.get("reasoning_options", [])
        if isinstance(options, list):
            efforts = [value for option in options if isinstance(option, dict) and option.get("type") == "effort"
                       for value in option.get("values", []) if isinstance(value, str)]
            if efforts:
                metadata.setdefault("reasoning_efforts", list(dict.fromkeys(efforts)))
                default = next((option.get("default") for option in options if isinstance(option, dict)
                                and option.get("type") == "effort" and option.get("default") in efforts), "")
                if default:
                    metadata.setdefault("reasoning_effort_default", default)
        cost = found.get("cost")
        if isinstance(cost, dict):
            metadata.setdefault("cost", {name: value for name, value in cost.items()
                                if name in {"input", "output", "cache_read", "cache_write"}
                                and type(value) in {int, float} and 0 <= value < 1_000_000})
        metadata.setdefault("source", CATALOG_URL)
        metadata.setdefault("state", "reference")
    return result


def reference_models(base_url, data):
    """Public candidates for this endpoint, never a claim about account access."""
    from .provider_catalog import MAX_PROVIDER_MODELS
    key = endpoint_key(base_url)
    if not key:
        return []
    found = {}
    for provider in data.values():
        if not isinstance(provider, dict) or not isinstance(provider.get("api"), str) or endpoint_key(provider["api"]) != key:
            continue
        models = provider.get("models")
        if not isinstance(models, dict):
            continue
        for model_id, model in models.items():
            if isinstance(model_id, str) and model_id.strip() and len(model_id) <= 512 and isinstance(model, dict):
                found[model_id] = {"id": model_id, "name": str(model.get("name") or model_id)[:512], "limits": {}, "source": CATALOG_URL, "state": "reference"}
    return enrich(sorted(found.values(), key=lambda m: m["id"].casefold())[:MAX_PROVIDER_MODELS], base_url, data)
