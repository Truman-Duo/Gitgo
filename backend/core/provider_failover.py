"""Per-primary, non-recursive backup queues with a single global enable switch."""
import hashlib
import json


def normalize(config):
    result = dict(config)
    known = {p["id"] for p in config.get("providers", [])}
    primary = config.get("active_provider", "")
    if "failover_routes" in config:
        routes = config["failover_routes"]
        if not isinstance(routes, dict):
            raise ValueError("Invalid per-API backup routes")
        routes = {owner: list(order) if isinstance(order, list) else order for owner, order in routes.items()}
        for owner, order in routes.items():
            if owner not in known or not isinstance(order, list) or any(not isinstance(p, str) for p in order):
                raise ValueError("Backup route contains an unknown API")
            if len(order) > 2 or len(set(order)) != len(order) or owner in order or set(order) - known:
                raise ValueError("Each API supports at most two distinct backups, excluding itself")
    else:
        legacy = config.get("failover_order", [])
        if not isinstance(legacy, list) or any(not isinstance(p, str) for p in legacy):
            raise ValueError("Invalid legacy backup queue")
        order = list(dict.fromkeys(p for p in legacy if p in known and p != primary))[:2]
        routes = {primary: order} if primary in known else {}
        if legacy:
            result.setdefault("legacy_failover_order", list(legacy))
    result["failover_routes"] = routes
    # Compatibility projection for old readers, never used as a second authority.
    result["failover_order"] = list(routes.get(primary, []))
    return result


def revision(config):
    normalized = normalize(config)
    state = {k: normalized.get(k) for k in ("active_provider", "failover_enabled", "failover_routes", "circuit_breaker")}
    state["provider_ids"] = sorted(p["id"] for p in normalized.get("providers", []))
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
