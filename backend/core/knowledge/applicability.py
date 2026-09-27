"""Host-side freshness checks for persisted lessons.

Lessons are durable guidance, not timeless truth.  Automatically harvested
lessons bind to the files and dependency graph observed when they were made;
changed evidence downgrades them to retrieval-only until revalidated.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_lesson_evidence(workspace: str | Path, signals: list[dict]) -> dict:
    root = Path(workspace).resolve()
    candidates: set[str] = set()
    for signal in signals:
        trigger = str(signal.get("trigger") or "").strip()
        detail = signal.get("detail") if isinstance(signal.get("detail"), dict) else {}
        raw_paths = [trigger, detail.get("file"), detail.get("path")]
        raw_paths.extend(detail.get("target_files") or [])
        for raw in raw_paths:
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                path = (root / raw).resolve()
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if path.is_file():
                candidates.add(relative)
    files = {}
    for relative in sorted(candidates):
        try:
            files[relative] = _sha256(root / relative)
        except OSError:
            continue
    graph_path = root / ".gitgo" / "dependency_graph.v2.json"
    graph_digest = ""
    try:
        if graph_path.is_file():
            graph_digest = _sha256(graph_path)
    except OSError:
        pass
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source_signal_ids": sorted({
            str(item.get("signal_id") or "") for item in signals
            if item.get("signal_id")
        }),
        "workspace_files": files,
        "dependency_graph_digest": graph_digest,
    }


def assess_lesson(lesson, workspace: str | Path) -> dict:
    root = Path(workspace).resolve()
    evidence = getattr(lesson, "evidence", None)
    source = str(getattr(lesson, "source", "manual") or "manual")
    superseded_by = str(getattr(lesson, "superseded_by", "") or "")
    if superseded_by:
        return {"state": "superseded", "reason": "replacement_recorded",
                "superseded_by": superseded_by}
    expires_at = str(getattr(lesson, "expires_at", "") or "")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                return {"state": "needs_recheck", "reason": "expired"}
        except ValueError:
            return {"state": "needs_recheck", "reason": "invalid_expiry"}
    if not isinstance(evidence, dict):
        if source == "auto_harvested":
            return {"state": "needs_recheck", "reason": "legacy_unbound_evidence"}
        return {"state": "current", "reason": "manual_guidance"}
    for relative, expected in sorted((evidence.get("workspace_files") or {}).items()):
        try:
            path = (root / str(relative)).resolve()
            path.relative_to(root)
            actual = _sha256(path) if path.is_file() else ""
        except (OSError, ValueError):
            actual = ""
        if actual != str(expected):
            return {"state": "needs_recheck", "reason": "source_file_changed",
                    "path": str(relative)}
    expected_graph = str(evidence.get("dependency_graph_digest") or "")
    if expected_graph:
        graph = root / ".gitgo" / "dependency_graph.v2.json"
        try:
            actual_graph = _sha256(graph) if graph.is_file() else ""
        except OSError:
            actual_graph = ""
        if actual_graph != expected_graph:
            return {"state": "needs_recheck", "reason": "dependency_graph_changed"}
    return {"state": "current", "reason": "evidence_matches"}
