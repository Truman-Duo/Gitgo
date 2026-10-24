"""Exact file candidates owned by the file service, independent of model output.

Projection has no target-file effects. The cooperating-writer lock protects
recheck/check/commit; arbitrary external writers remain outside that guarantee.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from .file_operations import metadata_mutation_error


@contextmanager
def file_service_scope(cancel_check, runtime=None):
    """Borrow the shared invocation runtime without serializing callbacks."""
    from backend.core.loop.service_runtime import tool_service_scope
    with tool_service_scope(cancel_check, runtime):
        yield


def file_service_runtime():
    from backend.core.loop.service_runtime import service_runtime
    return service_runtime()


def runtime_is_current():
    runtime = file_service_runtime()
    return runtime is None or runtime.is_current()


def cancellation_requested():
    from backend.core.loop.service_runtime import service_cancelled
    return service_cancelled()


@dataclass(frozen=True)
class FileMutation:
    operation: str
    workspace: Path
    path: Path
    before_sha256: str
    before: str
    after: str
    replacements: int = 0
    after_bytes: bytes | None = None
    text_view_available: bool = True

    @property
    def payload(self) -> bytes:
        return self.after.encode("utf-8") if self.after_bytes is None else self.after_bytes

    @property
    def after_sha256(self) -> str:
        # Absence is distinct from the SHA-256 of a present empty file.
        return "" if self.operation == "delete_file" else hashlib.sha256(self.payload).hexdigest()

    def binding(self) -> dict:
        return {"operation": self.operation, "path": str(self.path),
                "before_sha256": self.before_sha256,
                "candidate_sha256": self.after_sha256}


def commit_file_mutation(candidate: FileMutation, args: dict) -> dict:
    from .workspace_tools import _confined_path, _allowed_roots
    from backend.core.storage.maintenance import StorageLease, StorageMaintenanceActive

    denied = metadata_mutation_error(candidate.workspace, candidate.path) or metadata_mutation_error(candidate.workspace, args.get("path", candidate.path))
    if denied:
        return {**candidate.binding(), **denied}
    if not runtime_is_current():
        return {**candidate.binding(), "error": "FILE_CANDIDATE_STALE", "effect_state": "not_committed", "execution_state": "not_started"}
    if cancellation_requested():
        return {**candidate.binding(), "error": "TOOL_CANCELLED", "effect_state": "not_committed", "execution_state": "not_started"}

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
        inspection = inspect_file_candidate(candidate, args)
        if inspection.get("error"):
            return inspection
        return commit_checked_file(candidate, inspection["lesson_feedback"])


def inspect_file_candidate(candidate: FileMutation, args: dict) -> dict:
    """Common inspection for single-file and all-before-effects batch commits."""
    denied = metadata_mutation_error(candidate.workspace, candidate.path)
    if denied:
        return {**candidate.binding(), **denied}
    from backend.core.knowledge.prewrite import check_candidate
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
    return {"lesson_feedback": feedback}


def commit_checked_file(candidate: FileMutation, feedback: dict) -> dict:
    """Caller owns the writer lock and has completed candidate checks."""
    denied = metadata_mutation_error(candidate.workspace, candidate.path)
    if denied:
        return {**candidate.binding(), **denied}
    from .workspace_tools import _atomic_write, _rel, _text_diff
    path = candidate.path
    # Write precisely this frozen candidate. There is no second model call
    # and no regeneration of replacement parameters after inspection.
    current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""
    if current != candidate.before_sha256:
        return {"error": "FILE_CHANGED", "actual_sha256": current,
                "effect_state": "not_committed", "execution_state": "not_started",
                "lesson_feedback": feedback, **candidate.binding()}
    if not runtime_is_current():
        return {**candidate.binding(), "error": "FILE_CANDIDATE_STALE", "effect_state": "not_committed", "execution_state": "not_started"}
    if cancellation_requested():
        return {**candidate.binding(), "error": "TOOL_CANCELLED", "effect_state": "not_committed",
                "execution_state": "not_started", "lesson_feedback": feedback}
    try:
        deletion = None
        if candidate.operation == "delete_file":
            deletion = _commit_deletion(candidate)
            if deletion.get("error"):
                return {**candidate.binding(), "lesson_feedback": feedback, **deletion}
        else:
            _atomic_write(path, candidate.payload)
        observed = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""
    except OSError as exc:
        return {**candidate.binding(), "error": "FILE_MUTATION_FAILED", "detail": str(exc),
                "effect_state": "ambiguous", "execution_state": "completed",
                "lesson_feedback": feedback}
    result = {"path": _rel(candidate.workspace, path),
              "before_sha256": candidate.before_sha256, "after_sha256": observed,
              "candidate_sha256": candidate.after_sha256,
              "size": len(candidate.payload),
              "diff": _text_diff(_rel(candidate.workspace, path), candidate.before, candidate.after),
              "lesson_feedback": feedback}
    if deletion is not None:
        result.update(deletion)
    if feedback.get("lookup",{}).get("state")=="failed":
        result["lesson_recall_warning"]={"code":"LESSON_RECALL_UNAVAILABLE",
            "message":"File committed with ordinary advice unavailable; mandatory inventory/checks were evaluated separately. This is not an advice-check pass."}
    result["candidate_view"] = "text" if candidate.text_view_available else "binary"
    if observed == candidate.after_sha256 and deletion is None and candidate.text_view_available:
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
    elif deletion is None:
        result["action"] = "updated" if candidate.before_sha256 else "created"
    if observed != candidate.after_sha256:
        result.update(error="FILE_CHANGED_AFTER_COMMIT", effect_state="ambiguous")
    return result


def _commit_deletion(candidate: FileMutation) -> dict:
    """Recover exact bytes with a rename, without a copy/unlink fallback.

    This shares the cooperating-writer lock with edits and writes. External
    writers remain outside that lock: verify retained bytes and report an
    uncertain effect rather than deleting or rolling back an uninspected file.
    """
    import os
    import time
    import uuid
    from .workspace_tools import _confined_path
    trash_id = f"{time.time_ns()}-{uuid.uuid4().hex}"
    relative = candidate.path.relative_to(candidate.workspace)
    trash_relative = Path(".gitgo/trash/files") / trash_id / relative
    facts = {"trash_ref": trash_relative.as_posix(), "action": "deleted"}
    attempted = False
    try:
        trash = _confined_path(candidate.workspace, str(trash_relative), must_exist=False)
        trash.parent.mkdir(parents=True, exist_ok=True)
        attempted = True
        os.rename(candidate.path, trash)
        retained = trash.read_bytes()
        if hashlib.sha256(retained).hexdigest() != candidate.before_sha256:
            return {**facts, "error": "FILE_CHANGED_AFTER_COMMIT", "recoverable": True,
                    "effect_state": "ambiguous", "execution_state": "completed"}
        return {**facts, "recoverable": True, "size": len(retained)}
    except OSError as exc:
        return {**facts, "action": "delete_uncertain" if attempted else "delete_failed",
                "error": "FILE_MUTATION_FAILED", "detail": str(exc),
                "effect_state": "ambiguous" if attempted else "not_committed",
                "execution_state": "completed" if attempted else "not_started",
                "recovery_state": "unknown" if attempted else "not_started"}
