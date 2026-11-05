"""User request-body preferences; transport and task authority stay protected."""
import copy
import json
import math

PROTECTED = {"model", "messages", "input", "instructions", "system", "tools", "tool_choice", "stream", "stream_options",
             "max_tokens", "max_completion_tokens", "max_output_tokens", "store", "previous_response_id", "prompt_cache_key",
             "api_key", "authorization", "include", "metadata"}


def validate_preferences(value, protocol):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"reasoning_effort", "body"}:
        raise ValueError("INVALID_REQUEST_PREFERENCES")
    effort = value.get("reasoning_effort", "")
    body = value.get("body", {})
    if not isinstance(effort, str) or len(effort) > 64 or not isinstance(body, dict):
        raise ValueError("INVALID_REQUEST_PREFERENCES")
    if effort and protocol not in {"auto", "openai_chat", "openai_responses", "anthropic_messages"}:
        raise ValueError("REASONING_PREFERENCE_PROTOCOL_UNSUPPORTED")
    if set(body) & PROTECTED:
        raise ValueError("REQUEST_PREFERENCE_PROTECTED_FIELD")
    if effort and ({"reasoning", "reasoning_effort"} & set(body)):
        raise ValueError("REASONING_PREFERENCE_CONFLICT")
    if effort and isinstance(body.get("output_config"), dict) and "effort" in body["output_config"]:
        raise ValueError("REASONING_PREFERENCE_CONFLICT")
    if effort and protocol == "anthropic_messages" and "output_config" in body and not isinstance(body["output_config"], dict):
        raise ValueError("INVALID_OUTPUT_CONFIG")
    try:
        encoded = json.dumps(body, allow_nan=False)
        if len(encoded.encode()) > 16384:
            raise ValueError()
    except (ValueError, TypeError, RecursionError):
        raise ValueError("INVALID_REQUEST_PREFERENCES") from None
    for name, lo, hi in (("temperature", 0, 2), ("top_p", 0, 1)):
        if name in body and (type(body[name]) not in {int, float} or not math.isfinite(body[name]) or not lo <= body[name] <= hi):
            raise ValueError("INVALID_" + name.upper())
    if "seed" in body and type(body["seed"]) is not int:
        raise ValueError("INVALID_SEED")
    return {**({"reasoning_effort": effort} if effort else {}), **({"body": copy.deepcopy(body)} if body else {})}


def apply_preferences(body, preferences, protocol):
    result = copy.deepcopy(body)
    def merge(target, patch):
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)
    merge(result, preferences.get("body", {}))
    effort = preferences.get("reasoning_effort")
    if effort:
        if protocol == "openai_responses":
            result.setdefault("reasoning", {})["effort"] = effort
        elif protocol == "anthropic_messages":
            result.setdefault("output_config", {})["effort"] = effort
        else:
            result["reasoning_effort"] = effort
    return result
