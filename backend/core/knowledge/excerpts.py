"""Native changed-text excerpts. Exact content continuity is not semantic truth."""
from __future__ import annotations
import difflib
import hashlib
from bisect import bisect_right
from pathlib import Path

MAX_TEXT = 512_000
MAX_EXCERPTS = 12
MAX_EXCERPT_CHARS = 2000

def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def changed_excerpts(path: str, before: str, after: str, file_sha256: str) -> list[dict]:
    # Bounds limit this extra observer's work; full-file evidence remains fallback.
    if len(before) + len(after) > MAX_TEXT:
        return []
    left, right = before.splitlines(keepends=True), after.splitlines(keepends=True)
    if len(left) + len(right) > 12_000:
        return []
    result = []
    for tag, _, _, start, end in difflib.SequenceMatcher(None, left, right).get_opcodes():
        if tag == "equal":
            continue
        first, last = max(0, start - 2), min(len(right), max(end, start + 1) + 2)
        text = "".join(right[first:last])
        if not text.strip() or len(text) > MAX_EXCERPT_CHARS:
            continue
        digest = text_hash(text)
        identity = text_hash(path + "\0" + file_sha256 + "\0" + str(first) + "\0" + digest)
        result.append({"excerpt_id": "sx_" + identity[:24], "path": path,
            "start_line": first + 1, "end_line": last, "text": text,
            "sha256": digest, "observed_file_sha256": file_sha256,
            "unique_at_capture": after.count(text) == 1, "origin": "native_file_diff"})
        if len(result) == MAX_EXCERPTS:
            break
    return result

def excerpt_location(text: str, excerpt: dict) -> dict | None:
    sample = excerpt.get("text")
    if (not isinstance(sample, str) or not sample.strip() or len(sample) > MAX_EXCERPT_CHARS
            or excerpt.get("sha256") != text_hash(sample) or excerpt.get("unique_at_capture") is not True):
        return None
    if len(text) > MAX_TEXT:
        return None
    start = text.find(sample)
    if start < 0 or text.find(sample, start + 1) >= 0:
        return None
    # Coordinates must use the same splitlines contract as native file reads,
    # including CR-only files and text inserted before a sample on its line.
    boundaries = [0]
    for line in text.splitlines(keepends=True):
        boundaries.append(boundaries[-1] + len(line))
    return {"start_line": bisect_right(boundaries, start),
            "end_line": bisect_right(boundaries, start + len(sample) - 1)}

def capture_excerpts(root: Path, signal: dict) -> list[dict]:
    result = []
    raw = signal.get("knowledge_excerpts") or []
    if not isinstance(raw, list):
        return []
    for item in raw[:MAX_EXCERPTS]:
        if not isinstance(item, dict) or item.get("origin") != "native_file_diff":
            continue
        try:
            path = (root / str(item.get("path") or "")).resolve()
            relative = path.relative_to(root).as_posix()
            if not path.is_file() or path.stat().st_size > MAX_TEXT:
                continue
            text = path.read_bytes().decode("utf-8")
            location = excerpt_location(text, item)
            if location is None or not isinstance(item.get("excerpt_id"), str):
                continue
        except (OSError, ValueError):
            continue
        signal_id = str(signal.get("signal_id") or "")
        # Selection identity belongs to a real observation, even when two writes
        # happen to produce the same text/file version.
        reference = "sx_" + text_hash(signal_id + "\0" + item["excerpt_id"])[:24]
        result.append({**item, "native_excerpt_id": item["excerpt_id"], "excerpt_id": reference,
                       "path": relative, "signal_id": signal_id, "location_at_summary": location})
    return result

def select_excerpts(selected_sources: list[dict], references: list[str] | None) -> list[dict]:
    available = {item["excerpt_id"]: item for source in selected_sources
                 for item in source.get("available_excerpts", [])}
    if references is None:
        return []  # Legacy summaries conservatively keep whole-file freshness.
    if (not isinstance(references, list) or len(references) > MAX_EXCERPTS
            or any(not isinstance(ref, str) or ref not in available for ref in references)):
        raise ValueError("Excerpt references must belong to the selected real sources")
    return [available[ref] for ref in dict.fromkeys(references)]

def assess_excerpts(root: Path, excerpts: list[dict]) -> dict:
    locations = {}
    if (not isinstance(excerpts, list) or len(excerpts) > MAX_EXCERPTS
            or any(not isinstance(item, dict) for item in excerpts)):
        return {"state": "needs_recheck", "reason": "invalid_source_excerpts"}
    for item in excerpts:
        try:
            path = (root / str(item.get("path") or "")).resolve()
            path.relative_to(root)
            if not path.is_file() or path.stat().st_size > MAX_TEXT:
                return {"state": "needs_recheck", "reason": "source_excerpt_unavailable", "path": item.get("path")}
            location = excerpt_location(path.read_bytes().decode("utf-8"), item)
        except (OSError, ValueError):
            location = None
        if location is None:
            return {"state": "needs_recheck", "reason": "source_excerpt_changed_or_ambiguous", "path": item.get("path")}
        locations[str(item.get("excerpt_id") or "")] = location
    return {"state": "current", "reason": "source_excerpts_unchanged", "locations": locations}

def edit_touches_excerpts(before: str, after: str, excerpts: list[dict]) -> bool:
    """A native textual overlap hint, never a semantic dependency check."""
    if len(before) + len(after) > MAX_TEXT:
        return True  # Unknown/expensive comparisons stay conservative.
    ranges = [excerpt_location(before, item) for item in excerpts]
    if any(item is None for item in ranges):
        return True
    left, right = before.splitlines(keepends=True), after.splitlines(keepends=True)
    if len(left) + len(right) > 12_000:
        return True
    changes = [item for item in difflib.SequenceMatcher(None, left, right).get_opcodes() if item[0] != "equal"]
    for _, first, last, _, _ in changes:
        for span in ranges:
            start, end = span["start_line"] - 1, span["end_line"]
            if first == last:
                if start <= first <= end:
                    return True
            elif first < end and last > start:
                return True
    return False
