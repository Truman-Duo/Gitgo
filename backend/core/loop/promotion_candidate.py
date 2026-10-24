"""Inspect a formal Git promotion using the common candidate/check/lock service.

Git still owns rich filesystem application (binary, modes, links), and the
existing promotion record owns recovery. No model callback or second ledger.
"""
import hashlib
import os
from pathlib import Path

from backend.core.sandbox_read import ReadRoot
from backend.core.tools.file_mutation import FileMutation, cancellation_requested, runtime_is_current
from backend.core.tools.file_mutation_batch import FileMutationBatch, checked_file_batch

_MAX_BYTES = 16 * 1024 * 1024
_MAX_TOTAL = 64 * 1024 * 1024


def _bytes(root, name, entry):
    if entry is None:
        return b""
    mode, _digest = entry
    path = root / name
    path.parent.resolve().relative_to(root)
    if mode == "120000":
        # Read the link itself; never load its target as candidate text.
        return os.fsencode(os.readlink(path))
    if mode not in {"100644", "100755"}:
        raise ValueError("unsupported promotion object type")
    with ReadRoot(root) as acquired, acquired.open(name) as opened:
        return opened.read_bytes(max_bytes=_MAX_BYTES)


def matches_record(manager, commit, paths, record, side):
    from .promotion import _matches, _tree
    if not _matches(manager, commit, paths):
        return False
    versions = record.get("file_versions")
    if versions is None:
        return True  # Legacy records prove Git representation only.
    try:
        tree = _tree(manager, commit, paths)
        return all((hashlib.sha256(_bytes(manager.repo_root, row["path"], tree[row["path"]])).hexdigest()
                    if row["path"] in tree else "") == row[side + "_sha256"] for row in versions)
    except (OSError, ValueError, KeyError):
        return False


def _raise(result):
    from .worktree import WorktreeError
    exc = WorktreeError(str(result["error"]) + ": formal promotion did not complete; inspect effect_state before retry")
    from backend.core.errors import error_payload
    exc.diagnostic = {**error_payload(str(result["error"]), message=str(result.get("detail") or str(exc)),
        state_changed=result.get("effect_state") in {"committed", "partial"}), **result}
    raise exc


