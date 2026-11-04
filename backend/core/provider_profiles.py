"""Models owned by one API connection; selected fields project into the runtime contract."""
import copy
from .loop.provider_preferences import validate_preferences
from .loop.provider_protocol import ProviderProtocol
from .provider_catalog import MAX_PROVIDER_MODELS

FIELDS = ("model_id", "protocol", "context_window", "max_output_tokens", "capabilities", "limits_source")
CONFIG_FIELDS = ("request_preferences", "sources", "deployment", "verification", "warnings")


def selected_profile(provider):
    config = provider.get("configuration", {})
    return {**{k: copy.deepcopy(provider[k]) for k in FIELDS if k in provider},
            **{k: copy.deepcopy(config[k]) for k in CONFIG_FIELDS if k in config}}


def profiles(provider):
    """Legacy connections acquire a single model without rewriting the original file."""
    items = provider.get("configuration", {}).get("models")
    if isinstance(items, list) and items:
        return copy.deepcopy(items)
    return [selected_profile(provider)] if provider.get("model_id") else []


def apply_profile(provider, profile):
    result = copy.deepcopy(provider)
    result.update({k: copy.deepcopy(profile[k]) for k in FIELDS if k in profile})
    config = result.setdefault("configuration", {})
    for key in CONFIG_FIELDS:
        config.pop(key, None)
        if key in profile:
            config[key] = copy.deepcopy(profile[key])
    return result


def compile_profiles(raw, provider, existing, receipts):
    """Compile all profiles before the single encrypted configuration commit."""
    current = selected_profile(provider)
    old = {p["model_id"]: p for p in profiles(existing or {})}
    same_connection = bool(existing and all(provider[k] == existing[k] for k in ("base_url", "api_key", "kind", "auth_mode")))
    if raw is None:
        result = list(old.values()) if same_connection else []
        result = [p for p in result if p["model_id"] != current["model_id"]]
        return [*result, current]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_PROVIDER_MODELS:
        raise ValueError("INVALID_PROVIDER_MODELS")
    result, seen, discovery_indexes = [], set(), {}
    allowed = {"model_id", "protocol", "context_window", "max_output_tokens", "request_preferences", "context_source", "output_source", "model_receipt", "probe_receipt"}
    for item in raw:
        if not isinstance(item, dict) or set(item) - allowed:
            raise ValueError("INVALID_PROVIDER_MODEL")
        model = item.get("model_id")
        if not isinstance(model, str) or not model.strip() or len(model) > 512 or model != model.strip() or model in seen:
            raise ValueError("INVALID_PROVIDER_MODEL_ID")
        seen.add(model)
        protocol = ProviderProtocol(item.get("protocol", provider["protocol"])).value
        context, output = item.get("context_window", 128000), item.get("max_output_tokens", 4096)
        if type(context) is not int or type(output) is not int or context < 1024 or not 1 <= output < context:
            raise ValueError("INVALID_PROVIDER_LIMITS")
        preferences = validate_preferences(item.get("request_preferences", {}), protocol)
        if model == current["model_id"]:
            if protocol == "auto" and item.get("probe_receipt"):
                protocol = current["protocol"]  # The selected model's receipt was validated by provider_save.
            if (protocol, context, output, preferences) != (current["protocol"], current["context_window"], current["max_output_tokens"], current.get("request_preferences", {})):
                raise ValueError("SELECTED_MODEL_PROFILE_MISMATCH")
            result.append(current)
            continue
        previous = old.get(model, {}) if same_connection else {}
        unchanged = previous.get("protocol") == protocol and previous.get("request_preferences", {}) == preferences
        profile = {"model_id": model, "protocol": protocol, "context_window": context, "max_output_tokens": output,
                   "limits_source": "configured", "request_preferences": preferences,
                   "capabilities": copy.deepcopy(previous.get("capabilities", {})) if unchanged else {}}
        draft = {k: provider[k] for k in ("base_url", "api_key", "kind", "auth_mode")}
        draft.update(model_id=model, protocol=protocol)
        receipt_id = item.get("model_receipt")
        discovery_key = (receipt_id, protocol)
        if receipt_id and discovery_key not in discovery_indexes:
            discovery = receipts.evidence(receipt_id, {**draft, "model_id": ""}, "models")
            discovery_indexes[discovery_key] = {m["id"]: m for m in discovery["models"]}
        deployment = discovery_indexes[discovery_key].get(model) if receipt_id else previous.get("deployment")
        if deployment:
            profile["deployment"] = copy.deepcopy(deployment)
        sources = {"context_window": item.get("context_source", "user"), "max_output_tokens": item.get("output_source", "user")}
        if any(v not in {"user", "default", "reference", "discovered"} for v in sources.values()):
            raise ValueError("INVALID_PARAMETER_SOURCE")
        limits = (deployment or {}).get("limits", {})
        profile["warnings"] = ["DECLARED_LIMIT_EXCEEDED"] if context > limits.get("context_window", context) or output > limits.get("max_output_capacity", output) else []
        if sources["context_window"] in {"reference", "discovered"} and context != limits.get("context_window"):
            raise ValueError("INVALID_PARAMETER_SOURCE")
        profile["sources"] = sources
        probe = receipts.evidence(item.get("probe_receipt"), {**draft, "request_preferences": preferences}, "probe")
        if probe:
            protocol = profile["protocol"] = probe["protocol"]
            profile["capabilities"] = probe["capabilities"]
            profile["verification"] = {"checks": probe["checks"], "observations": probe["observations"], "at": probe["capabilities"]["probed_at"]}
        elif unchanged and previous.get("verification"):
            profile["verification"] = copy.deepcopy(previous["verification"])
        if protocol == "auto":
            raise ValueError("PROTOCOL_PROBE_REQUIRED")
        result.append(profile)
    if current["model_id"] not in seen:
        raise ValueError("SELECTED_MODEL_NOT_CONFIGURED")
    return result
