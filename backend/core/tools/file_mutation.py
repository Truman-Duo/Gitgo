"""Exact file candidates owned by the file service, independent of model output.

Projection has no target-file effects. The cooperating-writer lock protects
recheck/check/commit; arbitrary external writers remain outside that guarantee.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileMutation:
    operation: str
    workspace: Path
    path: Path
    before_sha256: str
    before: str
    after: str
    replacements: int = 0

    @property
    def after_sha256(self) -> str:
        return hashlib.sha256(self.after.encode("utf-8")).hexdigest()

    def binding(self) -> dict:
        return {"operation": self.operation, "path": str(self.path),
                "before_sha256": self.before_sha256,
                "candidate_sha256": self.after_sha256}


def commit_file_mutation(candidate: FileMutation, args: dict) -> dict:
    from .workspace_tools import _atomic_write, _confined_path, _allowed_roots, _rel, _text_diff
    from backend.core.storage.maintenance import StorageLease, StorageMaintenanceActive
    from backend.core.knowledge.prewrite import check_candidate

    # Runtime-injected lock root is shared by all cooperating project runners.
    # Direct service callers use a workspace-local fallback, not a sandbox.
    lock_root = Path(args.get("_mutation_lock_root") or candidate.workspace / ".gitgo" / "mutation-locks")
    key = hashlib.sha256(str(candidate.path).casefold().encode()).hexdigest()
    lock_dir = lock_root / key
    lock_dir.mkdir(parents=True, exist_ok=True)
    try:
        lease = StorageLease(lock_dir, exclusive=True)
    except StorageMaintenanceActive:
        return {"error": "FILE_MUTATION_BUSY", "effect_state": "not_committed",
                "execution_state": "not_started", **candidate.binding()}
    with lease:
        path = _confined_path(candidate.workspace, args.get("path", ""),
                             must_exist=False, allowed_roots=_allowed_roots(args))
        if path != candidate.path:
            return {"error": "FILE_CHANGED", "effect_state": "not_committed",
                    "execution_state": "not_started", **candidate.binding()}
        current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""
        if current != candidate.before_sha256:
            return {"error": "FILE_CHANGED", "actual_sha256": current,
                    "effect_state": "not_committed", "execution_state": "not_started",
                    **candidate.binding()}
        try:
            feedback = check_candidate(candidate, args)
        except Exception as exc:
            from backend.core.knowledge.prewrite import LessonInventoryUnavailable
            return {"error": "LESSON_CHECK_UNAVAILABLE", "detail": str(exc),
                    "effect_state": "not_committed", "execution_state": "not_started",
                    "lesson_feedback":{"lookup":{"state":"not_started","inventory":"unknown" if isinstance(exc,LessonInventoryUnavailable) else "complete"},
                        "mandatory_check_state":"unknown","advisory_state":"not_returned","error_type":type(exc).__name__},
                    **candidate.binding()}
        if feedback["blocked"]:
            code = ("LESSON_CHECK_UNAVAILABLE" if any(item["code"] == "LESSON_CHECK_UNAVAILABLE"
                    for item in feedback["diagnostics"]) else "KNOWN_LESSON_VIOLATION")
            return {"error": code, "effect_state": "not_committed",
                    "execution_state": "not_started", "lesson_feedback": feedback,
                    **candidate.binding(),
                    "next_actions": [{"action": candidate.operation,
                                      "effect": "Revise the proposed content using the diagnostic and resubmit against the current file version."}]}
        if feedback.get("needs_feedback"):
            feedback["advisory_state"]="returned_before_write"
            return {"error":"LESSON_FEEDBACK_REQUIRED","effect_state":"not_committed",
                    "execution_state":"not_started","lesson_feedback":feedback,**candidate.binding(),
                    "next_actions":[{"action":candidate.operation,"lesson_feedback_id":feedback["feedback_id"],
                        "effect":"Consider the advice before resubmitting. To keep this exact candidate, reference lesson_feedback_id after receiving it in a model request. It is not user approval or proof of understanding."}]}
        # Write precisely this frozen candidate. There is no second model call
        # and no regeneration of replacement parameters after inspection.
        current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""
        if current != candidate.before_sha256:
            return {"error": "FILE_CHANGED", "actual_sha256": current,
                    "effect_state": "not_committed", "execution_state": "not_started",
                    "lesson_feedback": feedback, **candidate.binding()}
        _atomic_write(path, candidate.after.encode("utf-8"))
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
        result = {"path": _rel(candidate.workspace, path),
                  "before_sha256": candidate.before_sha256, "after_sha256": observed,
                  "candidate_sha256": candidate.after_sha256,
                  "size": len(candidate.after.encode("utf-8")),
                  "diff": _text_diff(_rel(candidate.workspace, path), candidate.before, candidate.after),
                  "lesson_feedback": feedback}
        if feedback.get("lookup",{}).get("state")=="failed":
            result["lesson_recall_warning"]={"code":"LESSON_RECALL_UNAVAILABLE",
                "message":"File committed with ordinary advice unavailable; mandatory inventory/checks were evaluated separately. This is not an advice-check pass."}
        if observed == candidate.after_sha256:
            try:
                from backend.core.knowledge.excerpts import changed_excerpts
                result["knowledge_excerpts"] = changed_excerpts(
                    _rel(candidate.workspace, path), candidate.before, candidate.after, observed)
            except Exception as exc:
                # An observer must not turn a committed write into a retry.
                result["knowledge_capture_warning"] = {
                    "code": "SOURCE_EXCERPTS_UNAVAILABLE", "error_type": type(exc).__name__,
                    "message": "File committed; excerpt capture failed. Whole-file evidence remains the fallback."}
        if candidate.operation == "edit_file":
            result["replacements"] = candidate.replacements
        else:
            result["action"] = "updated" if candidate.before_sha256 else "created"
        if observed != candidate.after_sha256:
            result.update(error="FILE_CHANGED_AFTER_COMMIT", effect_state="ambiguous")
        return result
