"""Bounded display of captured stderr; never tool authority or cleanup proof."""
from __future__ import annotations

import re
from backend.core.storage.redaction import redact_for_persistence

MAX_STDERR_CHARS = 8192
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _safe_text(value: str) -> str:
    # Strip terminal sequences before masking: escape codes can split tokens.
    text = _CONTROLS.sub("", _ANSI.sub("", value))
    return redact_for_persistence(text)


def safe_runner_text(value: str) -> str:
    return _safe_text(value)[:MAX_STDERR_CHARS]


def capture_runner_diagnostics(result) -> dict:
    safe = _safe_text(getattr(result, "stderr", "") or "")
    return {
        "exit_code": getattr(result, "exit_code", -1),
        "duration_ms": getattr(result, "duration_ms", 0.0),
        "timed_out": bool(getattr(result, "timed_out", False)),
        "stderr": safe[:MAX_STDERR_CHARS],
        "stderr_truncated": len(safe) > MAX_STDERR_CHARS,
        "stderr_partial": bool(getattr(result, "stderr_partial", False)),
    }