def commit_promotion_patch(manager, integration, changed, patch, base, tip, args, prepare, *, guard_paths=(), input_paths=()):
    from .promotion import _tree, _matches
    root = manager.repo_root
    version_paths = list(dict.fromkeys([*guard_paths, *changed]))
    before_tree, after_tree = _tree(manager, base, version_paths), _tree(manager, tip, version_paths)
    candidates, used = [], 0
    try:
        if not 1 <= len(changed) <= 128:
            raise ValueError("promotion candidate must contain 1..128 files")
        for name in changed:
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValueError("unsafe promotion path")
            before = _bytes(root, name, before_tree.get(name))
            after = _bytes(integration, name, after_tree.get(name))
            used += len(before) + len(after)
            if used > _MAX_TOTAL:
                raise ValueError("promotion candidates exceed 64 MiB total")
            view = all(entry is None or entry[0] != "120000" for entry in
                       (before_tree.get(name), after_tree.get(name))) and b"\0" not in before + after
            try:
                before_text, after_text = before.decode("utf-8"), after.decode("utf-8")
            except UnicodeError:
                view, before_text, after_text = False, "", ""
            candidates.append(FileMutation("write_file" if name in after_tree else "delete_file",
                root, root / name, hashlib.sha256(before).hexdigest() if name in before_tree else "",
                before_text if view else "", after_text if view else "", after_bytes=after, text_view_available=view))
        # A Git patch binds modes/link transitions too; commit timestamps are
        # intentionally absent so identical retry projections have stable IDs.
        batch = FileMutationBatch(root, tuple(candidates), operation="promote_agent_changes",
                                  projection_version=hashlib.sha256(patch).hexdigest())
    except (OSError, ValueError) as exc:
        _raise({"error": "PROMOTION_CANDIDATE_UNAVAILABLE", "detail": str(exc),
                "effect_state": "not_committed", "execution_state": "not_started"})

    def digest_at(candidate, tree):
        name = candidate.path.relative_to(root).as_posix()
        return hashlib.sha256(_bytes(root, name, tree[name])).hexdigest() if name in tree else ""

    def versions_match():
        return _matches(manager, base, guard_paths or changed) and all(
            digest_at(c, before_tree) == c.before_sha256 for c in candidates)

    with checked_file_batch(batch, args, check_versions=versions_match,
                            guard_paths=[root / name for name in {*guard_paths, *input_paths}]) as inspection:
        if inspection.result.get("error"):
            _raise(inspection.result)
        versions = []
        by_name = {c.path.relative_to(root).as_posix(): c for c in candidates}
        for name in version_paths:
            candidate = by_name.get(name)
            before = (candidate.before_sha256 if candidate else
                hashlib.sha256(_bytes(root, name, before_tree[name])).hexdigest() if name in before_tree else "")
            versions.append({"path": name, "before_sha256": before,
                "after_sha256": candidate.after_sha256 if candidate else before})
        prepare(versions)  # Existing prepared revision owns exact raw versions too.
        if cancellation_requested() or not runtime_is_current():
            _raise({**inspection.result, "error": "TOOL_CANCELLED" if cancellation_requested() else "FILE_CANDIDATE_STALE",
                    "effect_state": "not_committed", "execution_state": "not_started"})
        try:
            manager._git_input(root, patch, "apply", "--binary", "--whitespace=nowarn", "-")
            if not _matches(manager, tip, changed):
                raise RuntimeError("formal output differs from the recorded Git candidate")
            for c in candidates:
                name = c.path.relative_to(root).as_posix()
                raw = _bytes(root, name, after_tree.get(name))
                actual = hashlib.sha256(raw).hexdigest() if name in after_tree else ""
                if actual != c.after_sha256:
                    raise RuntimeError("formal output bytes differ from frozen candidate")
        except Exception as exc:
            rows = []
            for c in candidates:
                name = c.path.relative_to(root).as_posix()
                state = "ambiguous"
                try:
                    if _matches(manager, tip, [name]) and digest_at(c, after_tree) == c.after_sha256:
                        state = "committed"
                    elif _matches(manager, base, [name]) and digest_at(c, before_tree) == c.before_sha256:
                        state = "not_committed"
                except Exception:
                    pass  # Observation failure cannot prove an effect absent.
                rows.append({**c.binding(), "path": name, "effect_state": state})
            states = {row["effect_state"] for row in rows}
            effect = "ambiguous" if "ambiguous" in states else "partial" if len(states) > 1 else next(iter(states))
            _raise({**inspection.result, "error": "PROMOTION_EFFECT_AMBIGUOUS", "detail": str(exc),
                    "file_results": rows, "effect_state": effect, "execution_state": "completed"})
        rows = [{**c.binding(), "path": c.path.relative_to(root).as_posix(), "after_sha256": c.after_sha256,
                 "effect_state": "committed", "execution_state": "completed",
                 **({"recovery_ref": f"git:{base}:{c.path.relative_to(root).as_posix()}", "recovery_kind": "git_baseline"}
                    if c.path.relative_to(root).as_posix() in before_tree else {})} for c in candidates]
        result = {**{key: value for key, value in inspection.result.items() if key != "files"},
                  "file_results": rows, "lesson_feedback": inspection.result["lesson_feedback"],
                  "effect_state": "committed", "knowledge_excerpts": []}
        try:
            from backend.core.knowledge.excerpts import changed_excerpts, MAX_EXCERPTS
            for c in candidates:
                if c.text_view_available and c.operation != "delete_file":
                    result["knowledge_excerpts"].extend(changed_excerpts(c.path.relative_to(root).as_posix(),
                        c.before, c.after, c.after_sha256)[:max(0, MAX_EXCERPTS - len(result["knowledge_excerpts"]))])
        except Exception as exc:
            result.setdefault("warnings", []).append({"code": "SOURCE_EXCERPTS_UNAVAILABLE", "error_type": type(exc).__name__,
                "message": "Promotion committed; source excerpt observation failed. Preserve the completed effect."})
        if result["lesson_feedback"]["lookup"]["state"] == "failed":
            result["lesson_recall_warning"] = {"code": "LESSON_RECALL_UNAVAILABLE",
                "message": "Promotion committed with ordinary advice unavailable; mandatory checks were evaluated separately."}
        return result
