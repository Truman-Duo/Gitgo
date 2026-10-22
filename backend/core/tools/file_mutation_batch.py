"""All-before-effects inspection, using the existing exact file service.

This is a cooperating-writer batch, not a filesystem transaction. A crash or
an external writer can leave partial/uncertain effects; the invocation journal
and per-file results retain that distinction. There is no automatic rollback.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .file_mutation import FileMutation, inspect_file_candidate, commit_checked_file, cancellation_requested, runtime_is_current


def _digest(members, field, projection_version=""):
    body = [[str(c.path), c.operation, getattr(c, field)] for c in members]
    if projection_version:
        body.append(["projection_version", projection_version])
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class FileMutationBatch:
    workspace: Path
    members: tuple[FileMutation, ...]
    operation: str = "apply_patch"
    projection_version: str = ""

    @property
    def path(self):
        return self.workspace

    @property
    def before_sha256(self):
        return _digest(self.members, "before_sha256", self.projection_version)

    @property
    def after_sha256(self):
        return _digest(self.members, "after_sha256", self.projection_version)

    def binding(self):
        return {"operation": self.operation, "before_sha256": self.before_sha256,
                "candidate_sha256": self.after_sha256,
                "files": [c.path.relative_to(self.workspace).as_posix() for c in self.members]}


def _current(candidate):
    from .workspace_tools import _confined_path
    path = _confined_path(candidate.workspace, str(candidate.path), must_exist=False)
    if path != candidate.path:
        raise ValueError("candidate path changed")
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _feedback(batch, inspections, args):
    from backend.core.knowledge.feedback import identity
    feedbacks = [r["lesson_feedback"] for r in inspections]
    diagnostics = [d for f in feedbacks for d in f.get("diagnostics", [])]
    diagnostics.sort(key=lambda d: d["level"] != "error")
    context = args.get("_lesson_context") or {}
    identifier = ("lf_" + hashlib.sha256(json.dumps([identity(batch, context, diagnostics),
        [f.get("feedback_id", "") for f in feedbacks]], sort_keys=True).encode()).hexdigest()) if diagnostics else ""
    reference = str(args.get("lesson_feedback_id") or "")
    exposed = reference in context.get("feedback_in_provider_input", [])
    continued = bool(reference and reference == identifier and exposed)
    states = [f.get("mandatory_check_state", "unknown") for f in feedbacks]
    mandatory = next((s for s in ("unknown", "failed", "violation", "passed") if s in states), "not_required")
    inventory_unknown = any(f.get("lookup", {}).get("inventory") == "unknown" for f in feedbacks)
    lookup_failed = any(f.get("lookup", {}).get("state") == "failed" for f in feedbacks)
    blocked = any(r.get("error") not in (None, "LESSON_FEEDBACK_REQUIRED") for r in inspections)
    needs = any(f.get("needs_feedback") for f in feedbacks) and not continued and not blocked
    return {"phase": "before_write", "candidate_sha256": batch.after_sha256,
            "blocked": blocked, "needs_feedback": needs, "feedback_id": identifier,
            "diagnostics": diagnostics[:20], "diagnostic_count": sum(f.get("diagnostic_count", 0) for f in feedbacks),
            "diagnostic_coverage": "partial" if len(diagnostics) > 20 or any(f.get("diagnostic_coverage") == "partial" for f in feedbacks) else "complete",
            "lookup": {"state": "not_started" if inventory_unknown else "failed" if lookup_failed else "completed",
                       "inventory": "unknown" if inventory_unknown else "complete", "scope": "ordinary_advice", "mode": "native_candidate_lookup"},
            "mandatory_check_state": mandatory,
            "advisory_state": "unavailable" if lookup_failed else "continuation_requested" if continued else "returned_before_write" if needs else "not_required",
            "continuation_state": "not_requested" if not reference else "reference_not_exposed" if not exposed else "binding_changed" if reference != identifier else "same_candidate_requested",
            "user_override": all(f.get("user_override", False) for f in feedbacks),
            "model_understanding": "unknown", "model_adoption": "unknown"}


@dataclass(frozen=True)
class BatchInspection:
    result: dict
    member_feedbacks: tuple[dict, ...] = ()


@contextmanager
def file_writer_locks(workspace, paths, args):
    """The same cross-process lock names for commit and reconciliation."""
    from backend.core.storage.maintenance import StorageLease
    root = Path(args.get("_mutation_lock_root") or workspace / ".gitgo/mutation-locks")
    with ExitStack() as owners:
        for path in sorted(set(paths), key=lambda p: str(p).casefold()):
            directory = root / hashlib.sha256(str(path).casefold().encode()).hexdigest()
            directory.mkdir(parents=True, exist_ok=True)
            owners.enter_context(StorageLease(directory, exclusive=True))
        yield


@contextmanager
def checked_file_batch(batch: FileMutationBatch, args: dict, *, check_versions=None, guard_paths=()):
    """Hold existing writer locks through inspection and a trusted commit.

    A specialized service may supply its own version check for richer objects
    (Git modes/links). This callback is runtime code, never model JSON.
    The service still owns actual commit/effect verification and recovery.
    """
    from backend.core.storage.maintenance import StorageMaintenanceActive
    if not batch.members or len(batch.members) > 128:
        raise ValueError("file batch must contain 1..128 members")
    paths = [c.path for c in batch.members]
    if len(set(paths)) != len(paths) or any(a in b.parents for a in paths for b in paths if a != b):
        raise ValueError("overlapping file candidates")
    binding = batch.binding()
    def failed(code, **extra):
        return BatchInspection({**binding, "error": code, "effect_state": "not_committed", "execution_state": "not_started", **extra})
    from .file_operations import metadata_mutation_error
    if any(metadata_mutation_error(batch.workspace, path) for path in paths):
        from backend.core.errors import error_payload
        yield BatchInspection({**binding, **error_payload("PROTECTED_PATH"), "effect_state": "not_committed", "execution_state": "not_started"})
        return
    if cancellation_requested() or not runtime_is_current():
        yield failed("TOOL_CANCELLED" if cancellation_requested() else "FILE_CANDIDATE_STALE")
        return
    with ExitStack() as owners:
        try:
            owners.enter_context(file_writer_locks(batch.workspace, [*paths, *guard_paths], args))
        except StorageMaintenanceActive:
            yield failed("FILE_MUTATION_BUSY")
            return
        def versions_match():
            try:
                return (check_versions() if check_versions is not None
                        else all(_current(c) == c.before_sha256 for c in batch.members))
            except (OSError, ValueError):
                return False
        if not versions_match():
            yield failed("FILE_CHANGED")
            return
        # The model's single feedback reference belongs to the complete batch,
        # never to a subset or to one member's earlier single-file advisory.
        member_args = {k: v for k, v in args.items() if k != "lesson_feedback_id"}
        inspections = [inspect_file_candidate(c, member_args) for c in batch.members]
        feedback = _feedback(batch, inspections, args)
        if feedback["blocked"]:
            code = "LESSON_CHECK_UNAVAILABLE" if any(r.get("error") == "LESSON_CHECK_UNAVAILABLE" for r in inspections) else "KNOWN_LESSON_VIOLATION"
            yield failed(code, lesson_feedback=feedback)
            return
        if feedback["needs_feedback"]:
            yield failed("LESSON_FEEDBACK_REQUIRED", lesson_feedback=feedback, next_actions=[{
                "action": batch.operation, "lesson_feedback_id": feedback["feedback_id"],
                "effect": "Consider the advice, then revise or resubmit the complete exact candidate batch after receiving this feedback in a model request. This is not approval."}])
            return
        if not versions_match():
            yield failed("FILE_CHANGED", lesson_feedback=feedback)
            return
        if cancellation_requested() or not runtime_is_current():
            yield failed("TOOL_CANCELLED" if cancellation_requested() else "FILE_CANDIDATE_STALE", lesson_feedback=feedback)
            return
        yield BatchInspection({**binding, "lesson_feedback": feedback}, tuple(r["lesson_feedback"] for r in inspections))


def commit_file_batch(batch: FileMutationBatch, args: dict, *, check_versions=None, guard_paths=()) -> dict:
    with checked_file_batch(batch, args, check_versions=check_versions, guard_paths=guard_paths) as inspected:
        if inspected.result.get("error"):
            return inspected.result
        binding = batch.binding()
        feedback = inspected.result["lesson_feedback"]
        member_feedbacks = inspected.member_feedbacks
        rows = []
        excerpts = []
        warnings = []
        def observations():
            return {"knowledge_excerpts": excerpts, **({"warnings": warnings} if warnings else {})}
        for c, member_feedback in zip(batch.members, member_feedbacks):
            try:
                if _current(c) != c.before_sha256:
                    result = {"error": "FILE_CHANGED", "effect_state": "not_committed"}
                else:
                    result = commit_checked_file(c, member_feedback)
            except Exception as exc:
                # Once a commit is attempted, a consumer/IO exception cannot
                # prove that the target was unchanged.
                result = {"error": "FILE_MUTATION_FAILED", "detail": type(exc).__name__, "effect_state": "ambiguous"}
            if not result.get("error"):
                from backend.core.knowledge.excerpts import MAX_EXCERPTS
                excerpts.extend(result.get("knowledge_excerpts", [])[:max(0, MAX_EXCERPTS - len(excerpts))])
            for key in ("knowledge_capture_warning", "lesson_recall_warning"):
                if result.get(key) and len(warnings) < 16:
                    warnings.append(result[key])
            row = {**c.binding(), "path": c.path.relative_to(batch.workspace).as_posix(),
                   "effect_state": result.get("effect_state", "committed"),
                   "execution_state": result.get("execution_state", "not_started" if result.get("effect_state") == "not_committed" else "completed"),
                   **{k: result[k] for k in ("error", "after_sha256", "trash_ref", "recoverable", "recovery_state") if k in result}}
            rows.append(row)
            if result.get("error"):
                committed = any(r["effect_state"] == "committed" for r in rows)
                uncertain = any(r["effect_state"] == "ambiguous" for r in rows)
                rows.extend({**rest.binding(), "path": rest.path.relative_to(batch.workspace).as_posix(),
                             "effect_state": "not_committed", "execution_state": "not_started"}
                            for rest in batch.members[len(rows):])
                return {**binding, "error": result["error"], "file_results": rows,
                        "effect_state": "ambiguous" if uncertain else "partial" if committed else "not_committed",
                        "execution_state": "completed", "lesson_feedback": feedback, **observations()}
        result = {**binding, "after_sha256": batch.after_sha256, "file_results": rows,
                  "count": len(rows), "applied": True, "effect_state": "committed", "lesson_feedback": feedback, **observations()}
        if feedback["lookup"]["state"] == "failed":
            result["lesson_recall_warning"] = {"code": "LESSON_RECALL_UNAVAILABLE",
                "message": "Patch committed with ordinary advice unavailable; this is not an advice-check pass."}
        return result
