"""Locale-independent UTF-8 JSON-lines transport helpers."""

from __future__ import annotations


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
