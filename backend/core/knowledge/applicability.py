"""State/evidence freshness checks for persisted lessons.

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
        detail = {**signal, **(signal.get("detail") if isinstance(signal.get("detail"), dict) else {})}
        raw_paths = [detail.get("file"), detail.get("path")]
        targets = detail.get("target_files") or []
        raw_paths.extend(targets if isinstance(targets, list) else [])
        if trigger:
            try:
                if (root / trigger).is_file():
                    raw_paths.append(trigger)
            except OSError:
                pass
        for raw in raw_paths:
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                path = (root / raw).resolve()
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if relative != ".":
                candidates.add(relative)
    files = {}
    for relative in sorted(candidates):
        try:
            files[relative] = _sha256(root / relative) if (root / relative).is_file() else ""
        except OSError:
            continue
    graph_path = root / ".gitgo" / "dependency_graph.v2.json"
    graph_digest = ""
    try:
        if graph_path.is_file():
            graph_digest = _sha256(graph_path)
    except OSError:
        pass
    from .excerpts import capture_excerpts
    from backend.core.workbench.materials import captured_bindings
    materials = [binding for signal in signals for binding in captured_bindings(root, signal)]
    return {
        **({"workbench_materials": materials} if materials else {}),
        "available_excerpts": [item for signal in signals for item in capture_excerpts(root, signal)],
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
    from .review import review_status
    review = review_status(lesson)
    if review != "accepted":
        return {"state": "needs_recheck", "reason": "lesson_" + review}
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
    if evidence.get("source_attribution") == "unresolved":
        return {"state": "needs_recheck", "reason": "harvest_sources_unresolved"}
    if evidence.get("scope") == "user_shared":
        from backend.core.storage import resolve_existing_storage_paths
        paths = resolve_existing_storage_paths(root)
        project_id = paths.project_id if paths is not None else ""
        confirmed = any(isinstance(item, dict) and item.get("project_id") == project_id
                        and item.get("kind") == "confirmation"
                        for item in getattr(lesson, "verifications", []))
        return {"state": "current" if confirmed else "needs_recheck",
                "reason": "confirmed_for_project" if confirmed else "shared_applicability_unverified",
                "tech_stack": lesson.tech_stack}
    if evidence.get("workbench_materials"):
        from backend.core.workbench.materials import assess_bindings
        assessment = assess_bindings(root, evidence["workbench_materials"])
        if assessment["state"] != "current":
            return assessment
    excerpts = evidence.get("workspace_excerpts") or []
    excerpt_paths = set()
    if excerpts:
        from .excerpts import assess_excerpts
        assessment = assess_excerpts(root, excerpts)
        if assessment["state"] != "current":
            return assessment
        excerpt_paths = {item["path"] for item in excerpts}
    for relative, expected in sorted((evidence.get("workspace_files") or {}).items()):
        if relative in excerpt_paths:
            continue  # Explicitly selected excerpts, not an inferred whole-file invariant.
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
