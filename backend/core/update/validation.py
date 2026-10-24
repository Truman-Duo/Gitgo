"""Bounded wire validation shared by update discovery and signature envelopes."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit


class UpdateError(ValueError):
    """Stable failure code without echoing untrusted contents or URLs."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def reject(code: str, message: str) -> None:
    raise UpdateError(code, message)


def bounded_bytes(value: bytes, limit: int) -> None:
    if type(value) is not bytes or not 0 < len(value) <= limit:
        reject("UPDATE_INPUT_SIZE", "Expected nonempty bytes within the size limit")


def json_object(raw: bytes, limit: int) -> dict[str, Any]:
    bounded_bytes(raw, limit)
    # Do not depend on Python's configurable recursion limit for network JSON.
    depth = 0
    in_string = escaped = False
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                in_string = False
        elif char == 34:
            in_string = True
        elif char in (91, 123):
            depth += 1
            if depth > 16:
                reject("UPDATE_JSON_INVALID", "JSON nesting exceeds the wire limit")
        elif char in (93, 125):
            depth -= 1
            if depth < 0:
                reject("UPDATE_JSON_INVALID", "Unbalanced JSON containers")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                reject("UPDATE_JSON_INVALID", "Duplicate JSON field")
            result[key] = value
        return result

    def integer(value):
        if len(value) > 20:
            reject("UPDATE_JSON_INVALID", "Integer exceeds the wire limit")
        return int(value)

    def noninteger(_):
        reject("UPDATE_JSON_INVALID", "Only integer JSON numbers are supported")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_int=integer, parse_float=noninteger, parse_constant=noninteger)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise UpdateError("UPDATE_JSON_INVALID", "Invalid UTF-8 JSON object") from error
    if type(value) is not dict:
        reject("UPDATE_JSON_INVALID", "Expected a JSON object")
    return value


def fields(value: Any, expected: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        reject("UPDATE_SCHEMA_INVALID", "Missing or unsupported fields")
    return value


def text(value: Any, pattern: str, maximum: int = 128) -> str:
    if type(value) is not str or not 0 < len(value) <= maximum or re.fullmatch(pattern, value) is None:
        reject("UPDATE_SCHEMA_INVALID", "Invalid string field")
    return value


def number(value: Any, minimum: int = 1, maximum: int = 2**53 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        reject("UPDATE_SCHEMA_INVALID", "Invalid integer field")
    return value


def digest(value: Any) -> str:
    return text(value, r"[0-9a-f]{64}", 64)


def timestamp(value: Any) -> datetime:
    text(value, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", 20)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise UpdateError("UPDATE_SCHEMA_INVALID", "Invalid UTC timestamp") from error


def url_text(value: Any) -> str:
    if type(value) is not str or not 0 < len(value) <= 2048:
        reject("UPDATE_URL_INVALID", "Invalid release URL")
    if any(ord(char) <= 32 or ord(char) >= 127 for char in value) or "\\" in value:
        reject("UPDATE_URL_INVALID", "Release URL contains forbidden characters")
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        reject("UPDATE_URL_INVALID", "Malformed percent encoding")
    return value


def https_url(value: Any, allowed_hosts: tuple[str, ...], *, allow_query: bool = False) -> str:
    """Validate configured release hosts; a fetch adapter must also check redirects."""
    url_text(value)
    try:
        parsed = urlsplit(value)
        decoded = unquote(parsed.path, errors="strict")
        valid = (parsed.scheme == "https" and parsed.hostname in allowed_hosts
                 and parsed.port in (None, 443) and parsed.username is None
                 and parsed.password is None and (allow_query is True or not parsed.query) and not parsed.fragment
                 and parsed.path.startswith("/") and "\\" not in decoded
                 and not any(ord(char) < 32 or ord(char) == 127 for char in decoded)
                 and not any(part in (".", "..") for part in decoded.split("/")))
    except (ValueError, UnicodeError) as error:
        raise UpdateError("UPDATE_URL_INVALID", "Invalid release URL") from error
    if not valid:
        reject("UPDATE_URL_INVALID", "URL must use HTTPS on an explicitly allowed release host")
    return value
