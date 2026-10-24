"""Read a process model identity without inferring from global configuration."""
def recorded_model_id(model_id, host_ledger) -> str:
    if model_id and model_id != "unknown":
        return str(model_id)
    for record in reversed(list(host_ledger or [])):
        if record.get("event") == "provider_route_activated":
            return str(record.get("model") or "")
        if record.get("event") == "provider_changed":
            return str(record.get("model_id") or "")
    return ""
