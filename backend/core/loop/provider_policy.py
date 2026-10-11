"""Validated configuration shared by provider UI, runtime and probes."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit


def provider_endpoint(value: str, *, kind: str = "remote", auth_mode: str = "api_key") -> str:
    if kind not in {"remote", "local"} or auth_mode not in {"api_key", "none"}:
        raise ValueError("Choose remote/local and api_key/none authentication")
    if auth_mode == "none" and kind != "local":
        raise ValueError("Authentication-free providers must be explicitly local")
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Provider URL must be an absolute HTTP(S) endpoint")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("Provider URL cannot contain credentials, a query or fragment")
    port = parsed.port  # also rejects malformed port values
    host = parsed.hostname
    if kind == "local":
        if host.casefold() == "localhost":
            host = "127.0.0.1"
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("Local provider must use localhost or a loopback IP address") from exc
        if not address.is_loopback:
            raise ValueError("Local provider must use a loopback endpoint")
    rendered_host = f"[{host}]" if ":" in host else host
    authority = rendered_host + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme, authority, parsed.path.rstrip("/"), "", ""))


def failover_settings(config: dict) -> dict:
    raw = dict(config.get("circuit_breaker") or {})
    result = {"failure_threshold": 4, "success_threshold": 2, "cooldown_seconds": 60,
              "error_rate_threshold": 0.6, "min_requests": 10, "window_size": 100}
    if set(raw) - set(result):
        raise ValueError("Unknown circuit breaker setting")
    result.update(raw)
    for name, maximum in (("failure_threshold", 100), ("success_threshold", 10),
                          ("cooldown_seconds", 3600), ("min_requests", 100), ("window_size", 1000)):
        value = result[name]
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    if result["min_requests"] > result["window_size"]:
        raise ValueError("min_requests cannot exceed window_size")
    rate = result["error_rate_threshold"]
    if type(rate) not in (float, int) or not 0 < rate <= 1:
        raise ValueError("error_rate_threshold must be greater than zero and at most one")
    return result
