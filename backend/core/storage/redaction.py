"""Shared fail-closed redaction for all durable local records."""

from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from backend.core.unicode_safety import normalize_unicode_text


_SECRET_KEYS = re.compile(
    r"(?i)^(?:api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|"
    r"private[_-]?key|client[_-]?secret|secret|password|credential|token|"
    r".+(?:_api_key|_password|_secret|_credential|_access_token|_private_key))$"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_API_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


def redact_for_persistence(value: Any, *, key: str = "") -> Any:
    """Remove credentials before data reaches CAS, SQLite, trace, or live wire."""
    if key and _SECRET_KEYS.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): redact_for_persistence(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_for_persistence(item) for item in value]
    if isinstance(value, tuple):
        return [redact_for_persistence(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return redact_for_persistence(asdict(value))
    if isinstance(value, Enum):
        return redact_for_persistence(value.value)
    if isinstance(value, Path):
        return redact_for_persistence(str(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return normalize_unicode_text(_API_TOKEN.sub(
            "[REDACTED_API_KEY]",
            _BEARER.sub("Bearer [REDACTED]", value),
        ))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"[UNSUPPORTED_TYPE:{type(value).__name__}]"
