"""Frozen document candidates committed by the existing file service."""
import base64
import hashlib
import json

from .file_mutation import FileMutation, commit_file_mutation, file_service_runtime, cancellation_requested
from .workspace_tools import _workspace, _confined_path, _allowed_roots
from .file_operations import metadata_mutation_error

_NAMESPACE = "document_candidates.v1"
_TEXT = {".txt", ".md", ".markdown"}
_BINARY = {".docx", ".pptx", ".xlsx", ".xlsm", ".pdf"}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fail(code, detail=""):
    from backend.core.errors import error_payload
    return {**error_payload(code, **({"message": detail[:2000]} if detail else {})),
            "effect_state": "not_committed", "execution_state": "not_started"}


def document_create(args):
    try:
        workspace = _workspace(args)
        denied = metadata_mutation_error(workspace, args.get("path", ""))
        if denied:
            return denied
        path = _confined_path(workspace, args.get("path", ""), must_exist=False, allowed_roots=_allowed_roots(args))
        denied = metadata_mutation_error(workspace, path)
        if denied:
            return denied
        suffix = path.suffix.lower()
        if suffix not in _TEXT | _BINARY:
            return _fail("DOCUMENT_FORMAT_UNSUPPORTED")
        before = b""
        existed = path.exists()
        if existed:
            from backend.core.sandbox_read import ReadRoot
            with ReadRoot(path.parent) as root, root.open(path.name) as opened:
                before = opened.read_bytes()
            digest = hashlib.sha256(before).hexdigest()
            if not args.get("overwrite") or args.get("expected_sha256") != digest:
                return _fail("DOCUMENT_WRITE_FAILED", "Existing output requires overwrite=true and the current expected_sha256")
        else:
            digest = ""
    except (OSError, UnicodeError, ValueError) as exc:
        return _fail("DOCUMENT_WRITE_FAILED", str(exc))
    if suffix in _TEXT:
        candidate = FileMutation("write_file", workspace, path, digest,
            before.decode("utf-8", "replace"), str(args.get("content") or ""))
        return _result(commit_file_mutation(candidate, args), suffix)
    return _binary_candidate(args, workspace, path, digest)


def _result(result, suffix):
    if not result.get("error"):
        result.update(sha256=result["after_sha256"], format=suffix.removeprefix("."))
    for action in result.get("next_actions", []):
        action["action"] = "document_create"
    return result


def _binary_candidate(args, workspace, path, before_digest):
    runtime = file_service_runtime()
    context = args.get("_lesson_context") or {}
    owner = {k: context.get(k, "") for k in ("task_id", "process_id", "session_id")}
    if runtime is None or not all(owner.values()):
        return _fail("FILE_SERVICE_REQUIRED", "Binary document candidates require the registered file-service runtime")
    storage = runtime.storage
    identity = {"owner": owner, "project_id": storage.paths.project_id, "workspace": str(workspace), "path": str(path)}
    specification = {k: args[k] for k in ("title", "content", "slides", "sheets") if k in args}
    # The private filename must not change user-visible default titles/sheets.
    specification["title"] = str(args.get("title") or path.stem)
    request = hashlib.sha256(_encoded({**identity, "specification": specification, "before_sha256": before_digest, "registration": runtime.registration})).hexdigest()
    key = hashlib.sha256(_encoded(identity)).hexdigest()
    from backend.core.storage.maintenance import StorageLease, StorageMaintenanceActive
    lock = storage.paths.state_home / "mutation-locks" / ("document-" + key)
    lock.mkdir(parents=True, exist_ok=True)
    try:
        lease = StorageLease(lock, exclusive=True)
    except StorageMaintenanceActive:
        return _fail("FILE_MUTATION_BUSY")
    with lease:
        try:
            reference = storage.get_state_ref(_NAMESPACE, key)
            record = json.loads(storage.read_blob(reference)) if reference else None
            valid = bool(record and record.get("version") == 1 and record.get("identity") == identity and record.get("request") == request)
            if not valid and args.get("lesson_feedback_id"):
                return _fail("FILE_CANDIDATE_STALE", "The original frozen document candidate is unavailable or has a different binding; inspect the current target and submit without the stale reference if regeneration is intended")
            if valid:
                if (record.get("render", {}).get("cleanup", {}).get("complete") is not True
                        or record.get("render", {}).get("access_retired") is not True):
                    return _fail("FILE_CANDIDATE_STALE", "Frozen candidate lacks confirmed native lifecycle evidence")
                raw = base64.b64decode(record["bytes"], validate=True)
                if hashlib.sha256(raw).hexdigest() != record["sha256"]:
                    return _fail("FILE_CANDIDATE_STALE", "Frozen candidate failed integrity verification")
                if len(raw) > 8 * 1024 * 1024:
                    return _fail("FILE_CANDIDATE_STALE", "Frozen candidate exceeds its bound")
                source = "frozen"
            else:
                if cancellation_requested():
                    return _fail("TOOL_CANCELLED")
                raw = runtime.render(specification, path.suffix.lower())
                record = {"version": 1, "identity": identity, "request": request,
                    "sha256": hashlib.sha256(raw).hexdigest(), "bytes": base64.b64encode(raw).decode("ascii"),
                    "render": runtime.last_render}
                reference = storage.put_blob(_encoded(record), media_type="application/vnd.gitgo.document-candidate+json")
                storage.put_state_ref(_NAMESPACE, key, reference)
                source = "generated"
        except Exception as exc:
            # Only native production / freezing has happened. Formal commit is
            # below this block, so these errors must never hide a target effect.
            return {**_fail(getattr(exc, "code", "DOCUMENT_WRITE_FAILED"), str(exc)),
                    "candidate_render": runtime.last_render}
        candidate = FileMutation("write_file", workspace, path, before_digest, "", "",
                                 after_bytes=raw, text_view_available=False)
        result = commit_file_mutation(candidate, args)
        result.update(candidate_ref=reference, candidate_source=source, candidate_render=record.get("render", {}))
        if not result.get("error"):
            try:
                storage.release_state_ref(_NAMESPACE, key, expected_ref=reference)
            except Exception as exc:
                result.setdefault("warnings", []).append({"code": "DOCUMENT_CANDIDATE_RETIRE_FAILED",
                    "message": "Document committed; pending candidate reference could not be retired. Preserve the completed effect.",
                    "error_type": type(exc).__name__})
        return _result(result, path.suffix.lower())
