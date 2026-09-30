"""Deterministic handling for malformed UTF-16 text at durable boundaries."""

from __future__ import annotations

import re
from typing import Any


_UNPAIRED_SURROGATE = re.compile(r"[\ud800-\udfff]")


def normalize_unicode_text(value: str) -> str:
    """Make a string valid UTF-8 while retaining the offending code unit.

    A lone surrogate can enter from JavaScript slicing, legacy Git metadata or
    an old persisted session.  Replacing it with a literal ``\\uXXXX`` marker
    is stable, inspectable, and safer than either crashing or silently dropping
    user data.
    """
    return _UNPAIRED_SURROGATE.sub(
        lambda match: f"\\u{ord(match.group(0)):04x}", value,
    )


def unicode_integrity_error(value: str) -> str:
    """Return a deterministic explanation when fresh text is not UTF-8 safe.

    ``normalize_unicode_text`` remains useful for quarantining legacy durable
    data, but applying it to a newly submitted prompt silently changes user
    intent.  Fresh transport input must fail closed before any provider spend.
    """
    match = _UNPAIRED_SURROGATE.search(value)
    if match is None:
        return ""
    return (
        "input contains an unpaired UTF-16 surrogate at code-unit offset "
        f"{match.start()} (U+{ord(match.group(0)):04X})"
    )


def normalize_unicode_value(value: Any) -> Any:
    """Recursively normalize strings in JSON-shaped data."""
    if isinstance(value, str):
        return normalize_unicode_text(value)
    if isinstance(value, dict):
        return {
            normalize_unicode_text(str(key)): normalize_unicode_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalize_unicode_value(item) for item in value]
    if isinstance(value, tuple):
        return [normalize_unicode_value(item) for item in value]
    return value
