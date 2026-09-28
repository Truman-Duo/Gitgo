"""Provider-neutral runtime capability negotiation.

The model decides what the user is trying to achieve.  The Host owns the
mechanical facts: which provider features are available, which Gitgo fallback
is configured, and which user-visible recovery choices are valid.  Keeping
those facts in one projection avoids provider-specific prompt patches and lets
the same contract grow beyond web search.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


def _capability_dict(value: object) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        try:
            return dict(value.to_dict())
        except (TypeError, ValueError):
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def build_capability_snapshot(
    *,
    model_id: str,
    protocol: str,
    capabilities: object,
    runtime_preferences: Mapping[str, object] | None = None,
    available_tools: set[str] | None = None,
) -> dict[str, Any]:
    """Return a secret-free, machine-readable runtime capability projection."""
    caps = _capability_dict(capabilities)
    prefs = dict(runtime_preferences or {})
    tools = set(available_tools or ())
    mode = str(prefs.get("web_search_mode") or "auto").strip().lower()
    endpoint_configured = bool(str(prefs.get("web_search_endpoint") or "").strip())
    hosted_search = bool(caps.get("hosted_web_search", False))
    search_tool_visible = "web_search" in tools
    fetch_tool_visible = "web_fetch" in tools

    if mode == "disabled":
        search_state = "disabled"
    elif mode == "provider":
        search_state = "available_native" if hosted_search else "provider_plan_unavailable"
    elif mode == "searxng":
        search_state = "available_fallback" if endpoint_configured else "fallback_unconfigured"
    elif hosted_search:
        search_state = "available_native"
    elif endpoint_configured:
        search_state = "available_fallback"
    else:
        search_state = "no_route_configured"

    options: list[dict[str, Any]] = []
    if hosted_search and mode != "provider":
        options.append({
            "action": "use_provider_search",
            "available": True,
            "effect": "Use the active model API's hosted search capability.",
        })
    if endpoint_configured and mode != "searxng":
        options.append({
            "action": "use_configured_fallback",
            "available": True,
            "requires_user_approval": True,
            "effect": "Switch to auto mode and use the already configured SearXNG fallback.",
        })
    if not endpoint_configured:
        options.append({
            "action": "configure_searxng",
            "available": False,
            "requires_user_input": ["endpoint"],
            "effect": "Add an independent search transport; it may have its own hosting or API cost.",
        })
    options.extend((
        {
            "action": "switch_provider",
            "available": True,
            "requires_user_approval": True,
            "effect": "Use another configured model provider whose probed plan exposes hosted search.",
        },
        {
            "action": "continue_offline",
            "available": True,
            "effect": "Answer from existing knowledge and clearly mark material freshness limits.",
        },
    ))

    return {
        "model": {
            "model_id": str(model_id or "unknown"),
            "protocol": str(protocol or "unknown"),
            "harness": "Gitgo",
        },
        "provider_capabilities": {
            key: caps.get(key)
            for key in (
                "streaming", "tools", "parallel_tools", "reasoning_continuation",
                "prompt_cache", "context_window", "max_output_tokens",
                "hosted_web_search", "hosted_web_fetch",
            )
            if key in caps
        },
        "web_access": {
            "mode": mode,
            "search_state": search_state,
            "search_tool_visible": search_tool_visible,
            "fetch_tool_visible": fetch_tool_visible,
            "provider_hosted_search": hosted_search,
            "searxng_configured": endpoint_configured,
            "engine": str(prefs.get("web_search_engine") or "duckduckgo"),
            "options": options,
        },
    }


def record_provider_changed(
    session,
    *,
    model_id: str,
    protocol: str,
    provider_route: str,
) -> dict[str, Any] | None:
    """Persist one model-visible identity delta for an actual route change.

    Normal requests do not receive a repeated identity envelope.  The current
    provider/model remains Host-owned and can be read on demand through
    ``capability_status``.  A route change is different: native continuation
    state has just been quarantined, so the model also needs one nearby fact
    that supersedes any older identity in conversation history.
    """
    event = {
        "event": "provider_changed",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider_route": str(provider_route or ""),
        "model_id": str(model_id or "unknown"),
        "protocol": str(protocol or "unknown"),
        "harness": "Gitgo",
    }
    for item in reversed(list(getattr(session, "host_ledger", []) or [])):
        if item.get("event") != "provider_changed":
            continue
        if all(item.get(key) == event[key] for key in (
            "provider_route", "model_id", "protocol", "harness",
        )):
            return None
        break
    session.host_ledger.append(event)
    session.append_host_steering(
        "[HOST PROVIDER CHANGED]\n"
        f"Current model: {event['model_id']}\n"
        "Runtime: Gitgo harness\n"
        "This supersedes older provider/model identity in the conversation. "
        "Only disclose both facts when the user asks about model identity. "
        "Do not volunteer unrelated capability or configuration status.",
        steering_type="provider_changed",
        version=event["provider_route"],
    )
    return event
