"""Bounded diagnostic origin. It never selects a store or grants authority."""
from __future__ import annotations

import json
import os


def frontend_origin() -> dict:
    raw = os.getenv("GITGO_FRONTEND_ORIGIN", "")
    if not raw:
        return {"version": 1, "terminal": "unknown", "platform": os.name, "source": "unspecified"}
    if len(raw) > 1024:
        raise ValueError("FRONTEND_ORIGIN_INVALID: origin exceeds 1KB")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("FRONTEND_ORIGIN_INVALID: unsupported schema")
    result = {"version": 1}
    for key in ("terminal", "platform", "source"):
        text = value.get(key)
        if not isinstance(text, str) or not text or len(text) > 64 or any(ord(c) < 32 for c in text):
            raise ValueError(f"FRONTEND_ORIGIN_INVALID: invalid {key}")
        result[key] = text
    return result
