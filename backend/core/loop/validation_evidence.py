"""Version witnesses for validation, not a second state store or semantic oracle.

Snapshots reuse the dependency scanner's generated-directory exclusions, but
never silently truncate. A bounded or unreadable scope is unknown, not current.
All records travel through existing tool receipts/checkpoints/worktree leases.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from backend.core.dependency_graph import _IGNORED_PARTS

MAX_FILES = 4096
MAX_BYTES = 128 * 1024 * 1024


def file_version(root, name):
    root = Path(root).resolve()
    path = root / str(name)
    relative = path.relative_to(root).as_posix()
    path.resolve().relative_to(root)
    before = path.stat()
    if not path.is_file() or before.st_size > MAX_BYTES:
        raise ValueError("validation input is not a bounded file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        total = 0
        for chunk in iter(lambda: stream.read(128 * 1024), b""):
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError("validation input exceeds byte budget")
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("validation input changed while reading")
    return relative, digest.hexdigest(), total


def workspace_version(workspace, *, input_files=None):
    """Hash a declared input set, or a bounded business-workspace scope.

    Environment/network inputs are outside this local-file witness. Explicit
    scopes remain explicit; they never claim to describe an entire project.
    """
    if not workspace:
        return {"state": "unavailable", "reason": "workspace unavailable"}
    root = Path(workspace).resolve()
    scope = "workspace" if input_files is None else "declared_inputs"
    if input_files is not None and (not isinstance(input_files, (list, tuple)) or not all(isinstance(p, str) and p for p in input_files)):
        return {"state": "unavailable", "reason": "invalid validation input scope"}
    paths = None if input_files is None else sorted(set(input_files))
    result = {"schema_version": 1, "workspace": str(root), "scope": scope, "input_files": paths}
    files = {}
    try:
        if not root.is_dir():
            raise ValueError("workspace unavailable")
        if paths is None:
            names = []
            def fail(error):
                raise error
            for directory, dirs, entries in os.walk(root, followlinks=False, onerror=fail):
                dirs[:] = sorted(d for d in dirs if d.casefold() not in _IGNORED_PARTS)
                if any((Path(directory) / d).is_symlink() for d in dirs):
                    raise ValueError("directory symlink requires an explicit validation scope")
                names.extend((Path(directory) / name).relative_to(root).as_posix()
                             for name in sorted(entries) if name.casefold() not in _IGNORED_PARTS)
                if len(names) > MAX_FILES:
                    raise ValueError("validation scope exceeds file budget; declare input_files")
        else:
            names = paths
        if len(names) > MAX_FILES:
            raise ValueError("validation scope exceeds file budget")
        size = 0
        for name in names:
            relative, digest, count = file_version(root, name)
            files[relative] = digest
            size += count
            if size > MAX_BYTES:
                raise ValueError("validation scope exceeds byte budget; declare input_files")
        encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        return {**result, "state": "observed", "files": files,
                "digest": hashlib.sha256(encoded).hexdigest()}
    except (OSError, ValueError, TypeError) as exc:
        return {**result, "state": "unavailable", "reason": str(exc)}


def version_summary(version):
    if not isinstance(version, dict):
        return None
    return {**{k: v for k, v in version.items() if k != "files"},
            "file_count": len(version["files"]) if "files" in version else version.get("file_count", 0)}


def assess_version(version, workspace, *, cache=None):
    if not isinstance(version, dict) or version.get("state") != "observed":
        return {"state": "needs_recheck", "reason": "validation_version_unavailable"}
    if version.get("workspace") != str(Path(workspace).resolve()):
        return {"state": "needs_recheck", "reason": "validation_workspace_changed"}
    key = "workspace:" + json.dumps([str(Path(workspace).resolve()), version.get("input_files")], sort_keys=True)
    current = cache.get(key) if cache is not None else None
    if current is None:
        current = workspace_version(workspace, input_files=version.get("input_files"))
        if cache is not None:
            cache[key] = current
    if current.get("state") != "observed":
        return {"state": "needs_recheck", "reason": current.get("reason", "validation_unavailable")}
    if current.get("digest") != version.get("digest"):
        before, after = version.get("files", {}), current["files"]
        changed = (sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
                   if "files" in version else [])
        return {"state": "needs_recheck", "reason": "validation_inputs_changed", "changed_files": changed[:50]}
    return {"state": "current", "scope": version.get("scope")}


def test_receipt_assessment(process, receipt, *, cache=None):
    workspace = process.worktree_path or process.workspace_root
    if receipt.get("task_id") and receipt["task_id"] != (process.active_task_id or process.process_id):
        return {"state": "needs_recheck", "reason": "validation_task_changed"}
    # A later failing run supersedes a green even if no source file changed.
    # Successful same-version seed runs remain composable.
    # Keep this chronology check outside the per-evaluation content cache.
    rows = []
    def visit(items):
        for item in items:
            if item.get("task_id") in (None, "", process.active_task_id or process.process_id):
                rows.append(item)
                visit(item.get("child_receipts") or [])
    visit(process.tool_receipts)
    latest = next((item for item in reversed(rows)
                   if item.get("tool_name") == "run_test" and item.get("test_id") == receipt.get("test_id")), None)
    if (latest is not None and latest.get("receipt_id") != receipt.get("receipt_id")
            and (latest.get("succeeded") is not True or latest.get("test_passed") is not True
                 or latest.get("test_target") != receipt.get("test_target"))):
        return {"state": "needs_recheck", "reason": "superseded_test_run"}
    version = receipt.get("validation_version")
    key = "assessment:" + json.dumps(version, sort_keys=True)
    if cache is not None and key in cache:
        return cache[key]
    result = assess_version(version, workspace, cache=cache)
    if cache is not None:
        cache[key] = result
    return result


def review_target_version(process):
    from .completion_gates import contract_digest
    lease = process.worktree or {}
    record = {"process_id": process.process_id,
              "task_id": process.active_task_id or process.process_id,
              "contract_digest": contract_digest(process)}
    if lease.get("result_commit") and lease.get("state") in {"sealed", "disposed"}:
        return {**record, "sealed_commit": lease["result_commit"]}
    workspace = process.worktree_path or process.workspace_root
    return {**record, "workspace_version": version_summary(workspace_version(workspace))}


def review_is_current(process, expected):
    if not isinstance(expected, dict):
        return False
    current = review_target_version(process)
    if any(expected.get(key) != current.get(key) for key in ("process_id", "task_id", "contract_digest", "sealed_commit")):
        return False
    if current.get("sealed_commit"):
        return True
    prior, observed = expected.get("workspace_version") or {}, current.get("workspace_version") or {}
    return (prior.get("state") == observed.get("state") == "observed"
            and prior.get("workspace") == observed.get("workspace")
            and prior.get("digest") == observed.get("digest"))


def has_current_review(process):
    records = process.runtime_preferences.get("review_evidence", {})
    return any(review_is_current(process, records.get(reviewer)) for reviewer in process.review_approvals)


def pinned_inputs(process):
    """Only explicit native read dependencies, never every incidental read."""
    task = process.active_task_id or process.process_id
    result = {}
    def collect(receipts):
        for receipt in receipts:
            if receipt.get("task_id") not in (None, "", task):
                continue
            item = receipt.get("input_dependency")
            if receipt.get("succeeded") and receipt.get("committed") and isinstance(item, dict):
                result[item["path"]] = dict(item)
            collect(receipt.get("child_receipts") or [])
    collect(process.tool_receipts)
    return list(result.values())
