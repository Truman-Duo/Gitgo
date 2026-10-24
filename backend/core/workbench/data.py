"""Bounded exact JSON snapshot operations; no kernel, SQL or expression DSL."""
from __future__ import annotations
from decimal import Decimal, Inexact, localcontext
import json
import hashlib
import re

from backend.core.storage.redaction import redact_for_persistence

MAX_INPUT_BYTES = 120_000


class MissingValue(ValueError):
    pass


def _number(text):
    value = Decimal(text)
    if not value.is_finite() or len(value.as_tuple().digits) > 64 or abs(value.as_tuple().exponent) > 100 or value.copy_abs() > Decimal("1e100"):
        raise ValueError("JSON numbers exceed the finite 64-digit / exponent-100 bounds")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object keys are not accepted")
        result[key] = value
    return result


def parse_json(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("JSON snapshot must be at most 120000 bytes")
    def invalid(_):
        raise ValueError("non-finite JSON numbers are not accepted")
    try:
        result = json.loads(text, parse_int=_number, parse_float=_number,
                            parse_constant=invalid, object_pairs_hook=_pairs)
    except (RecursionError, ArithmeticError) as exc:
        raise ValueError("JSON nesting or numeric bounds exceeded") from exc
    todo = [(result, 0)]
    count = 0
    while todo:
        value, depth = todo.pop()
        count += 1
        if count > 20_000 or depth > 32:
            raise ValueError("JSON exceeds 20000 nodes or depth 32")
        if isinstance(value, dict):
            todo.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            todo.extend((v, depth + 1) for v in value)
    return result


def encode_json(value):
    """Preserve exact recorded numbers without a binary-float conversion."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + encode_json(v)
                              for k, v in sorted(value.items())) + "}"
    if isinstance(value, list):
        return "[" + ",".join(encode_json(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _redact(value):
    if isinstance(value, Decimal):
        return value
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            # Reuse the canonical sensitive-key policy without converting
            # exact Decimal values into unsupported-type markers.
            probe = redact_for_persistence({key: None})[key]
            result[key] = probe if probe is not None else _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return redact_for_persistence(value)


def snapshot_json(text):
    parsed = parse_json(text)
    original = encode_json(parsed)
    snapshot = encode_json(_redact(parsed))
    # Normalization/redaction can expand the encoded document; bound it again.
    parse_json(snapshot)
    return snapshot, snapshot != original


def pointer(value, path):
    if not isinstance(path, str) or len(path) > 500 or (path and not path.startswith("/")):
        raise ValueError("selectors must be bounded JSON Pointers, with empty string selecting the root")
    tokens = path.split("/")[1:] if path else []
    if len(tokens) > 32:
        raise ValueError("selector exceeds 32 segments")
    for token in tokens:
        if re.search(r"~(?![01])", token):
            raise ValueError("invalid JSON Pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token) and len(token) < 8 and int(token) < len(value):
            value = value[int(token)]
        else:
            raise MissingValue("JSON Pointer does not select an existing value")
    return value


def _scalar_identity(value):
    if isinstance(value, Decimal):
        if value == 0:
            return ("number", "0")
        # Fixed notation and trailing-zero removal avoid Decimal.normalize's
        # ambient precision and distinguish strings/bools from numeric values.
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return ("number", text)
    if isinstance(value, (dict, list)):
        raise ValueError("distinct_count supports JSON scalars only")
    return (type(value).__name__, value)


def aggregate(text, *, selector, reducer, field="", null_policy="reject"):
    if reducer not in {"count", "key_count", "sum", "min", "max", "distinct_count"}:
        raise ValueError("unsupported reducer")
    if null_policy not in {"reject", "skip"}:
        raise ValueError("null_policy must be reject or skip")
    rows = pointer(parse_json(text), selector)
    if reducer == "key_count":
        if field:
            raise ValueError("key_count counts object members and does not accept field")
        if not isinstance(rows, dict) or len(rows) > 10_000:
            raise ValueError("key_count requires an object of at most 10000 members")
        return {"value": len(rows), "value_type": "integer", "collection_type": "object",
                "input_members": len(rows), "used_members": len(rows),
                "coverage": "Selected snapshot object members only; upstream completeness and semantic field classification are not established."}
    if not isinstance(rows, list) or len(rows) > 10_000:
        raise ValueError("selector must identify an array of at most 10000 rows")
    # Validate field syntax even for empty input, without treating absence as
    # a syntax error. Count always counts every row, including nulls/repeats.
    try:
        pointer({}, field)
    except MissingValue:
        pass
    if reducer == "count" and field:
        raise ValueError("count counts rows and does not accept field")
    result = {"input_rows": len(rows), "used_rows": len(rows), "skipped_null": 0,
              "skipped_missing": 0, "coverage": "Selected snapshot array only; upstream completeness, extraction correctness and units are not established.",
              "duplicates": "retained", "value_type": "integer"}
    if reducer == "count":
        return {**result, "value": len(rows)}
    values = []
    for row in rows:
        try:
            value = pointer(row, field)
        except MissingValue:
            if null_policy != "skip":
                raise ValueError("a row has no selected field; choose an explicit skip policy or correct the selector")
            result["skipped_missing"] += 1
            continue
        if value is None:
            if null_policy != "skip":
                raise ValueError("null values require an explicit skip policy")
            result["skipped_null"] += 1
            continue
        if reducer != "distinct_count" and not isinstance(value, Decimal):
            raise ValueError("numeric reducers require JSON numbers; strings and booleans are not coerced")
        values.append(value)
    result["used_rows"] = len(values)
    if reducer == "distinct_count":
        return {**result, "value": len({_scalar_identity(v) for v in values}),
                "duplicates": "collapsed by typed JSON scalar value; numeric 1 and 1.0 are equal"}
    if reducer != "sum" and not values:
        raise ValueError("min/max require at least one non-null selected number")
    with localcontext() as context:
        context.prec = 512
        context.traps[Inexact] = True
        answer = sum(values, Decimal(0)) if reducer == "sum" else min(values) if reducer == "min" else max(values)
    return {**result, "value": format(answer, "f"), "value_type": "decimal_string"}


def text_digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_snapshots(document):
    """Privacy normalization cannot silently change bound input/query bytes."""
    for cell in document["cells"]:
        meta = cell.get("metadata", {}).get("gitgo", {})
        expected = meta.get("snapshot_sha256") if meta.get("kind") in {"context_source", "provided_data", "tool_result_source"} else (
            meta.get("payload_sha256") if meta.get("kind") == "computed_value" else None)
        if expected is not None and text_digest(cell["source"]) != expected:
            raise ValueError("stored input or computation payload no longer matches its bound digest")
