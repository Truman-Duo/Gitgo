"""Explicit, durable deletion; reads never authorize maintenance.

The catalog uses StorageRuntime in a host-maintenance scope, outside the project
being removed. It is not another Agent/governance authority. Every destructive
attempt validates the frozen manifest and takes the existing exclusive lease.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from backend.core.config import ConfigManager
from backend.core.storage import StorageRuntime, get_storage
from backend.core.storage.bindings import release_storage
from backend.core.storage.paths import _default_state_home
from .services import OperationError

TERMINAL = {"completed", "failed", "cancelled", "timed_out", "killed", "orphaned"}


def blocked(message: str) -> OperationError:
    from backend.core.errors import error_payload
    info = error_payload("DELETION_BLOCKED", message=message, next_actions=[
        {"action": "inspect_plan"}, {"action": "stop_active_tasks_then_retry"},
    ])["error_info"]
    return OperationError("DELETION_BLOCKED", message, details=info)


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def checked_directory(raw: str, *, protected: list[Path]) -> Path:
    if not raw or not Path(raw).is_absolute():
        raise blocked("Deletion target must be an explicit absolute directory")
    target = Path(raw)
    resolved = target.resolve(strict=True)
    if target != resolved or target.is_symlink() or getattr(target, "is_junction", lambda: False)():
        raise blocked("Refusing redirected deletion target")
    if not resolved.is_dir() or len(resolved.parts) < 3:
        raise blocked("Refusing broad or non-directory deletion target")
    for item in [Path.home(), Path(resolved.anchor)]:
        item = item.resolve()
        if item == resolved or resolved in item.parents:
            raise blocked(f"Target contains protected location: {item}")
    for item in protected:
        item = item.resolve()
        if item == resolved or resolved in item.parents or (item.is_dir() and item in resolved.parents):
            raise blocked(f"Target overlaps protected location: {item}")
    return resolved


class DeletionService:
    def __init__(self, *, prepare_runtime=None):
        self.prepare_runtime = prepare_runtime or (lambda _project: None)

    @property
    def catalog(self):
        workspace = _default_state_home().parent / "maintenance-catalog"
        workspace.mkdir(parents=True, exist_ok=True)
        return get_storage(workspace)

    def _project(self, name):
        cfg = ConfigManager.load(strict=True)
        matches = [p for p in cfg.projects if p.name == name]
        if len(matches) != 1:
            raise blocked("Project identity is missing or ambiguous")
        return cfg, matches[0]

    def targets(self, project: str = "") -> dict:
        cfg = ConfigManager.load(strict=True)
        result = {"projects": [{"name": p.name, "workspace": p.workspace_path,
                   "archived": p.archived, "legacy_delete_requires_confirmation": bool(p.pending_hard_delete_at)}
                  for p in cfg.projects if p.archived], "processes": []}
        projects = [self._project(project)[1]] if project else list(cfg.projects)
        for p in projects:
            try:
                rows = get_storage(p.workspace_path).read_project_b_processes().values()
            except (OSError, RuntimeError, ValueError):
                continue
            result["processes"].extend(
                {**item, "project": p.name}
                for item in rows if bool(item.get("archived", False))
            )
        return result

    def _snapshot(self, project: str, scope: str, mode: str, process_id: str = "", *, storage=None) -> dict:
        if scope not in {"project", "process"} or mode not in {"soft", "hard"}:
            raise blocked("Invalid deletion scope/mode")
        cfg, p = self._project(project)
        protected = [ConfigManager.default_path(), _default_state_home()]
        protected += [Path(item.workspace_path) for item in cfg.projects if item.name != project and item.workspace_path]
        workspace = checked_directory(p.workspace_path, protected=protected)
        store = storage or get_storage(workspace)
        if store.paths.workspace != workspace:
            raise blocked("Storage is not bound to the selected workspace")
        all_rows = store.list_agent_process_links()
        b_processes = store.read_project_b_processes()
        session_id = ""
        ids: list[str] = []
        if scope == "process":
            b = b_processes.get(process_id)
            if not b:
                raise blocked("Select an existing B, not A")
            session_id = b["session_id"]
            ids = sorted(str(row["process_id"]) for row in all_rows if row["session_id"] == session_id)
            if any(row["parent_process_id"] in ids and row["process_id"] not in ids for row in all_rows):
                raise blocked("This B owns another B; remove that session explicitly first")
        worktrees = [item for item in store.list_worktree_states()
                     if item.get("isolated") and item.get("state") != "disposed"
                     and (scope == "project" or item.get("process_id") in ids)]
        if mode == "soft" and worktrees:
            raise blocked("Soft deletion preserves source, but this scope owns isolated worktrees. Promote/dispose them first or explicitly choose hard deletion.")
        stat = workspace.stat()
        terminal = {"completed", "failed", "cancelled", "timed_out", "killed", "orphaned"}
        selected_b = [b_processes[process_id]] if scope == "process" else list(b_processes.values())
        return {"project": project, "scope": scope, "mode": mode, "process_id": process_id,
                "session_id": session_id, "process_ids": ids,
                "b_process_count": len(selected_b),
                "unfinished_b_process_count": sum(
                    1 for item in selected_b
                    if str(item.get("status") or "").lower() not in terminal
                ),
                "project_id": store.paths.project_id, "workspace": str(workspace),
                "workspace_identity": [stat.st_dev, stat.st_ino],
                "state_root": str(store.paths.project_root), "state_home": str(store.paths.state_home),
                "project_config_hash": fingerprint(asdict(p)),
                "worktrees": worktrees,
                "effect": ("Remove project runtime/governance and unregister; " + ("remove workspace files." if mode == "hard" else "keep workspace files."))
                    if scope == "project" else "Remove this B session and all its execution records; preserve A, other B and shared-workspace files. A's historical evidence and numeric usage remain; unreferenced blobs use bounded GC."}

    def preview(self, project: str, scope: str = "project", mode: str = "soft", process_id: str = "") -> dict:
        manifest = self._snapshot(project, scope, mode, process_id)
        cfg, _ = self._project(project)
        delay_key = "process_delete_delay_minutes" if scope == "process" else "delete_delay_minutes"
        delay = int(cfg.safety.get(delay_key, 10))
        if not 0 <= delay <= 43200:
            raise blocked("Delete delay must be between 0 and 43200 minutes")
        manifest.update(plan_id=uuid.uuid4().hex, created_at_seconds=time.time(), delay_minutes=delay,
                        subject_id=manifest["project_id"] + ":" + (manifest["session_id"] or "project"))
        return self.catalog.create_deletion_plan(manifest, not_before=0)

    def _validate(self, manifest: dict, *, storage=None):
        current = self._snapshot(manifest["project"], manifest["scope"], manifest["mode"], manifest["process_id"], storage=storage)
        if any(current[key] != manifest[key] for key in current):
            raise blocked("Deletion targets changed after preview; cancel and preview again")

    def confirm(self, plan_id: str) -> dict:
        plan = self.catalog.read_deletion_plan(plan_id)
        m = plan["manifest"]
        if plan["state"] != "preview" or time.time() - m["created_at_seconds"] > 600:
            raise blocked("Preview expired or already acted upon; request a fresh preview")
        self._validate(m)
        return self.catalog.transition_deletion_plan(plan_id, "preview", "scheduled",
                  not_before=time.time() + m["delay_minutes"] * 60)

    def cancel(self, plan_id: str) -> dict:
        plan = self.catalog.read_deletion_plan(plan_id)
        if plan["state"] not in {"preview", "scheduled", "blocked"}:
            raise blocked("Executing/partially executed plans cannot be cancelled as if nothing happened")
        return self.catalog.transition_deletion_plan(plan_id, plan["state"], "cancelled")

    def retry(self, plan_id: str) -> dict:
        plan = self.catalog.read_deletion_plan(plan_id)
        if plan["state"] != "blocked":
            raise blocked("Only a preflight-blocked plan may retry; partial execution requires inspection")
        self._validate(plan["manifest"])
        return self.catalog.transition_deletion_plan(plan_id, "blocked", "scheduled", not_before=time.time())

    def run(self, plan_id: str) -> dict:
        catalog = self.catalog
        plan = catalog.read_deletion_plan(plan_id)
        if plan["state"] != "scheduled" or plan["not_before"] > time.time():
            raise blocked("Plan is not scheduled or is not due")
        # SQL compare-and-set owns this attempt across all Native Hosts.
        catalog.transition_deletion_plan(plan_id, "scheduled", "executing")
        m = plan["manifest"]
        mutated = False
        store = None
        try:
            self.prepare_runtime(m["project"])
            release_storage(m["workspace"])
            store = StorageRuntime(m["workspace"], state_home=m["state_home"], exclusive_maintenance=True)
            self._validate(m, storage=store)
            if store.list_incomplete_processes():
                raise blocked("Project has unfinished/recoverable tasks; finish or explicitly discard them first")
            # Revalidate every checkout before the first destructive step.
            worktree_manager = None
            if m["worktrees"]:
                from backend.core.loop.worktree import AgentWorktreeManager
                worktree_manager = AgentWorktreeManager(m["workspace"], store)
                for record in m["worktrees"]:
                    target = Path(record["path"]).resolve()
                    if target == worktree_manager.worktrees_root or worktree_manager.worktrees_root not in target.parents:
                        raise blocked("Unmanaged worktree in deletion manifest")
            mutated = True
            for record in m["worktrees"]:
                from backend.core.loop.worktree import WorktreeLease
                worktree_manager.dispose(WorktreeLease.from_dict(record), keep_ref=False)
            if m["scope"] == "process":
                store.purge_b_session(m["session_id"], plan_id)
            else:
                # A durable retirement barrier is checked by every new runtime.
                # Keep this tiny marker and the stable lease inode; Windows
                # cannot remove an open lock file, and removing it races readers.
                marker = store.paths.project_root / "project-deleted.json"
                with marker.open("x", encoding="utf-8") as handle:
                    json.dump({"plan_id": plan_id, "project_id": m["project_id"]}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                store.close()
                store = None
                root = Path(m["state_root"])
                for child in root.iterdir():
                    if child.name in {"project-deleted.json", "storage-maintenance.lock"}:
                        continue
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                target = Path(m["workspace"]) if m["mode"] == "hard" else Path(m["workspace"]) / ".gitgo"
                if target.exists():
                    checked_directory(str(target), protected=[ConfigManager.default_path(), _default_state_home()])
                    shutil.rmtree(target)
                cfg, p = self._project(m["project"])
                if fingerprint(asdict(p)) != m["project_config_hash"]:
                    raise blocked("Project config changed during deletion; manual reconciliation required")
                cfg.projects.remove(p)
                ConfigManager.save(cfg, allow_empty=True)
            return catalog.transition_deletion_plan(plan_id, "executing", "completed")
        except Exception as exc:
            return catalog.transition_deletion_plan(plan_id, "executing",
                "recovery_required" if mutated else "blocked", error=f"GITGO-E7501: {exc}")
        finally:
            if store is not None:
                store.close()

    def run_due(self) -> list[dict]:
        return [self.run(plan["plan_id"]) for plan in self.catalog.list_deletion_plans(limit=8, due_only=True)]
