"""Locale-independent UTF-8 JSON-lines transport helpers."""

from __future__ import annotations

import json
from typing import Any


def dump_protocol_json(value: Any, **kwargs: Any) -> str:
    """Serialize a process-boundary value without producing invalid UTF-8.

    JavaScript strings, legacy Git metadata, and filesystem APIs can preserve
    an unpaired UTF-16 surrogate. JSON can carry it as an escape, but UTF-8
    cannot encode it directly. Wire messages therefore use ASCII JSON escapes;
    ordinary Unicode, including Chinese text, is reconstructed unchanged by
    the receiving JSON parser.
    """
    kwargs.pop("ensure_ascii", None)
    return json.dumps(value, ensure_ascii=True, **kwargs)


def write_utf8_line(stream, line: str) -> None:
    """Write one protocol line as UTF-8 even when Windows chose a legacy locale.

    Real terminals may expose a GBK/CP1252 ``TextIOWrapper`` while pipes and
    clients require a stable wire encoding.  When a binary buffer is available,
    bypass the locale codec; StringIO/test streams retain the text fallback.
    """
    payload = (line + "\n").encode("utf-8")
    binary = getattr(stream, "buffer", None)
    if binary is not None:
        binary.write(payload)
        binary.flush()
        return
    stream.write(line + "\n")
    stream.flush()
