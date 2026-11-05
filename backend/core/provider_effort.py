"""Effort options are model declarations, not an inferred universal scale."""


def effort_options(profile):
    metadata = (profile.get("deployment") or {}).get("metadata") or {}
    protocol = profile.get("protocol")
    raw = metadata.get("reasoning_efforts")
    levels = list(dict.fromkeys(v for v in raw if isinstance(v, str) and v and v == v.strip() and len(v) <= 64
                               and all(ord(c) >= 32 and ord(c) != 127 for c in v))) if isinstance(raw, list) else []
    supported_protocol = protocol in {"openai_chat", "openai_responses", "anthropic_messages"}
    if not supported_protocol or metadata.get("reasoning") is False:
        levels = []
        state = "unsupported"
    else:
        state = metadata.get("state", "declared") if levels else "unknown"
    default = metadata.get("reasoning_effort_default", "")
    default = default if default in levels else ""
    effort = profile.get("request_preferences", {}).get("reasoning_effort", "")
    return {"levels": levels, "state": state, "source": metadata.get("source", ""),
            "effort": effort, "default_effort": default, "effective_effort": effort or default}
