"""Content-bound review, independent of knowledge tier and freshness."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

_CONTENT_FIELDS = ("project_name", "abstract", "trigger", "rule", "category", "severity", "tech_stack",
                   "dangerous_tools", "prerequisite_tools", "required_tools",
                   "check", "checker_revision", "evidence", "expires_at", "qualification")


def content_version(lesson) -> str:
    from .models import Lesson
    raw = lesson.to_dict() if hasattr(lesson, "to_dict") else dict(lesson)
    # Persisted models omit empty/default fields. Hash the same canonical model
    # on both sides so a user edit that clears tool hints keeps its exact receipt.
    raw = Lesson.from_dict(raw).to_dict()
    return hashlib.sha256(json.dumps({key: raw.get(key) for key in _CONTENT_FIELDS if key!="checker_revision" or raw.get(key)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def review_status(lesson) -> str:
    review = getattr(lesson, "review", None)
    if not isinstance(review, dict) or review.get("status") != "accepted":
        return "needs_review"
    if review.get("kind") not in {"user_proposal", "explicit_verification", "author_supplied", "explicit_share"}:
        return "needs_review"
    return "accepted" if review.get("content_version") == content_version(lesson) else "content_changed"


def can_consume(lesson) -> bool:
    return review_status(lesson) == "accepted"


def reviewed_record(raw: dict, *, kind: str, reference: str) -> dict:
    result = dict(raw)
    result["review"] = {"status": "accepted", "kind": kind, "reference": reference,
                        "content_version": content_version(raw),
                        "reviewed_at": datetime.now(timezone.utc).isoformat()}
    return result


def mark_reviewed(lesson, *, kind: str, reference: str):
    lesson.review = reviewed_record(lesson.to_dict(), kind=kind, reference=reference)["review"]
    return lesson

def proposal_version(project: str, proposal_id: str, signal_ids: list[str], candidates: list[dict]) -> str:
    return hashlib.sha256(json.dumps({"project": project, "proposal_id": proposal_id,
        "signal_ids": sorted(signal_ids), "candidates": candidates}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
