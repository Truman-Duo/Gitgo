"""Context capacity and Gitgo budget are different, source-bearing values."""
from __future__ import annotations

import copy

CONTEXT_BUDGETS = (256_000, 512_000, 1_000_000)
DEFAULT_CONTEXT_BUDGET = 512_000
POLICY_VERSION = "2026-11-07.1"


def default_context_budget(capacity=None):
    if type(capacity) is int and capacity > 0:
        return min(capacity, 1_000_000 if capacity >= 1_000_000 else DEFAULT_CONTEXT_BUDGET)
    return DEFAULT_CONTEXT_BUDGET


def resolve_profile(profile, base_url, *, legacy_source=None):
    """Read-only migration; only a known default opts into the new policy.

    Unknown legacy origins remain user/legacy values. A later explicit save
    persists this projection through the existing revision/secret boundary.
    """
    from .provider_metadata import model_metadata
    result = copy.deepcopy(profile)
    model_id = result.get("model_id", "")
    if not model_id:
        return result
    deployment = model_metadata(model_id, base_url, result.get("deployment"))
    if deployment.get("limits") or deployment.get("metadata"):
        result["deployment"] = deployment
    capacity = deployment.get("limits", {}).get("context_window")
    sources = result.setdefault("sources", {})
    origin = sources.get("context_window") or legacy_source
    source = origin if origin in {"user", "default", "reference", "discovered", "legacy"} else (
        "default" if "context_window" not in result else "legacy")
    sources["context_window"] = source
    if source == "default":
        result["context_window"] = default_context_budget(capacity)
    elif "context_window" not in result:
        result["context_window"] = default_context_budget(capacity)
    # The budget is not a server capability. Keep the capacity, its source and
    # the chosen policy independently, including an exact sub-256k capacity.
    result["budget"] = {**result.get("budget", {}), "policy_version": POLICY_VERSION,
        "context_window": result["context_window"], "source": source,
        "capacity": capacity, "capacity_source": deployment.get("field_sources", {}).get("context_window", deployment.get("source", "")) if capacity else "",
        "choices": [value for value in CONTEXT_BUDGETS if not capacity or value <= capacity]}
    return result


def resolve_provider(provider):
    """Resolve a copy while callers retain the raw revision as CAS authority."""
    result = copy.deepcopy(provider)
    configuration = result.setdefault("configuration", {})
    models = configuration.get("models")
    if isinstance(models, list) and models:
        models = [resolve_profile(item, result.get("base_url", "")) for item in models]
        configuration["models"] = models
        selected = next((p for p in models if p.get("model_id") == result.get("model_id")), None)
    else:
        selected = resolve_profile({**{key: result[key] for key in ("model_id", "context_window", "max_output_tokens") if key in result},
            **{key: configuration[key] for key in ("deployment", "sources", "budget") if key in configuration}},
            result.get("base_url", ""), legacy_source=result.get("limits_source"))
    if selected:
        result["context_window"] = selected["context_window"]
        for key in ("deployment", "sources", "budget"):
            if key in selected:
                configuration[key] = copy.deepcopy(selected[key])
    return result
