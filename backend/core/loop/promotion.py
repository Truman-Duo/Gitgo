"""Task-scoped incremental promotion using canonical CAS/state revisions.

The record distinguishes a prepared filesystem effect from acknowledged state.
Reconciliation verifies the exact prepared postcondition; it never invents a
receipt for the interrupted call or overwrites unrelated current contents.
"""
import json
import os
import uuid
from pathlib import Path


def _tree(manager, commit, paths):
    if not paths:
        return {}
    raw = manager._git_bytes(manager.repo_root, "ls-tree", "-rz", commit, "--", *paths)
    entries = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, name = record.split(b"\t", 1)
        mode, kind, digest = meta.decode().split()
        name = name.decode("utf-8", errors="surrogateescape")
        if name in paths:
            entries[name] = (mode, digest)
    return entries


def _matches(manager, commit, paths):
    expected = _tree(manager, commit, paths)
    for name in paths:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            return False
        path = manager.repo_root / relative
        try:
            path.parent.resolve().relative_to(manager.repo_root)
        except ValueError:
            return False
        entry = expected.get(name)
        present = os.path.lexists(path)
        if entry is None:
            if present:
                return False
            continue
        if not present:
            return False
        mode, digest = entry
        if path.is_symlink():
            if mode != "120000":
                return False
            raw = manager._run(manager.repo_root, ["git", "hash-object", "--stdin"],
                               input_bytes=os.fsencode(os.readlink(path))).stdout.decode().strip()
        else:
            if not path.is_file() or mode not in {"100644", "100755"}:
                return False
            raw = manager._git_text(manager.repo_root, "hash-object", f"--path={name}", "--", name)
        if raw != digest:
            return False
    return True


def _verify_inputs(manager, integration, output_paths, dependencies):
    from .validation_evidence import file_version
    changed = []
    for dependency in dependencies:
        name = str(dependency.get("path") or "")
        try:
            root = integration if name in output_paths else manager.repo_root
            _name, actual, _size = file_version(root, name)
        except (OSError, ValueError):
            actual = None
        if not dependency.get("sha256") or actual != dependency["sha256"]:
            changed.append(name)
    if changed:
        from .worktree import WorktreeError
        raise WorktreeError("VALIDATION_INPUT_CHANGED: pinned inputs require recheck: " + ", ".join(sorted(set(changed))))


def promote(manager, *, snapshot_commit, ordered_commits, promotion_id, artifact_paths=(), input_dependencies=()):
    from .worktree import WorktreeError
    namespace = "agent_promotion"
    key = manager._ref_id(promotion_id)
    storage = manager.storage
    head_ref = storage.get_state_ref(namespace, key)
    prior = json.loads(storage.read_blob(head_ref)) if head_ref else {}

    def save(record, expected):
        ref = storage.put_blob(json.dumps(record, sort_keys=True).encode(), media_type="application/json")
        if not storage.commit_state_revision(namespace, key, ref, expected_ref=expected,
                revision_key=f"{key}:{uuid.uuid4().hex}"):
            raise WorktreeError("PROMOTION_STATE_CHANGED: reload the current promotion state")
        return ref

    if prior and prior.get("snapshot_commit") != snapshot_commit:
        raise WorktreeError("PROMOTION_SNAPSHOT_MISMATCH: promotion belongs to another task snapshot")
    reconciled = False
    if prior.get("state") == "prepared":
        if _matches(manager, prior["result_commit"], prior["paths"]):
            prior = {**{k: v for k, v in prior.items() if k != "previous"},
                     "state": "committed", "reconciled_effect": True}
            head_ref = save(prior, head_ref)
            reconciled = True
        elif _matches(manager, prior["before_commit"], prior["paths"]):
            prior = prior["previous"]
            head_ref = save(prior, head_ref)
        else:
            raise WorktreeError("PROMOTION_EFFECT_AMBIGUOUS: prepared effect differs from both recorded versions; inspect without replay")
    commits = list(dict.fromkeys(str(c) for c in ordered_commits if c))
    applied = list(prior.get("applied_commits", []))
    pending = [c for c in commits if c not in applied]
    base = prior.get("result_commit") or snapshot_commit
    integration = manager._target_path(f"promote-{key}-{uuid.uuid4().hex}")
    manager._add_detached(integration, base)
    result = None
    try:
        for commit in pending:
            manager._apply_business_commit(integration, commit)
            manager._commit(integration, f"gitgo integrate {promotion_id}")
        tip = manager._git_text(integration, "rev-parse", "HEAD")
        changed = [p.decode("utf-8", errors="surrogateescape") for p in manager._git_bytes(
            integration, "diff", "--name-only", "-z", base, tip, "--").split(b"\0") if p]
        paths = sorted(set(prior.get("paths", [])) | set(changed))
        # Validate dependencies against the candidate integrated output for
        # DAG-produced inputs, and against the live workspace for other inputs.
        dependencies = list({(d.get("process_id"), d.get("path"), d.get("sha256")): d
                             for d in [*(prior.get("input_dependencies") or []), *input_dependencies]}.values())
        _verify_inputs(manager, integration, paths, dependencies)
        if not _matches(manager, base, paths):
            raise WorktreeError("workspace changed after DAG snapshot on: " + ", ".join(paths))
        files, notices = manager._delivery_files(integration, changed,
            [*artifact_paths, *(paths if reconciled or not pending else [])])
        patch = manager._git_bytes(integration, "diff", "--binary", "--full-index", base, tip, "--")
        record = {"snapshot_commit": snapshot_commit, "before_commit": base, "result_commit": tip,
                  "applied_commits": list(dict.fromkeys([*applied, *pending])), "paths": paths,
                  "input_dependencies": dependencies}
        if pending or not prior:
            manager._git_text(manager.repo_root, "update-ref", manager._ref("promotions", key), tip)
            prepared = {**record, "state": "prepared", "previous": prior or {
                "state": "committed", "snapshot_commit": snapshot_commit, "result_commit": base,
                "applied_commits": applied, "paths": prior.get("paths", [])}}
            head_ref = save(prepared, head_ref)
            if patch:
                manager._git_input(manager.repo_root, patch, "apply", "--binary", "--whitespace=nowarn", "-")
            try:
                save({**record, "state": "committed"}, head_ref)
            except Exception as exc:
                raise WorktreeError("PROMOTION_METADATA_PENDING: filesystem effect applied; inspect or repeat the exact request for reconciliation") from exc
        result = {"promoted": True, "changed_files": changed, "commits": commits,
                  "applied_commits": pending, "already_promoted": [c for c in commits if c in applied],
                  "snapshot_commit": snapshot_commit, "result_commit": tip,
                  "reconciled_effect": reconciled, "files": files, "artifact_notices": notices}
        return result
    finally:
        try:
            manager._remove_path(integration)
        except WorktreeError as exc:
            if result is not None:
                result["cleanup_error"] = str(exc)
