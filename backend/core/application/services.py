"""Canonical Gitgo application service layer.

This module deliberately has no dependency on MCP, Dashboard, or any other
transport.  Native hosts and compatibility adapters call the same operations.
"""

from __future__ import annotations

import inspect
import base64
import hashlib
import json
import os
import threading
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from backend.core.config import ConfigManager


_GOVERNANCE_TRACE_EVENTS = {
    "governance_snapshot", "completion_gate", "decision_required", "decision_resumed",
    "user_decision_requested", "user_decision_received", "context_compaction_requested",
    "context_compaction_completed", "context_compaction_failed", "context_window_action",
    "context_window_exceeded", "task_bundle_delegated", "agent_dag_admitted", "agent_dag_admission_failed",
    "deadline_extended", "repository_scope_blocked", "repository_scope_warning",
    "session_recovery_blocked", "session_recovery_resumed", "session_recovery_failed",
    "session_recovery_discarded", "storage_health", "worktree_leased", "worktree_sealed",
    "worktree_promoted", "worktree_cleanup_failed", "worktree_unavailable",
}


def _feed_cursor_encode(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _feed_cursor_decode(value: str) -> dict:
    if not value:
        return {"history": None, "trace": None}
    try:
        padding = "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(value + padding).decode())
        if not isinstance(decoded, dict):
            raise ValueError
        return {"history": decoded.get("history"), "trace": decoded.get("trace")}
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperationError("INVALID_CURSOR", "governance feed cursor is invalid") from exc


class OperationError(RuntimeError):
    """Stable application error surfaced through a transport envelope."""

    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


def _project(project_name: str):
    cfg = ConfigManager.load()
    for project in cfg.projects:
        if project.name == project_name:
            _external_workspace(getattr(project, "workspace_path", ""))
            return cfg, project
    raise OperationError("PROJECT_NOT_FOUND", f"Project '{project_name}' was not found")


def _review_harvest_candidates(candidates: list[dict], reviews: list[dict]) -> list[dict]:
    """Validate the entire explicit user review before writing any candidate.

    The same confirmation operation can discard individual suggestions or
    revise their wording; editing cannot grant executable policy authority.
    """
    def invalid():
        raise OperationError("INVALID_HARVEST_REVIEW",
            "Review every candidate once using its index and accept/discard; only trigger, rule and qualification may be revised.")
    if not isinstance(reviews, list) or len(reviews) != len(candidates):
        invalid()
    seen, accepted = set(), []
    for review in reviews:
        if not isinstance(review, dict) or set(review) - {"index", "action", "trigger", "rule", "qualification"}:
            invalid()
        index = review.get("index")
        if type(index) is not int or index < 0 or index >= len(candidates) or index in seen:
            invalid()
        seen.add(index)
        if not isinstance(review.get("action"), str) or review["action"] not in {"accept", "discard"}:
            invalid()
        if review["action"] == "discard":
            if set(review) & {"trigger", "rule", "qualification"}:
                invalid()
            continue
        raw = dict(candidates[index])
        changed = False
        for key, minimum, maximum in (("trigger", 1, 240), ("rule", 20, 2000)):
            if key not in review:
                continue
            value = review[key]
            if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
                invalid()
            raw[key] = value.strip()
            changed = changed or raw[key] != candidates[index].get(key)
        if "qualification" in review:
            from backend.core.knowledge.models import normalize_qualification, QUALIFICATION_KEYS
            value = review["qualification"]
            if not isinstance(value, dict) or set(value) - set(QUALIFICATION_KEYS):
                invalid()
            try:
                normalized = normalize_qualification(value)
            except ValueError:
                invalid()
            raw["qualification"] = normalized
            changed = changed or normalized != candidates[index].get("qualification")
        if changed:
            from datetime import datetime, timezone
            raw["resolution_history"] = [*(raw.get("resolution_history") or []), {
                "kind": "candidate_user_revision", "at": datetime.now(timezone.utc).isoformat(),
                "previous_trigger": candidates[index].get("trigger", ""),
                "previous_rule": candidates[index].get("rule", ""),
                "previous_qualification": candidates[index].get("qualification"),
            }]
            # Tool names inferred from one example must not survive a revised
            # rule as independent workflow authority.
            for key in ("dangerous_tools", "prerequisite_tools", "required_tools"):
                raw[key] = []
        raw["check"] = None
        accepted.append(raw)
    return accepted


def _external_workspace(path):
    from backend.core.update.installation import assert_external_resource
    from backend.core.update.validation import UpdateError
    try: assert_external_resource(Path(path or ""))
    except UpdateError:
        raise OperationError("UNSAFE_WORKSPACE_PATH", "Project workspace must stay outside the managed software installation") from None


def _public_provider(provider: dict) -> dict:
    from backend.core.llm_config import LLMConfigManager
    result = {k: v for k, v in provider.items() if k not in {"api_key", "secret_ref"}}
    key = str(provider.get("api_key", ""))
    result["api_key_present"] = bool(key)
    result["revision"] = LLMConfigManager.revision(provider)
    result["api_key_display"] = (
        key[:4] + "***" + key[-4:] if len(key) > 8 else ("***" if key else "")
    )
    return result


def _normalize_provider_base_url(value: str) -> str:
    """Canonicalize an endpoint without inventing a provider-specific path."""
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OperationError(
            "INVALID_PROVIDER_URL",
            "Base URL must be an absolute http:// or https:// endpoint.",
        )
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, parsed.query, ""))


class ApplicationServices:
    """Dispatches versioned, transport-neutral application operations."""

    def __init__(self, *, daemon_online: Callable[[str], bool] | None = None, prepare_deletion=None):
        self._daemon_online = daemon_online or (lambda _name: False)
        self._write_lock = threading.RLock()
        from backend.core.provider_drafts import ProviderDrafts
        self.provider_drafts = ProviderDrafts()
        from backend.core.update.preparation_service import UpdatePreparationService
        self.updates = UpdatePreparationService()
        from backend.core.loop.publish_queue import PublishQueue
        self.publish_runtime = PublishQueue()
        from .deletion import DeletionService
        self.deletion = DeletionService(prepare_runtime=prepare_deletion)
        self._operations: dict[str, Callable[..., Any]] = {
            "project.list": self.project_list,
            "project.create": self.project_create,
            "project.list_archived": self.project_list_archived,
            "project.archive": self.project_archive,
            "project.enter": self.project_enter,
            "project.delete": self.project_delete,
            "project.cancel_delete": self.project_cancel_delete,
            "process.rename": self.process_rename,
            "process.archive": self.process_archive,
            "bin.targets": self.deletion.targets,
            "deletion.preview": self.deletion.preview,
            "deletion.confirm": self.deletion.confirm,
            "deletion.cancel": self.deletion.cancel,
            "deletion.retry": self.deletion.retry,
            "deletion.list": lambda: self.deletion.catalog.list_deletion_plans(),
            "deletion.run": self.deletion.run,
            "config.get": self.config_get,
            "product.version": self.product_version,
            "update.status": self.update_status,
            "update.configure": self.update_configure,
            "update.check": self.update_check,
            "update.download": self.update_download,
            "update.cancel": self.update_cancel,
            "update.request_apply": self.update_request_apply,
            "update.request_rollback": self.update_request_rollback,
            "update.cancel_pending": self.update_cancel_pending,
            "config.set": self.config_set,
            "config.terminals": self.config_terminals,
            "config.web_search.test": self.config_web_search_test,
            "publish.get": self.publish_get,
            "publish.set": self.publish_set,
            "publish.items": self.publish_items,
            "publish.item": self.publish_item,
            "publish.task": self.publish_task,
            "publish.shared.items": self.publish_shared_items,
            "publish.history": self.publish_history,
            "publish.workspace": self.publish_workspace,
            "publish.diff": self.publish_diff,
            "publish.preview_diff": self.publish_preview_diff,
            "publish.platform_read": self.publish_platform_read,
            "publish.prepare": lambda project, **args: self.publish_command("prepare", project, args),
            "publish.plan.update": lambda project, **args: self.publish_command("plan", project, args),
            "publish.adopt": lambda project, **args: self.publish_command("adopt", project, args),
            "publish.reconcile": lambda project, **args: self.publish_command("reconcile", project, args),
            "publish.verify": lambda project, **args: self.publish_command("verify", project, args),
            "publish.execute": lambda project, **args: self.publish_command("execute", project, args),
            "publish.pause": lambda project, **args: self.publish_command("pause", project, args),
            "publish.resume": lambda project, **args: self.publish_command("resume", project, args),
            "publish.run": lambda project, **args: self.publish_command("run", project, args),
            "publish.migrate": lambda project, **args: self.publish_command("migrate", project, args),
            "publish.decide": lambda project, **args: self.publish_command("decide", project, args),
            "publish.share": lambda project, **args: self.publish_command("share", project, args),
            "publish.share.publish": lambda project, **args: self.publish_command("share_publish", project, args),
            "publish.collaborate": lambda project, **args: self.publish_command("collaborate", project, args),
            "publish.collaborate.send": lambda project, **args: self.publish_command("collaborate_send", project, args),
            "publish.account.save": self.publish_account_save,
            "publish.link_task": lambda project, **args: self.publish_command("link_task", project, args),
            "publish.package": lambda project, **args: self.publish_command("package", project, args),
            "publish.export": lambda project, **args: self.publish_command("export", project, args),
            "publish.tag": lambda project, **args: self.publish_command("tag", project, args),
            "publish.release": lambda project, **args: self.publish_command("release", project, args),
            "publish.import_package": lambda project, **args: self.publish_command("import_package", project, args),
            "publish.distribution_reconcile": lambda project, **args: self.publish_command("distribution_reconcile", project, args),
            "publish.download": lambda project, **args: self.publish_command("download", project, args),
            "publish.branch": lambda project, **args: self.publish_command("branch", project, args),
            "publish.branch.apply": lambda project, **args: self.publish_command("branch_apply", project, args),
            "publish.operation": self.publish_operation,
            "lesson.list": self.lesson_list,
            "lesson.search": self.lesson_search,
            "lesson.verify": self.lesson_verify,
            "lesson.share": self.lesson_share,
            "lesson.share.revoke": self.lesson_share_revoke,
            "lesson.harvest.preview": self.lesson_harvest_preview,
            "lesson.harvest.resolve": self.lesson_harvest_resolve,
            "lesson.harvest.list": self.lesson_harvest_list,
            "lesson.harvest.context": self.lesson_harvest_context,
            "contract.show": self.contract_show,
            "governance.quality": self.governance_quality,
            "governance.patterns": self.governance_patterns,
            "governance.feed": self.governance_feed,
            "governance.releases": self.governance_releases,
            "history.list": self.history_list,
            "memory.snapshot": self.memory_snapshot,
            "memory.list": self.memory_list,
            "memory.restore": self.memory_restore,
            "runtime.tools.list": self.runtime_tools_list,
            "runtime.tools.archive": self.runtime_tools_archive,
            "runtime.tools.restore": self.runtime_tools_restore,
            "trial.list": self.trial_list,
            "trial.triage": self.trial_triage,
            "formal.list": self.formal_list,
            "formal.edit_message": self.formal_edit_message,
            "formal.delete": self.formal_delete,
            "formal.dissolve": self.formal_dissolve,
            "template.list": self.template_list,
            "template.add": self.template_add,
            "template.edit": self.template_edit,
            "template.delete": self.template_delete,
            "provider.status": self.provider_status,
            "provider.save": self.provider_save,
            "provider.switch": self.provider_switch,
            "provider.delete": self.provider_delete,
            "provider.test": self.provider_test,
            "provider.presets": self.provider_presets,
            "provider.models": self.provider_models,
            "provider.model.options": self.provider_model_options,
            "provider.model.select": self.provider_model_select,
            "provider.effort.options": self.provider_effort_options,
            "provider.effort.select": self.provider_effort_select,
            "provider.probe": self.provider_probe,
            "provider.connection": self.provider_connection,
            "provider.cancel": self.provider_cancel,
            "provider.failover.configure": self.provider_failover_configure,
            "project.export": self.project_export,
        }

    @property
    def operations(self) -> tuple[str, ...]:
        return tuple(sorted(self._operations))

    def invoke(self, operation: str, arguments: dict | None = None) -> Any:
        handler = self._operations.get(operation)
        if handler is None:
            raise OperationError("UNKNOWN_OPERATION", f"Unknown operation: {operation}")
        arguments = arguments or {}
        try:
            inspect.signature(handler).bind(**arguments)
        except TypeError as exc:
            raise OperationError("INVALID_ARGUMENTS", str(exc)) from exc
        if operation.startswith("deletion."):
            from .deletion import blocked
            try:
                with self._write_lock:
                    return handler(**arguments)
            except (ValueError, OSError) as exc:
                raise blocked(str(exc)) from exc
        return handler(**arguments)

    def _process_presentation(self, project: str):
        from backend.core.storage import get_storage
        from backend.core.application.process_presentation import ProcessPresentation
        _cfg, proj = _project(project)
        return ProcessPresentation(get_storage(str(proj.workspace_path)))

    def process_rename(self, project: str, process_id: str, display_name: str) -> dict:
        try:
            return self._process_presentation(project).rename(process_id, display_name)
        except ValueError as exc:
            from backend.core.errors import error_payload
            info = error_payload("PROCESS_PRESENTATION_INVALID", message=str(exc),
                                 next_actions=[{"action": "refresh_process_list"}])["error_info"]
            raise OperationError("PROCESS_PRESENTATION_INVALID", str(exc), details=info) from exc

    def process_archive(self, project: str, process_id: str, archived: bool = True) -> dict:
        if not isinstance(archived, bool):
            raise OperationError("INVALID_ARGUMENTS", "archived must be a boolean")
        try:
            return self._process_presentation(project).archive(process_id, archived)
        except ValueError as exc:
            from backend.core.errors import error_payload
            info = error_payload("PROCESS_PRESENTATION_INVALID", message=str(exc),
                                 next_actions=[{"action": "refresh_process_list"}])["error_info"]
            raise OperationError("PROCESS_PRESENTATION_INVALID", str(exc), details=info) from exc

    def _project_storage(self, project: str):
        from backend.core.storage import get_storage
        _cfg, proj = _project(project)
        return get_storage(str(proj.workspace_path))

    def runtime_tools_list(self, project: str, include_archived: bool = True) -> dict:
        if not isinstance(include_archived, bool):
            raise OperationError("INVALID_ARGUMENTS", "include_archived must be a boolean")
        try:
            return self._project_storage(project).list_custom_tools(
                include_archived=include_archived,
            )
        except (ValueError, KeyError, OSError) as exc:
            from backend.core.errors import error_payload
            info = error_payload(
                "CUSTOM_TOOL_CATALOG_FAILED", message=str(exc),
                next_actions=[{"action": "inspect_storage_health"},
                              {"action": "retry"}],
            )["error_info"]
            raise OperationError("CUSTOM_TOOL_CATALOG_FAILED", str(exc), details=info) from exc

    @staticmethod
    def _custom_tool_state_error(exc: BaseException, action: str) -> OperationError:
        from backend.core.errors import error_payload
        raw = str(exc).strip("'\"")
        name = "CUSTOM_TOOL_NOT_FOUND" if raw.startswith("CUSTOM_TOOL_NOT_FOUND") \
            else "CUSTOM_TOOL_STATE_CHANGE_FAILED"
        info = error_payload(
            name, message=raw,
            next_actions=[{"action": "refresh_runtime_tools"},
                          {"action": action}],
        )["error_info"]
        return OperationError(name, raw, details=info)

    def runtime_tools_archive(self, project: str, name: str) -> dict:
        if not str(name).strip():
            raise OperationError("INVALID_ARGUMENTS", "custom tool name is required")
        try:
            return self._project_storage(project).set_custom_tool_archived(
                str(name), archived=True,
            )
        except (ValueError, KeyError, OSError) as exc:
            raise self._custom_tool_state_error(exc, "archive") from exc

    def runtime_tools_restore(self, project: str, name: str) -> dict:
        if not str(name).strip():
            raise OperationError("INVALID_ARGUMENTS", "custom tool name is required")
        try:
            return self._project_storage(project).set_custom_tool_archived(
                str(name), archived=False,
            )
        except (ValueError, KeyError, OSError) as exc:
            raise self._custom_tool_state_error(exc, "restore") from exc

    # Project/config -------------------------------------------------

    def project_list(self) -> list[dict]:
        with self._write_lock:
            cfg = ConfigManager.load()
        from backend.core.storage import resolve_existing_storage_paths
        projects: list[dict] = []
        for p in cfg.projects:
            if p.archived:
                continue
            try:
                paths = resolve_existing_storage_paths(p.workspace_path)
                project_id = str(paths.project_id if paths is not None else "")
            except (OSError, ValueError):
                project_id = ""
            projects.append({
                "name": p.name,
                "workspace": p.workspace_path,
                "project_id": project_id,
                "backup": p.backup_path,
                "commit_prefix": p.commit_format.get("prefix", ""),
                "daemonOnline": self._daemon_online(p.name),
            })
        return projects

    def project_create(self, name: str, workspace_path: str,
                       release_url: str = "", llm_provider: str = "",
                       workspace_mode: str = "attach_existing") -> dict:
        del llm_provider
        from backend.core.config import ProjectConfig
        from backend.models import FileAccess, RepoNode, RemoteTarget
        clean_name = str(name or "").strip()
        raw_workspace = str(workspace_path or "").strip()
        mode = str(workspace_mode or "attach_existing").strip().lower()
        if not clean_name:
            raise OperationError("PROJECT_NAME_REQUIRED", "Project name is required")
        if not raw_workspace:
            raise OperationError("PROJECT_WORKSPACE_REQUIRED", "Workspace path is required")
        if mode not in {"attach_existing", "create_new"}:
            raise OperationError(
                "INVALID_WORKSPACE_MODE",
                "workspace_mode must be attach_existing or create_new",
            )
        workspace = Path(raw_workspace).expanduser().resolve(strict=False)
        _external_workspace(workspace)
        if workspace == Path(workspace.anchor):
            raise OperationError(
                "UNSAFE_WORKSPACE_PATH", "A filesystem root cannot be used as a project workspace",
            )
        with self._write_lock:
            cfg = ConfigManager.load()
            for existing in cfg.projects:
                if existing.name.lower() == clean_name.lower():
                    hint = (" It is archived; restore it from Bin or choose a different name."
                            if existing.archived else " Choose a different project name.")
                    raise OperationError(
                        "DUPLICATE_NAME", f"Project '{existing.name}' already exists.{hint}",
                        details={"existing_project": existing.name,
                                 "archived": existing.archived,
                                 "workspace": existing.workspace_path},
                    )
            for existing in cfg.projects:
                if existing.workspace_path and Path(existing.workspace_path).resolve(strict=False) == workspace:
                    hint = (" It is archived; archiving keeps the workspace registered. "
                            "Restore it from Bin or choose a different folder."
                            if existing.archived else " Open that project or choose a different folder.")
                    raise OperationError(
                        "WORKSPACE_ALREADY_REGISTERED",
                        f"Workspace is already registered as project '{existing.name}'.{hint}",
                        details={"existing_project": existing.name,
                                 "archived": existing.archived,
                                 "workspace": str(workspace)},
                    )

            created = False
            if mode == "attach_existing":
                if not workspace.exists() or not workspace.is_dir():
                    raise OperationError(
                        "WORKSPACE_NOT_FOUND",
                        "Attach Existing requires an existing directory",
                    )
            else:
                if workspace.exists():
                    if not workspace.is_dir():
                        raise OperationError(
                            "WORKSPACE_NOT_DIRECTORY", "Workspace path is not a directory",
                        )
                    try:
                        next(workspace.iterdir())
                    except StopIteration:
                        pass
                    else:
                        raise OperationError(
                            "WORKSPACE_NOT_EMPTY",
                            "Create New requires a missing or empty directory",
                        )
                else:
                    try:
                        workspace.mkdir(parents=True, exist_ok=False)
                        created = True
                    except OSError as exc:
                        raise OperationError(
                            "WORKSPACE_CREATE_FAILED", f"Could not create workspace: {exc}",
                        ) from exc
            if not os.access(workspace, os.R_OK | os.W_OK):
                if created:
                    try:
                        workspace.rmdir()
                    except OSError:
                        pass
                raise OperationError(
                    "WORKSPACE_ACCESS_DENIED", "Workspace must be readable and writable",
                )
            project = ProjectConfig(
                name=clean_name,
                workspace=RepoNode(file_access=FileAccess(path=str(workspace))),
            )
            if release_url:
                project.release = RepoNode(
                    remote=RemoteTarget(url=release_url),
                )
            cfg.projects.append(project)
            try:
                ConfigManager.save(cfg)
            except BaseException:
                if created:
                    try:
                        workspace.rmdir()
                    except OSError:
                        pass
                raise
        return {"ok": True, "name": clean_name, "workspace": str(workspace),
                "workspace_mode": mode, "release_url": release_url}

    def project_list_archived(self) -> list[dict]:
        with self._write_lock:
            cfg = ConfigManager.load()
        return [{"name": p.name, "workspace": p.workspace_path,
                 "release_url": (p.release.remote.url if p.release.remote else "") or p.backup_path,
                 "archived": True,
                 "pending_hard_delete_at": p.pending_hard_delete_at or ""}
                for p in cfg.projects if p.archived]

    def project_enter(self, name: str) -> dict:
        """Validate explicit user entry before mounting any project UI/runtime."""
        from datetime import datetime, timezone, timedelta
        from .project_lifecycle import workspace_missing, archive_delay
        with self._write_lock:
            cfg = ConfigManager.load()
            project = next((p for p in cfg.projects if p.name == name), None)
            if project is None:
                raise OperationError("PROJECT_NOT_FOUND", f"Project '{name}' was not found")
            if project.archived:
                raise OperationError("PROJECT_ARCHIVED", f"Project '{name}' is archived; restore it first")
            try:
                missing = workspace_missing(project.workspace_path)
            except OSError as exc:
                raise OperationError("WORKSPACE_UNAVAILABLE", f"Cannot access workspace: {project.workspace_path}: {exc}") from exc
            if missing:
                entry = project.unavailable_entry
                if entry.get("workspace") != project.workspace_path or not entry.get("first_failed_at"):
                    entry = {"workspace": project.workspace_path,
                             "first_failed_at": datetime.now(timezone.utc).isoformat()}
                    project.unavailable_entry = entry
                    ConfigManager.save(cfg)
                delay = archive_delay(cfg)
                due = (datetime.fromisoformat(entry["first_failed_at"]) + timedelta(minutes=delay)).isoformat() if delay else None
                raise OperationError("WORKSPACE_NOT_FOUND",
                    f"Workspace does not exist: {project.workspace_path}" +
                    (f". Automatically archives after {delay} minutes if still missing." if delay else ""),
                    details={"workspace": project.workspace_path, "archive_at": due})
            if project.unavailable_entry:
                project.unavailable_entry = {}
                ConfigManager.save(cfg)
            return {"ok": True, "name": project.name, "workspace": project.workspace_path}

    def maintain_project_lifecycle(self) -> list[dict]:
        from .project_lifecycle import maintain_unavailable_projects
        with self._write_lock:
            cfg = ConfigManager.load()
            changes = maintain_unavailable_projects(cfg)
            if changes:
                ConfigManager.save(cfg)
            return changes

    def project_archive(self, name: str = "") -> dict:
        with self._write_lock:
            cfg = ConfigManager.load()
            if not name:
                archived = [p.name for p in cfg.projects if p.archived]
                return {"archived_projects": archived, "count": len(archived)}
            for project in cfg.projects:
                if project.name.lower() == name.lower():
                    from .project_lifecycle import set_archived
                    set_archived(project, not project.archived)
                    ConfigManager.save(cfg)
                    return {"ok": True, "name": project.name,
                            "state": "archived" if project.archived else "restored"}
        raise OperationError("PROJECT_NOT_FOUND", f"Project '{name}' was not found")

    def project_delete(self, name: str, mode: str = "soft") -> dict:
        from backend.core.errors import error_payload
        info = error_payload("DELETION_CONFIRMATION_REQUIRED", next_actions=[
            {"operation": "deletion.preview", "arguments": {"project": name, "mode": mode}},
        ])["error_info"]
        raise OperationError("DELETION_CONFIRMATION_REQUIRED", info["message"], details=info)

    def project_cancel_delete(self, name: str) -> dict:
        with self._write_lock:
            cfg = ConfigManager.load()
            for project in cfg.projects:
                if project.name.lower() == name.lower():
                    had_pending = bool(project.pending_hard_delete_at)
                    project.pending_hard_delete_at = ""
                    if had_pending:
                        ConfigManager.save(cfg)
                    return {"ok": had_pending, "name": name}
        raise OperationError("PROJECT_NOT_FOUND", f"Project '{name}' was not found")

    def product_version(self) -> dict:
        # The executing Host is authoritative, not editable user configuration
        # or a potentially different Dashboard package.
        from backend.version import __version__
        return {"product_id": "gitgo-terminal", "version": __version__}

    def _update_call(self, method, *arguments):
        from backend.core.update.validation import UpdateError
        try: return method(*arguments)
        except UpdateError as error: raise OperationError(error.code, str(error)) from None
        except Exception: raise OperationError("UPDATE_PREPARATION_FAILED", "Update preparation failed; current program unchanged") from None

    def update_configure(self, channel: str, automatic_check: bool, automatic_download: bool,
                         automatic_install: bool, revision: int) -> dict:
        return self._update_call(self.updates.configure, channel, automatic_check, automatic_download, automatic_install, revision)

    def update_status(self, channel: str | None = None) -> dict:
        return self._update_call(self.updates.status, channel)

    def update_check(self, channel: str, operation_id: str) -> dict:
        return self._update_call(self.updates.check, channel, operation_id)

    def update_download(self, channel: str, candidate_id: str, operation_id: str) -> dict:
        return self._update_call(self.updates.download, channel, candidate_id, operation_id)

    def update_cancel(self, operation_id: str) -> dict:
        return self._update_call(self.updates.cancel, operation_id)

    def update_request_apply(self, channel: str, candidate_id: str, operation_id: str) -> dict:
        return self._update_call(self.updates.request_apply, channel, candidate_id, operation_id)

    def update_request_rollback(self, rollback_id: str, operation_id: str) -> dict:
        return self._update_call(self.updates.request_rollback, rollback_id, operation_id)

    def update_cancel_pending(self, request_id: str, operation_id: str) -> dict:
        return self._update_call(self.updates.cancel_pending, request_id, operation_id)

    def config_get(self, key: str = "") -> dict:
        from backend.core.prompt_profiles import list_profiles
        cfg = ConfigManager.load()
        if not key:
            return {"safety": cfg.safety, "language": cfg.language,
                    "verbose": cfg.verbose, "diff_layout": cfg.diff_layout, "auto_compact": cfg.auto_compact,
                    "keyboard_preset": cfg.keyboard_preset, "keyboard_bindings": cfg.keyboard_bindings,
                    "agent_routing": cfg.agent_routing,
                    "prompt_profile": cfg.prompt_profile,
                    "prompt_profiles": list_profiles(),
                    "theme": cfg.theme, "animation": cfg.animation,
                    "external_editor": cfg.external_editor,
                    "launcher": dict(cfg.launcher or {}),
                    "schema_version": cfg.schema_version,
                    "revision": cfg.revision,
                    "web_search_mode": cfg.web_search_mode,
                    "web_search_endpoint": cfg.web_search_endpoint,
                    "web_search_engine": cfg.web_search_engine}
        value: Any = cfg
        for part in key.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            elif hasattr(value, part):
                value = getattr(value, part)
            else:
                raise OperationError("INVALID_KEY", f"Invalid config key: {key}")
        return {"key": key, "value": value}

    def config_terminals(self, startup=False, attached=False) -> dict:
        if type(startup) is not bool or type(attached) is not bool:
            raise OperationError("INVALID_VALUE", "Terminal startup flags must be booleans")
        from backend.core.terminal_launcher import terminal_inventory, startup_terminal_inventory
        from backend.core.terminal_metrics import console_display_metrics
        launcher = ConfigManager.load().launcher
        inventory = startup_terminal_inventory(launcher, attached=attached) if startup else terminal_inventory(launcher)
        return {**inventory, "display_metrics": console_display_metrics()}

    def config_set(self, key: str, value: Any) -> dict:
        with self._write_lock:
            cfg = ConfigManager.load()
            parts = key.split(".")
            if len(parts) == 2 and parts[0] == "safety":
                if parts[1] in {"delete_delay_minutes", "process_delete_delay_minutes", "unavailable_project_archive_delay_minutes"}:
                    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 43200:
                        raise OperationError("INVALID_VALUE", "Delete delay must be an integer between 0 and 43200 minutes")
                cfg.safety[parts[1]] = value
            elif key == "language":
                language = str(value).lower()
                if language not in ("en", "zh"):
                    raise OperationError("INVALID_VALUE", "language must be 'en' or 'zh'")
                cfg.language = language
                value = language
            elif key == "verbose":
                cfg.verbose = bool(value)
            elif key == "diff_layout":
                if value not in ("split", "unified"):
                    raise OperationError("INVALID_VALUE", "diff_layout must be 'split' or 'unified'")
                cfg.diff_layout = value
            elif key == "auto_compact":
                cfg.auto_compact = bool(value)
            elif key == "keyboard_preset":
                if not isinstance(value, str) or value not in ("standard", "portable"):
                    raise OperationError("INVALID_VALUE", "keyboard_preset must be 'standard' or 'portable'")
                cfg.keyboard_preset = value
                cfg.keyboard_bindings = {"version": 1, "overrides": ({
                    "editor.newline": {"key": "return", "meta": True},
                    "navigation.previousTab": {"key": "p", "ctrl": True},
                } if value == "portable" else {})}
            elif key == "keyboard_bindings":
                from backend.core.keyboard_bindings import validate_keyboard_bindings
                try:
                    value = validate_keyboard_bindings(value)
                except ValueError as exc:
                    raise OperationError("INVALID_VALUE", str(exc)) from exc
                cfg.keyboard_bindings = value
            elif key == "agent_routing":
                if value not in ("owner", "fresh"):
                    raise OperationError("INVALID_VALUE", "agent_routing must be 'owner' or 'fresh'")
                cfg.agent_routing = value
            elif key == "prompt_profile":
                from backend.core.prompt_profiles import snapshot
                from backend.core.prompt_templates import PromptTemplateError
                try:
                    snapshot(value)
                except PromptTemplateError as exc:
                    raise OperationError("INVALID_PROMPT_PROFILE", str(exc)) from exc
                cfg.prompt_profile = value
            elif key == "theme":
                cfg.theme = str(value)
            elif key == "animation":
                cfg.animation = bool(value)
            elif key == "external_editor":
                editor = str(value or "").strip().strip('"')
                if editor:
                    candidate = Path(editor).expanduser().resolve(strict=False)
                    if not candidate.is_file() or candidate.suffix.lower() != ".exe":
                        raise OperationError(
                            "INVALID_EXTERNAL_EDITOR",
                            "External editor must be the path of an existing .exe file",
                        )
                    editor = str(candidate)
                cfg.external_editor = editor
                value = editor
            elif key.startswith("launcher."):
                field_name = key.split(".", 1)[1]
                if field_name == "terminal":
                    from backend.core.terminal_launcher import TERMINAL_IDS, terminal_inventory
                    terminal = str(value or "auto").strip().lower()
                    if terminal not in TERMINAL_IDS:
                        raise OperationError(
                            "INVALID_TERMINAL_MODE",
                            "Choose a supported terminal from config.terminals",
                        )
                    inventory = terminal_inventory({**cfg.launcher, "terminal": terminal})
                    choice = next(c for c in inventory["options"] if c["id"] == terminal)
                    if not choice["available"]:
                        raise OperationError("TERMINAL_UNAVAILABLE",
                                             "The selected terminal is missing or could not be verified; refresh the terminal choices.",
                                             details={"warnings": inventory["warnings"]})
                    cfg.launcher["terminal"] = terminal
                    cfg.launcher["configured"] = True
                    if terminal != "custom":
                        cfg.launcher["command"] = choice["command"]
                        cfg.launcher["args"] = list(choice["args"])
                        cfg.launcher["identity"] = choice.get("identity")
                    value = terminal
                elif field_name == "command":
                    cfg.launcher["command"] = str(value or "").strip().strip('"')
                    value = cfg.launcher["command"]
                elif field_name == "args":
                    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                        raise OperationError("INVALID_TERMINAL_ARGS", "Terminal args must be a string array")
                    cfg.launcher["args"] = list(value)
                    value = list(value)
                else:
                    raise OperationError("UNKNOWN_KEY", f"Unsupported config key: {key}")
            elif key == "web_search_endpoint":
                from urllib.parse import urlparse
                endpoint = str(value or "").strip()
                if endpoint:
                    parsed = urlparse(endpoint)
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                        raise OperationError(
                            "INVALID_WEB_SEARCH_ENDPOINT",
                            "Web search endpoint must be an absolute http:// or https:// URL",
                        )
                cfg.web_search_endpoint = endpoint
                value = endpoint
            elif key == "web_search_mode":
                mode = str(value or "").strip().lower()
                if mode not in {"auto", "provider", "searxng", "disabled"}:
                    raise OperationError(
                        "INVALID_WEB_SEARCH_MODE",
                        "Web search mode must be auto, provider, searxng, or disabled",
                    )
                cfg.web_search_mode = mode
                value = mode
            elif key == "web_search_engine":
                engine = str(value or "").strip().lower()
                if engine not in {"google", "bing", "baidu", "yandex", "duckduckgo"}:
                    raise OperationError(
                        "INVALID_WEB_SEARCH_ENGINE",
                        "Web search engine must be google, bing, baidu, yandex, or duckduckgo",
                    )
                cfg.web_search_engine = engine
                value = engine
            else:
                raise OperationError("UNKNOWN_KEY", f"Unsupported config key: {key}")
            ConfigManager.save(cfg)
        return {"ok": True, "key": key, "value": value}

    def config_web_search_test(self) -> dict:
        """Probe the saved search adapter through the production code path.

        This is deliberately a separate read operation rather than a side
        effect hidden inside config.set: saving a valid offline endpoint must
        remain possible, while the Dashboard can report reachability honestly.
        """
        cfg = ConfigManager.load()
        endpoint = str(cfg.web_search_endpoint or "").strip()
        if not endpoint:
            return {
                "ok": False,
                "reachable": False,
                "error": "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
            }
        from backend.core.tools.web_tools import web_search
        result = web_search({
            "query": "Gitgo connectivity probe",
            "max_results": 1,
            "_web_search_endpoint": endpoint,
            "_web_search_engine": cfg.web_search_engine,
        })
        return {
            "ok": not bool(result.get("error")),
            "reachable": bool(result.get("provider_reachable")),
            "count": int(result.get("count") or 0),
            "empty_reason": str(result.get("empty_reason") or ""),
            "error": str(result.get("error") or ""),
            "error_info": result.get("error_info"),
            "engine": cfg.web_search_engine,
        }

    @staticmethod
    def _publish_project(project):
        # An explicit config path suppresses implicit legacy import/movement.
        path = ConfigManager.find_config()
        cfg = ConfigManager.load(path, strict=True) if path is not None else None
        proj = next((item for item in cfg.projects if item.name == project), None) if cfg else None
        if proj is None:
            raise OperationError("PROJECT_NOT_FOUND", "Project was not found")
        if not proj.workspace_path:
            raise OperationError("PUBLISH_WORKSPACE_UNCONFIGURED", "Publish requires an explicit workspace")
        _external_workspace(proj.workspace_path)
        return proj

    def publish_items(self, project: str, limit: int = 50, cursor: str = "") -> dict:
        from backend.core.storage import StorageDatabaseError
        from .publish import list_items, PublishQueryError
        proj = self._publish_project(project)
        try:
            return list_items(proj, limit=limit, cursor=cursor)
        except PublishQueryError as exc:
            raise OperationError(exc.code, "Refresh the Publish list or check query arguments") from exc
        except (OSError, ValueError, RuntimeError, StorageDatabaseError) as exc:
            raise OperationError("PUBLISH_STATE_UNAVAILABLE", "Saved Publish records could not be verified") from exc

    def publish_item(self, project: str, item_id: str) -> dict:
        from backend.core.storage import StorageDatabaseError
        from backend.core.publish.state import read_ledger, find_item, projection, PublishError
        proj = self._publish_project(project)
        try:
            ledger, reference = read_ledger(proj.workspace_path)
            return {"project": project, "item": projection(find_item(ledger, item_id), reference)}
        except PublishError as exc:
            raise OperationError(exc.code, "Publish item could not be read") from exc
        except (OSError, ValueError, RuntimeError, StorageDatabaseError) as exc:
            raise OperationError("PUBLISH_STATE_UNAVAILABLE", "Saved Publish records could not be verified") from exc

    def publish_task(self, project: str, process_id: str, task_id: str) -> dict:
        from backend.core.storage.runtime import read_existing_task_view
        from backend.core.storage import StorageDatabaseError, StorageBlocked
        if any(not isinstance(value, str) or not 1 <= len(value) <= 160 or any(ord(char) < 32 for char in value)
               for value in (process_id, task_id)):
            raise OperationError("PUBLISH_TASK_NOT_FOUND", "Select an existing task reference")
        proj = self._publish_project(project)
        try:
            result = read_existing_task_view(proj.workspace_path, process_id, task_id)
        except (OSError, ValueError, RuntimeError, StorageDatabaseError, StorageBlocked) as exc:
            raise OperationError("PUBLISH_STATE_UNAVAILABLE", "Saved task could not be verified") from exc
        if result is None:
            raise OperationError("PUBLISH_TASK_NOT_FOUND", "The exact task is no longer available")
        return {"project": project, **result}

    def publish_command(self, operation, project, arguments):
        from backend.core.storage import StorageDatabaseError
        from backend.core.tools.publish_service import invoke_local_publish
        from backend.core.publish.state import PublishError
        proj = self._publish_project(project)
        try:
            return invoke_local_publish(proj, operation, arguments, queue=self.publish_runtime, operator_authorized=True)
        except PublishError as exc:
            raise OperationError(exc.code, "Publish command could not proceed") from exc
        except (OSError, ValueError, RuntimeError, StorageDatabaseError) as exc:
            code = "PUBLISH_REQUEST_CONFLICT" if str(exc) == "COMMAND_IDENTITY_CONFLICT" else "PUBLISH_OPERATION_UNCERTAIN"
            raise OperationError(code, "Read the saved item before retrying this request") from exc

    def publish_shared_items(self, project, item_id, observation_id="", limit=20, cursor=""):
        from backend.core.publish.shared_query import shared_items
        from backend.core.publish.state import PublishError
        proj = self._publish_project(project)
        try:
            return shared_items(proj, item_id=item_id, observation_id=observation_id, limit=limit, cursor=cursor)
        except PublishError as exc:
            raise OperationError(exc.code, "Read an explicit saved shared observation") from exc

    def publish_history(self, project, item_id, head="candidate", limit=50, cursor=""):
        from backend.core.publish.history import history_page
        from backend.core.publish.state import PublishError
        try:
            return history_page(self._publish_project(project), item_id=item_id, head=head, limit=limit, cursor=cursor)
        except PublishError as exc:
            raise OperationError(exc.code, "Fixed Git history could not be read") from exc

    def publish_workspace(self, project, limit=50, cursor=""):
        from backend.core.publish.history import workspace_page
        from backend.core.publish.state import PublishError
        try:
            return workspace_page(self._publish_project(project), limit=limit, cursor=cursor)
        except PublishError as exc:
            raise OperationError(exc.code, "Workspace Git history could not be read") from exc

    def publish_diff(self, project, item_id, base_oid, head_oid="", head="candidate", limit_bytes=262144, snapshot_id=""):
        from backend.core.publish.history import diff_view
        from backend.core.publish.state import PublishError
        try:
            return diff_view(self._publish_project(project), item_id=item_id, base_oid=base_oid, head_oid=head_oid,
                head=head, limit_bytes=limit_bytes, snapshot_id=snapshot_id)
        except PublishError as exc:
            raise OperationError(exc.code, "Fixed Git difference could not be read") from exc

    def publish_preview_diff(self, project, item_id, binding=""):
        from backend.core.publish.history import preview_diff
        from backend.core.publish.state import PublishError
        try:
            return preview_diff(self._publish_project(project), item_id=item_id, binding=binding)
        except PublishError as exc:
            raise OperationError(exc.code, "Prepared result difference could not be read") from exc

    def publish_platform_read(self, project, target, resource, page=1):
        """Repository browsing needs no local candidate, ledger or Git mutation."""
        self._publish_project(project)
        from backend.remote.publish_platform import PlatformTarget, PlatformClient, PlatformError
        from backend.remote.publish_accounts import resolve_secret
        from backend.core.publish.state import digest
        from backend.core.storage.redaction import redact_for_persistence
        if type(page) is not int or not 1 <= page <= 20:
            raise OperationError("PUBLISH_PLATFORM_PAGE_INVALID", "Page must be between 1 and 20")
        try:
            parsed = PlatformTarget.parse(target)
            with PlatformClient(parsed, resolve_secret(parsed)) as client:
                snapshot = redact_for_persistence(client.snapshot(resource, page))
            return {**snapshot, "schema_version": 1, "read_only": True, "target": parsed.descriptor(),
                    "id": digest({"target": parsed.descriptor(), "snapshot": snapshot}), "binding": "read_only"}
        except PlatformError as exc:
            raise OperationError(exc.code, "Repository platform observation could not be read") from exc

    def publish_operation(self, project, item_id, operation_id):
        from backend.core.tools.publish_service import build_publish_tools
        from backend.core.publish.state import PublishError
        proj = self._publish_project(project)
        tool = build_publish_tools(proj)["publish_operation"]
        try:
            return tool.execute(tool.prepare_args({"item_id": item_id, "operation_id": operation_id}))
        except PublishError as exc:
            raise OperationError(exc.code, "Publish operation could not be read") from exc

    def publish_get(self, project: str) -> dict:
        _cfg, proj = _project(project)
        remote = proj.release.remote.url if proj.release.remote else ""
        return {
            "project": proj.name,
            "commit_format": dict(proj.commit_format or {}),
            "checks": proj.publish_checks,
            "privacy": dict(proj.outbound_policy or {}),
            "authorship": dict(proj.authorship or {}),
            "repositories": {"workspace": proj.workspace_path, "trial": proj.trial_path or "", "formal": proj.backup_path or ""},
            "remote_url": str(remote or ""),
            "stages": {"trial_configured": bool(proj.trial_path),
                       "release_configured": bool(remote or proj.backup_path)},
        }

    def publish_account_save(self, project: str, target: dict, token: str, request_id: str) -> dict:
        # Native credential settings intentionally bypass command argument
        # receipts, exactly as provider credentials do. No Agent tool exposes it.
        self._publish_project(project)
        from backend.remote.publish_accounts import provision_secret
        from backend.remote.publish_platform import PlatformError
        try:
            result = provision_secret(target, token)
        except PlatformError as exc:
            raise OperationError(exc.code, "Publish account could not be saved") from exc
        return {**result, "request_id": request_id, "operation_id": "account-settings", "effect_state": "committed"}

    def publish_set(self, project: str, section: str, value: Any, expected_value: Any = None) -> dict:
        from backend.core.config import normalize_outbound_policy
        from backend.models import RemoteTarget
        clean_section = str(section or "").strip().lower()
        with self._write_lock:
            cfg, proj = _project(project)
            current = {"privacy": dict(proj.outbound_policy or {}), "commit_format": dict(proj.commit_format or {}),
                "privacy_authorship": {"privacy": dict(proj.outbound_policy or {}), "authorship": dict(proj.authorship or {})},
                "checks": proj.publish_checks, "remote_url": str(proj.release.remote.url or "") if proj.release.remote else ""}
            if expected_value is not None and expected_value != current.get(clean_section):
                raise OperationError("PUBLISH_SETTINGS_CHANGED", "Settings changed; reload before saving this draft")
            if clean_section == "privacy_authorship":
                if not isinstance(value, dict) or set(value) != {"privacy", "authorship"}:
                    raise OperationError("INVALID_OUTBOUND_POLICY", "Expected privacy and authorship")
                try:
                    policy = normalize_outbound_policy(value["privacy"])
                except (TypeError, ValueError) as exc:
                    raise OperationError("INVALID_OUTBOUND_POLICY", str(exc)) from exc
                authorship = value["authorship"]
                if (not isinstance(authorship, dict) or authorship.get("mode") not in {"mixed", "clean"}
                        or any(type(authorship.get(key)) is not bool for key in ("strip_commit_coauthors", "strip_code_comments"))):
                    raise OperationError("INVALID_AUTHORSHIP_POLICY", "Invalid authorship settings")
                policy["version"] = int((proj.outbound_policy or {}).get("version", 0) or 0) + 1
                proj.outbound_policy = policy
                proj.force_exclude = list(policy["exclude_paths"])
                proj.security_scan = {key: policy[key] for key in ("enabled", "severity_threshold", "ignored_rules", "extra_patterns")}
                proj.authorship = {**dict(proj.authorship or {}), **authorship,
                    "exclude_tool_configs": list(policy["exclude_paths"]),
                    "privacy": {"level": policy["content_level"], "deep_scan": policy["deep_scan"]}}
                result = {"privacy": policy, "authorship": proj.authorship}
            elif clean_section == "privacy":
                try:
                    policy = normalize_outbound_policy(value)
                except (TypeError, ValueError) as exc:
                    raise OperationError("INVALID_OUTBOUND_POLICY", str(exc)) from exc
                policy["version"] = int((proj.outbound_policy or {}).get("version", 0) or 0) + 1
                proj.outbound_policy = policy
                # Compatibility mirrors for old adapters. Runtime policy reads
                # outbound_policy; these fields no longer own the decision.
                proj.force_exclude = list(policy["exclude_paths"])
                proj.security_scan = {
                    "enabled": policy["enabled"],
                    "severity_threshold": policy["severity_threshold"],
                    "ignored_rules": list(policy["ignored_rules"]),
                    "extra_patterns": list(policy["extra_patterns"]),
                }
                authorship = dict(proj.authorship or {})
                authorship["exclude_tool_configs"] = list(policy["exclude_paths"])
                authorship["privacy"] = {
                    "level": policy["content_level"],
                    "deep_scan": policy["deep_scan"],
                }
                proj.authorship = authorship
                result = policy
            elif clean_section == "commit_format":
                if not isinstance(value, dict):
                    raise OperationError("INVALID_COMMIT_FORMAT", "commit_format must be an object")
                allowed = {"prefix", "number_start", "padding", "plugins", "template_name"}
                unknown = sorted(set(value) - allowed)
                if unknown:
                    raise OperationError("INVALID_COMMIT_FORMAT", f"Unsupported fields: {', '.join(unknown)}")
                result = {**dict(proj.commit_format or {}), **value}
                proj.commit_format = result
            elif clean_section == "checks":
                from backend.core.publish.checks import normalize
                from backend.core.publish.state import PublishError
                try:
                    result = normalize(value)
                except PublishError as exc:
                    raise OperationError(exc.code, "Invalid Publish check policy") from exc
                proj.publish_checks = result
            elif clean_section == "remote_url":
                remote_url = str(value or "").strip()
                if remote_url:
                    import re
                    from urllib.parse import urlparse
                    parsed = urlparse(remote_url)
                    supported_url = (
                        parsed.scheme in {"http", "https", "ssh", "git", "file"}
                        and bool(parsed.netloc or (parsed.scheme == "file" and parsed.path))
                    )
                    scp_style = bool(re.fullmatch(
                        r"[^@\s]+@[^:\s]+:[^\s]+", remote_url,
                    ))
                    local_path = Path(remote_url).expanduser().is_absolute()
                    if (remote_url.startswith("-") or any(ord(char) < 32 for char in remote_url)
                            or not (supported_url or scp_style or local_path)):
                        raise OperationError(
                            "INVALID_REMOTE_URL",
                            "Remote must be an absolute Git URL, scp-style SSH target, or absolute local path",
                        )
                if proj.release.remote is None:
                    proj.release.remote = RemoteTarget()
                proj.release.remote.url = remote_url
                result = remote_url
            else:
                raise OperationError("INVALID_PUBLISH_SECTION", "section must be privacy, commit_format, checks or remote_url")
            ConfigManager.save(cfg)
        return {"ok": True, "project": project, "section": clean_section, "value": result}

    # Knowledge/governance -----------------------------------------

    def lesson_list(self, project: str) -> dict:
        from backend.core.knowledge.lesson import LessonManager
        from backend.core.knowledge.applicability import assess_lesson
        cfg, proj = _project(project)
        from backend.core.sync_session import SyncSession
        workspace = Path(SyncSession(proj, cfg).workspace_path)
        def wire(items):
            from backend.core.knowledge.review import review_status, content_version
            return [{**item.to_dict(), "applicability": assess_lesson(item, workspace),
                     "review_status": review_status(item), "content_version": content_version(item)}
                    for item in items]
        return {"abstract": wire(LessonManager.load_abstract(workspace)),
                "instances": wire(LessonManager.load_instance(workspace, project)),
                "pending": wire(LessonManager.load_pending(workspace, project))}

    def lesson_search(self, project: str, query: str, tech_stack: str = "") -> list[dict]:
        from backend.core.knowledge.lesson import LessonManager
        from backend.core.knowledge.applicability import assess_lesson
        cfg, proj = _project(project)
        from backend.core.sync_session import SyncSession
        workspace = Path(SyncSession(proj, cfg).workspace_path)
        return [{**item.to_dict(), "applicability": assess_lesson(item, workspace)}
                for item in LessonManager.search(
                    workspace, query, project_name=project, tech_stack=tech_stack, include_unreviewed=True)]

    def lesson_share(self, project: str, lesson_id: str, trigger: str, rule: str, tech_stack: str) -> dict:
        from backend.core.knowledge.manager import LessonManager
        cfg, proj = _project(project)
        from backend.core.sync_session import SyncSession
        workspace = Path(SyncSession(proj, cfg).workspace_path)
        lesson = LessonManager.share_abstract(workspace, lesson_id, trigger=trigger,
                                              rule=rule, tech_stack=tech_stack)
        return {"shared": True, "lesson": lesson.to_dict()}

    def lesson_share_revoke(self, project: str, lesson_id: str) -> dict:
        from backend.core.knowledge.manager import LessonManager
        cfg, proj = _project(project)
        from backend.core.sync_session import SyncSession
        workspace = Path(SyncSession(proj, cfg).workspace_path)
        return {"revoked": LessonManager.revoke_shared(workspace, lesson_id)}

    def lesson_verify(self, project: str, lesson_id: str, expected_version: str = "") -> dict:
        from backend.core.knowledge.lesson import LessonManager
        cfg, proj = _project(project)
        from backend.core.sync_session import SyncSession
        workspace = Path(SyncSession(proj, cfg).workspace_path)
        if not expected_version:
            raise OperationError("LESSON_VERSION_REQUIRED", "Confirm the lesson version actually shown to the user")
        try:
            result = LessonManager.verify(workspace, lesson_id, project_name=project,
                user_confirmation=True, expected_version=expected_version)
        except ValueError as exc:
            raise OperationError(str(exc), "Review the current lesson before confirming") from exc
        if result is None:
            raise OperationError("LESSON_NOT_FOUND", f"Lesson '{lesson_id}' was not found")
        return {"verified": lesson_id, "verified_count": result.verified_count}

    def lesson_harvest_preview(self, project: str, observation: str) -> dict:
        """Semantically propose lessons, but never save one without confirmation."""
        observation = str(observation or "").strip()
        if len(observation) < 12:
            raise OperationError(
                "LESSON_OBSERVATION_TOO_SHORT",
                "Describe the situation, what went wrong or right, and the reusable conclusion.",
            )
        from backend.core.history import HistoryManager
        from backend.core.knowledge.harvest import (
            capture_signal, fail_harvest, harvest_llm_summary,
            lease_harvest_signal_ids, resolve_harvest_proposal,
            stage_harvest_proposal,
        )
        from backend.core.llm_config import LLMConfigManager
        from backend.core.loop.llm import LLMProvider
        from backend.core.sync_session import SyncSession
        cfg, proj = _project(project)
        workspace = Path(SyncSession(proj, cfg).workspace_path)
        configured = LLMConfigManager.get_active()
        if configured is None:
            raise OperationError(
                "PROVIDER_NOT_CONFIGURED",
                "Configure and test an active Provider before semantic lesson harvest.",
            )
        from backend.core.daemon.policy_helpers import _runtime_llm_from_config
        llm = _runtime_llm_from_config((configured.base_url, configured.api_key, configured.model_id,
                                      configured.protocol, configured.runtime_capabilities()))
        source_event_id = "manual:" + uuid.uuid4().hex
        with HistoryManager.workspace_scope(str(workspace)):
            signal_id = capture_signal(
                "manual_explicit",
                {
                    "trigger": "Explicit user reflection",
                    "detail": {"observation": observation},
                    "source": "explicit_user",
                },
                project,
                source_event_id=source_event_id,
            )
            batch = lease_harvest_signal_ids(project, [signal_id])
            if not batch:
                raise OperationError(
                    "LESSON_HARVEST_LEASE_FAILED",
                    "The explicit lesson signal could not be leased for analysis.",
                )
            try:
                summary_report = {}
                lessons = harvest_llm_summary(
                    batch, llm, str(workspace), project, raise_on_error=True,
                    summary_report=summary_report,
                )
            except Exception as exc:
                fail_harvest(project, [signal_id], str(exc), summary_report=summary_report)
                raise OperationError("LESSON_HARVEST_FAILED", str(exc)) from exc
            if not lessons:
                resolve_harvest_proposal(
                    project,
                    stage_harvest_proposal(project, [signal_id], [], summary_report=summary_report)["proposal_id"],
                    accepted=False,
                )
                raise OperationError(
                    "LESSON_NOT_ACTIONABLE",
                    "The observation did not contain a reusable, testable lesson.",
                    details={"classifications": summary_report.get("classifications", []),
                             "scope": "project_knowledge_only"},
                )
            proposal = stage_harvest_proposal(project, [signal_id], lessons, summary_report=summary_report)
        return {
            **proposal,
            "summary": summary_report,
            "question": "Save these reusable lessons as pending knowledge?",
            "options": [
                {
                    "label": "Save pending", "action": "accept", "recommended": True,
                    "principle": "Human confirmation keeps semantic harvest advisory.",
                    "immediate_effect": "Store the proposed lessons in Pending.",
                    "downstream_effect": "They become searchable and injectable, but still require verification.",
                },
                {
                    "label": "Discard", "action": "discard", "recommended": False,
                    "principle": "Do not retain a conclusion that is not reusable.",
                    "immediate_effect": "Close this proposal without saving lessons.",
                    "downstream_effect": "The observation remains only in the harvest audit trail.",
                },
            ],
        }

    def lesson_harvest_resolve(
        self, project: str, proposal_id: str, action: str,
        candidate_reviews: list[dict] | None = None,
        expected_version: str = "",
    ) -> dict:
        from backend.core.history import HistoryManager
        from backend.core.knowledge.harvest import (
            get_harvest_proposal, resolve_harvest_proposal,
        )
        from backend.core.knowledge.models import Lesson
        from backend.core.knowledge.lesson import LessonManager
        from backend.core.sync_session import SyncSession
        cfg, proj = _project(project)
        workspace = Path(SyncSession(proj, cfg).workspace_path)
        clean_action = str(action or "").strip().lower()
        if clean_action not in {"accept", "discard"}:
            raise OperationError("INVALID_HARVEST_ACTION", "action must be accept or discard")
        if clean_action == "discard" and candidate_reviews is not None:
            raise OperationError("INVALID_HARVEST_REVIEW", "Discard does not accept candidate revisions")
        with HistoryManager.workspace_scope(str(workspace)):
            proposal = get_harvest_proposal(project, proposal_id)
            if proposal is None:
                raise OperationError(
                    "LESSON_HARVEST_PROPOSAL_NOT_FOUND",
                    "The harvest proposal is missing or was already resolved.",
                )
            if not expected_version:
                raise OperationError("HARVEST_PROPOSAL_VERSION_REQUIRED", "Confirm the version actually shown to the user")
            if expected_version != proposal["proposal_version"]:
                raise OperationError("HARVEST_PROPOSAL_CHANGED", "Review the current proposal version before confirming")
            candidates = None
            if clean_action == "accept" and candidate_reviews is not None:
                candidates = _review_harvest_candidates(list(proposal["candidates"]), candidate_reviews)
            try:
                resolved = resolve_harvest_proposal(project, proposal_id,
                    accepted=clean_action == "accept", candidates=candidates,
                    expected_version=expected_version or proposal["proposal_version"])
            except ValueError as exc:
                raise OperationError(str(exc), "Harvest proposal was changed or already resolved") from exc
        return {**resolved, "count": len(resolved["lesson_ids"])}

    def lesson_harvest_list(self, project: str) -> dict:
        """Explicit management view; never a model's normal knowledge injection."""
        from backend.core.history import HistoryManager
        from backend.core.knowledge.harvest import harvest_status, get_harvest_proposal
        from backend.core.sync_session import SyncSession
        cfg, proj = _project(project)
        workspace = str(SyncSession(proj, cfg).workspace_path)
        with HistoryManager.workspace_scope(workspace):
            proposals = [get_harvest_proposal(project, identifier)
                         for identifier in dict.fromkeys(harvest_status(project)["awaiting_confirmation"])]
        return {"proposals": [proposal for proposal in proposals if proposal is not None]}

    def lesson_harvest_context(self, project: str, proposal_id: str, expected_version: str,
                               candidate_index: int = 0) -> dict:
        from backend.core.history import HistoryManager
        from backend.core.knowledge.harvest import get_harvest_proposal_context
        from backend.core.sync_session import SyncSession
        cfg, proj = _project(project)
        with HistoryManager.workspace_scope(str(SyncSession(proj, cfg).workspace_path)):
            try:
                return get_harvest_proposal_context(project, proposal_id, expected_version, candidate_index)
            except ValueError as exc:
                raise OperationError(str(exc), "Refresh the proposal before discussing it") from exc

    def contract_show(self, project: str) -> dict:
        from backend.core.contract import ContractManager, detect_drift
        from backend.core.sync_session import SyncSession
        cfg, proj = _project(project)
        session = SyncSession(proj, cfg)
        workspace = Path(session.workspace_path)
        from backend.core.contract import ContractStateUnavailable
        try:
            contract = ContractManager.load(workspace)
        except ContractStateUnavailable as exc:
            raise OperationError(exc.code, str(exc), details={"query_state": "failed"}) from exc
        if contract is None:
            return {"contract": None}
        result = {"project": contract.project, "updated": contract.updated,
                  "tech_stack": contract.tech_stack,
                  "decided_features": [{"name": f.name, "location": f.location,
                                         "signature": f.signature,
                                         "confirmed_count": f.confirmed_count}
                                        for f in contract.decided_features],
                  "architecture_constraints": contract.architecture_constraints}
        entry_files = [e.rel_path for e in (session.entries or []) if e.status != "same"]
        if entry_files:
            result["drift_alerts"] = detect_drift(workspace, entry_files, contract)
        return result

    def governance_quality(self, project: str) -> dict:
        from backend.core.history import HistoryManager
        _cfg, proj = _project(project)
        from backend.core.governance import compute_quality_metrics, load_suggestion_pairs
        with HistoryManager.workspace_scope(str(proj.workspace_path)):
            return compute_quality_metrics(load_suggestion_pairs(project))

    def governance_patterns(self, project: str) -> dict:
        from backend.core.history import HistoryManager
        _cfg, proj = _project(project)
        from backend.core.governance import build_patterns_report
        with HistoryManager.workspace_scope(str(proj.workspace_path)):
            return build_patterns_report(project)

    def governance_feed(
        self, project: str, limit: int = 20, cursor: str | None = None,
    ) -> list[dict] | dict:
        from backend.core.history import HistoryManager
        _cfg, proj = _project(project)
        bounded = max(0, min(200, int(limit)))
        if not bounded:
            empty = {"items": [], "page": {
                "limit": 0, "next_cursor": None, "has_more": False,
                "scope": "governance_history_and_native_trace",
            }}
            return [] if cursor is None else empty
        page_cursor = _feed_cursor_decode(str(cursor or ""))
        kinds = {"policy_check_result", "governance_drift", "governance_lesson",
                 "workspace_state_snapshot", "rejection", "integrity_warning",
                 "governance_synced", "governance_pushed", "governance_dissolved"}
        with HistoryManager.workspace_scope(str(proj.workspace_path)):
            history_entries = [asdict(e) for e in HistoryManager.load()
                               if e.project_name == project and e.operation in kinds]
        for entry in history_entries:
            if not entry.get("event_id"):
                encoded = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str).encode()
                entry["event_id"] = "history:" + hashlib.sha256(encoded).hexdigest()[:24]
            entry["source"] = str(entry.get("source") or "history")
            entry["_cursor_source"] = "history"
        history_entries.sort(key=lambda item: (
            str(item.get("timestamp") or ""), str(item.get("event_id") or ""),
        ), reverse=True)
        history_before = page_cursor.get("history")
        if isinstance(history_before, list) and len(history_before) == 2:
            history_entries = [item for item in history_entries if (
                str(item.get("timestamp") or ""), str(item.get("event_id") or ""),
            ) < (str(history_before[0]), str(history_before[1]))]
        history_page = history_entries[:bounded + 1]
        from backend.core.storage import get_storage
        runtime = get_storage(str(proj.workspace_path))
        # Native loop governance is already durable in Trace. Select disjoint
        # event types instead of emitting a second History copy on every turn.
        trace_before = page_cursor.get("trace")
        trace_cursor = None
        if isinstance(trace_before, list) and len(trace_before) == 3:
            trace_cursor = (str(trace_before[0]), str(trace_before[1]), int(trace_before[2]))
        trace_rows = runtime.read_trace_activity(
            _GOVERNANCE_TRACE_EVENTS, limit=bounded + 1, before=trace_cursor,
        )
        trace_page = []
        for item in trace_rows:
            record = item["record"]
            detail = {key: record[key] for key in (
                "process_id", "signal_count", "governance_version", "phase", "reason",
                "verdict", "status", "allowed", "reasons", "decision_id", "shard_count", "detail_ref",
            ) if key in record}
            trace_page.append({
                "timestamp": item["occurred_at"], "project_name": project,
                "operation": item["event_type"], "status": item["severity"],
                "source": "trace",
                "event_id": f"{item['trace_id']}:{int(item['sequence']):020d}",
                "detail": detail, "_cursor_source": "trace",
                "_trace_cursor": [item["occurred_at"], item["trace_id"], int(item["sequence"])],
            })
        combined = sorted(
            [*history_page, *trace_page],
            key=lambda item: (str(item.get("timestamp") or ""), str(item.get("event_id") or "")),
            reverse=True,
        )
        page_items = combined[:bounded]
        next_state = dict(page_cursor)
        for item in page_items:
            if item["_cursor_source"] == "history":
                next_state["history"] = [item["timestamp"], item["event_id"]]
            else:
                next_state["trace"] = item["_trace_cursor"]
        has_more = (
            len(combined) > bounded or len(history_entries) > bounded
            or len(trace_rows) > bounded
        )
        public_items = [{
            key: value for key, value in item.items() if not key.startswith("_")
        } for item in page_items]
        if cursor is None:
            return list(reversed(public_items))
        return {
            "items": public_items,
            "page": {
                "limit": bounded,
                "next_cursor": _feed_cursor_encode(next_state) if has_more and page_items else None,
                "has_more": has_more,
                "scope": "governance_history_and_native_trace",
                "ordering": "newest_first",
                "native_event_types": sorted(_GOVERNANCE_TRACE_EVENTS),
            },
        }

    def governance_releases(self, project: str) -> dict:
        from backend.core.history import HistoryManager
        _cfg, proj = _project(project)
        from backend.core.governance import list_releases
        with HistoryManager.workspace_scope(str(proj.workspace_path)):
            return list_releases(project)

    def history_list(self, project: str = "", op: str | None = None,
                     limit: int = 20) -> list[dict]:
        from backend.core.history import HistoryManager
        if project:
            _cfg, proj = _project(project)
            with HistoryManager.workspace_scope(str(proj.workspace_path)):
                entries = HistoryManager.load()
        else:
            entries = HistoryManager.load()
        if project:
            entries = [e for e in entries if e.project_name == project]
        if op:
            entries = [e for e in entries if e.operation == op]
        bounded = max(0, min(200, int(limit)))
        return [asdict(e) for e in entries[-bounded:]] if bounded else []

    # Memory, trial, formal -----------------------------------------

    def memory_snapshot(self, project: str) -> dict:
        from backend.core.identity.snapshot import snapshot_tool_memories
        from backend.core.sync_session import SyncSession
        cfg, proj = _project(project)
        session = SyncSession(proj, cfg)
        if not session.backup_path:
            raise OperationError("NO_BACKUP_CONFIGURED", "No backup is configured")
        result = snapshot_tool_memories(session.workspace_path, session.backup_path, proj)
        return result  # Preserve no_sources/partial/unknown outcomes and retention warnings.

    def memory_list(self, project: str) -> list[dict]:
        from backend.core.identity.snapshot import list_memory_snapshots
        from backend.core.sync_session import SyncSession
        cfg, proj = _project(project)
        session = SyncSession(proj, cfg)
        if not session.backup_path:
            raise OperationError("NO_BACKUP_CONFIGURED", "No backup is configured")
        return list_memory_snapshots(session.backup_path, session.workspace_path)

    def memory_restore(self, project: str, ts: str | None = None) -> dict:
        from backend.core.identity.snapshot import restore_tool_memories
        from backend.core.sync_session import SyncSession
        cfg, proj = _project(project)
        session = SyncSession(proj, cfg)
        if not session.backup_path:
            raise OperationError("NO_BACKUP_CONFIGURED", "No backup is configured")
        return restore_tool_memories(session.backup_path, session.workspace_path,
                                     snapshot_timestamp=ts, project_name=proj.name)

    @staticmethod
    def _sync_session(project: str, *, scan: bool = False, trial: bool = False):
        from backend.core.sync_session import SyncSession
        cfg, proj = _project(project)
        session = SyncSession(proj, cfg)
        if scan:
            session.step_scan()
            session.step_load_commits()
        if trial:
            session.step_check_trial()
        return session

    def trial_list(self, project: str) -> list[dict]:
        session = self._sync_session(project, trial=True)
        return [{"index": i, "hash": c.hash, "message": c.message, "author": c.author,
                 "date": c.date, "triage": c.triage.value}
                for i, c in enumerate(session.incoming_changes)]

    def trial_triage(self, project: str, index: int, action: str) -> dict:
        if action not in ("accept", "promote", "discard"):
            raise OperationError("INVALID_ACTION", f"Invalid trial action: {action}")
        session = self._sync_session(project, trial=True)
        return {"triaged": session.step_triage_incoming(index, action),
                "action": action, "index": index}

    def formal_list(self, project: str) -> list[dict]:
        session = self._sync_session(project, scan=True)
        return [{"index": i, "prefix": fc.prefix, "number": fc.number,
                 "message": fc.message, "synced": fc.synced, "pushed": fc.pushed,
                 "is_incoming": fc.is_incoming, "sources_cleared": fc.sources_cleared,
                 "source_indices": sorted(fc.source_indices), "created_at": fc.created_at}
                for i, fc in enumerate(session.formal_commits)]

    def formal_edit_message(self, project: str, index: int, message: str) -> dict:
        session = self._sync_session(project, scan=True)
        return {"updated": session.step_edit_formal_message(index, message), "index": index}

    def formal_delete(self, project: str, index: int) -> dict:
        session = self._sync_session(project, scan=True)
        return {"deleted": session.step_delete_formal(index), "index": index}

    def formal_dissolve(self, project: str, index: int) -> dict:
        session = self._sync_session(project, scan=True)
        return {"dissolved": session.step_dissolve_formal(index), "index": index}

    # Templates/providers/export ------------------------------------

    def template_list(self) -> list[dict]:
        from backend.core.template_manager import TemplateManager
        return [{"name": t.name, "description": t.description,
                 "header_format": t.header_format, "body_format": t.body_format,
                 "prefix_override": t.prefix_override} for t in TemplateManager.load()]

    def template_add(self, name: str, description: str, header_format: str = "",
                     body_format: str = "", prefix_override: str | None = None) -> dict:
        from backend.core.template_manager import CommitTemplate, TemplateManager
        with self._write_lock:
            templates = TemplateManager.load()
            if any(t.name == name for t in templates):
                raise OperationError("TEMPLATE_EXISTS", f"Template '{name}' already exists")
            templates.append(CommitTemplate(name=name, description=description,
                                             header_format=header_format,
                                             body_format=body_format,
                                             prefix_override=prefix_override))
            TemplateManager.save(templates)
        return {"added": name}

    def template_edit(self, name: str, description: str | None = None,
                      header_format: str | None = None, body_format: str | None = None,
                      prefix_override: str | None = None) -> dict:
        from backend.core.template_manager import TemplateManager
        with self._write_lock:
            templates = TemplateManager.load()
            template = next((t for t in templates if t.name == name), None)
            if template is None:
                raise OperationError("TEMPLATE_NOT_FOUND", f"Template '{name}' was not found")
            for field, value in (("description", description), ("header_format", header_format),
                                 ("body_format", body_format),
                                 ("prefix_override", prefix_override)):
                if value is not None:
                    setattr(template, field, value)
            TemplateManager.save(templates)
        return {"updated": name}

    def template_delete(self, name: str) -> dict:
        from backend.core.template_manager import TemplateManager
        if name == "default":
            raise OperationError("CANNOT_DELETE_DEFAULT", "The default template cannot be deleted")
        with self._write_lock:
            templates = TemplateManager.load()
            updated = [t for t in templates if t.name != name]
            if len(updated) == len(templates):
                raise OperationError("TEMPLATE_NOT_FOUND", f"Template '{name}' was not found")
            TemplateManager.save(updated)
        return {"deleted": name}

    def provider_status(self) -> dict:
        from backend.core.llm_config import LLMConfigManager
        from backend.core.provider_failover import revision
        config = LLMConfigManager.load()
        return {"providers": [_public_provider(p) for p in config.get("providers", [])],
                "active_provider": config.get("active_provider", ""),
                "failover_enabled": config.get("failover_enabled", False),
                "failover_order": config.get("failover_order", []),
                "failover_routes": config.get("failover_routes", {}),
                "failover_revision": revision(config),
                "circuit_breaker": config.get("circuit_breaker", {}),
                "credential_cleanup": LLMConfigManager.credential_cleanup_status(),
                "health_scope": "owning_project_daemon"}

    def provider_failover_configure(self, enabled: bool, order: list[str] | None = None, circuit_breaker: dict | None = None,
                                    provider_id: str = "", expected_revision: str = "") -> dict:
        from backend.core.llm_config import LLMConfigManager
        try:
            return LLMConfigManager.configure_failover(enabled, order, circuit_breaker, provider_id, expected_revision)
        except (ValueError, TypeError) as exc:
            raise OperationError("INVALID_FAILOVER_CONFIG", str(exc)) from exc

    def provider_save(self, provider_id: str = "", name: str = "", base_url: str = "",
                      api_key: str = "", model_id: str = "",
                      retain_api_key: bool = False,
                      protocol: str = "openai_chat",
                      context_window: int = 128000,
                      max_output_tokens: int = 4096,
                      kind: str = "remote", auth_mode: str = "api_key",
                      expected_revision: str = "", preset_id: str = "",
                      model_receipt: str = "", probe_receipt: str = "",
                      context_source: str = "user", output_source: str = "user", creation_id: str = "",
                      request_preferences: dict | None = None, models: list[dict] | None = None) -> dict:
        from backend.core.llm_config import LLMConfigManager, LLMProvider
        from backend.core.loop.provider_protocol import ProviderProtocol
        if creation_id:
            import re
            if provider_id or not re.fullmatch(r"[a-f0-9]{12,32}", creation_id):
                raise OperationError("INVALID_CREATION_ID", "Invalid new provider request identity")
        name = str(name or "").strip()
        model_id = str(model_id or "").strip()
        if not name or not base_url or not model_id:
            raise OperationError("MISSING_FIELDS", "name, base_url, and model_id are required")
        from backend.core.loop.provider_policy import provider_endpoint
        try:
            base_url = provider_endpoint(base_url, kind=kind, auth_mode=auth_mode)
        except ValueError as exc:
            raise OperationError("INVALID_PROVIDER_URL", str(exc)) from exc
        try:
            protocol = ProviderProtocol(protocol).value
        except ValueError as exc:
            raise OperationError("INVALID_PROVIDER_PROTOCOL", str(exc)) from exc
        try:
            context_window = int(context_window)
            max_output_tokens = int(max_output_tokens)
        except (TypeError, ValueError) as exc:
            raise OperationError("INVALID_PROVIDER_LIMITS", str(exc)) from exc
        if context_window < 1024 or max_output_tokens < 1:
            raise OperationError(
                "INVALID_PROVIDER_LIMITS",
                "context_window must be >= 1024 and max_output_tokens >= 1",
            )
        if max_output_tokens >= context_window:
            raise OperationError(
                "INVALID_PROVIDER_LIMITS",
                "max_output_tokens must be smaller than context_window",
            )
        existing = next((p for p in LLMConfigManager.get_providers()
                         if p.id == provider_id), None) if provider_id else None
        if provider_id and existing is None:
            raise OperationError("PROVIDER_NOT_FOUND", "Provider was deleted; refresh before saving")
        if existing and retain_api_key and auth_mode == "api_key":
            if base_url != existing.base_url or auth_mode != existing.auth_mode:
                raise OperationError("API_KEY_DECISION_REQUIRED", "Enter a key explicitly for the changed endpoint")
            api_key = existing.api_key
        if auth_mode == "none":
            api_key = ""
        if not api_key and auth_mode == "api_key":
            if existing:
                raise OperationError(
                    "API_KEY_DECISION_REQUIRED",
                    "Explicitly retain the existing key or provide a replacement",
                )
            raise OperationError("MISSING_API_KEY", "api_key is required for a new provider")
        draft = dict(base_url=base_url, api_key=api_key, model_id=model_id, protocol=protocol, kind=kind, auth_mode=auth_mode)
        from backend.core.loop.provider_preferences import validate_preferences
        try:
            preferences = validate_preferences(request_preferences if request_preferences is not None else
                                               existing.configuration.get("request_preferences", {}) if existing else {}, protocol)
        except ValueError as exc:
            raise OperationError(str(exc), "Check advanced request preferences; transport and task fields are protected") from None
        try:
            discovered = self.provider_drafts.evidence(model_receipt, {**draft, "model_id": ""}, "models")
            probed = self.provider_drafts.evidence(probe_receipt, {**draft, "request_preferences": preferences}, "probe")
        except ValueError as exc:
            raise OperationError(str(exc), "Discovery or probe expired; run it again or save without it") from None
        from backend.core.provider_profiles import profiles, apply_profile
        previous_model = existing
        if existing and all(getattr(existing, k) == draft[k] for k in ("base_url", "api_key", "kind", "auth_mode")):
            previous_profile = next((p for p in profiles(existing.to_dict()) if p["model_id"] == model_id), None)
            if previous_profile:
                previous_model = LLMProvider.from_dict(apply_profile(existing.to_dict(), previous_profile))
        same_identity = bool(previous_model and all(getattr(previous_model, k) == v for k, v in draft.items()))
        configuration = dict(previous_model.configuration) if previous_model else {}
        if configuration.get("request_preferences", {}) != preferences:
            configuration.pop("verification", None)
        if not same_identity:
            for owned in ("verification", "deployment", "sources", "warnings"):
                configuration.pop(owned, None)
        from backend.core.provider_catalog import CATALOG_VERSION, PRESETS
        if preset_id and preset_id not in {p["id"] for p in PRESETS}:
            raise OperationError("INVALID_PROVIDER_PRESET", "Unknown connection preset")
        configuration.update(preset_id=preset_id, preset_version=CATALOG_VERSION,
                             sources={"context_window": context_source, "max_output_tokens": output_source})
        if context_source not in {"user", "default", "discovered", "reference"} or output_source not in {"user", "default", "discovered", "reference"}:
            raise OperationError("INVALID_PARAMETER_SOURCE", "Unknown parameter source")
        if discovered:
            deployment = next((m for m in discovered["models"] if m["id"] == model_id), None)
            if deployment:
                configuration["deployment"] = deployment
        limits = configuration.get("deployment", {}).get("limits", {})
        warnings = []
        if max_output_tokens > limits.get("max_output_capacity", max_output_tokens) or context_window > limits.get("context_window", context_window):
            warnings.append("DECLARED_LIMIT_EXCEEDED")
        if context_source in {"discovered", "reference"} and context_window != limits.get("context_window"):
            raise OperationError("INVALID_PARAMETER_SOURCE", "The context limit does not match its declared source")
        configuration["warnings"] = warnings
        configuration["request_preferences"] = preferences
        if probed:
            configuration["verification"] = {"checks": probed["checks"], "observations": probed["observations"],
                                               "at": probed["capabilities"]["probed_at"]}
        if protocol == "auto":
            if not probed:
                raise OperationError("PROTOCOL_PROBE_REQUIRED", "Test the automatic protocol before saving, or choose an explicit protocol")
            protocol = probed["protocol"]
        provider = LLMProvider(id=provider_id or creation_id, name=name, base_url=base_url,
                               api_key=api_key, model_id=model_id,
                               protocol=protocol,
                                capabilities=(
                                    probed["capabilities"] if probed else previous_model.capabilities if same_identity and previous_model.configuration.get("request_preferences", {}) == preferences else {}
                                ),
                                context_window=context_window,
                                max_output_tokens=max_output_tokens,
                                limits_source="configured",
                                kind=kind, auth_mode=auth_mode,
                                configuration=configuration,
                                created_at=existing.created_at if existing else "")
        from backend.core.provider_profiles import compile_profiles
        try:
            provider.configuration["models"] = compile_profiles(models, provider.to_dict(), existing.to_dict() if existing else None, self.provider_drafts)
        except (ValueError, TypeError) as exc:
            raise OperationError(str(exc) if isinstance(exc, ValueError) else "INVALID_PROVIDER_MODELS", "Check the API connection's configured models; draft retained") from None
        try:
            saved = LLMConfigManager.update(provider, expected_revision) if existing else LLMConfigManager.add(provider)
        except ValueError as exc:
            raise OperationError(str(exc), "Configuration changed elsewhere; refresh before saving") from None
        if saved is None:
            raise OperationError("UPDATE_FAILED", f"Provider '{provider_id}' could not be updated")
        return {"status": "updated" if existing else "created",
                "credential_cleanup": LLMConfigManager.credential_cleanup_status(),
                "provider": _public_provider(next(p for p in LLMConfigManager.load()["providers"] if p["id"] == saved.id))}

    def provider_switch(self, provider_id: str) -> dict:
        from backend.core.llm_config import LLMConfigManager
        provider = LLMConfigManager.switch(provider_id)
        if provider is None:
            raise OperationError("PROVIDER_NOT_FOUND", f"Provider '{provider_id}' was not found")
        return {"status": "switched", "active_provider": provider_id,
                "provider": _public_provider(next(p for p in LLMConfigManager.load()["providers"] if p["id"] == provider.id))}

    def provider_model_options(self) -> dict:
        from backend.core.llm_config import LLMConfigManager
        from backend.core.provider_profiles import profiles
        config = LLMConfigManager.load()
        provider = next((p for p in config["providers"] if p["id"] == config.get("active_provider")), None)
        if not provider:
            return {"provider_id": "", "models": [], "model_id": ""}
        return {"provider_id": provider["id"], "provider_name": provider["name"], "model_id": provider["model_id"],
                "revision": LLMConfigManager.revision(provider), "models": profiles(provider)}

    def provider_model_select(self, provider_id: str, model_id: str, expected_revision: str) -> dict:
        from backend.core.llm_config import LLMConfigManager, LLMProvider
        from backend.core.provider_profiles import profiles, apply_profile
        with LLMConfigManager._lock:
            config = LLMConfigManager.load()
            provider = next((p for p in config["providers"] if p["id"] == config.get("active_provider")), None)
            if not provider or provider["id"] != provider_id:
                raise OperationError("ACTIVE_PROVIDER_CHANGED", "The current API changed; reopen model selection")
            if not expected_revision:
                raise OperationError("PROVIDER_REVISION_REQUIRED", "Reopen model selection before switching")
            profile = next((p for p in profiles(provider) if p["model_id"] == model_id), None)
            if not profile:
                raise OperationError("MODEL_NOT_CONFIGURED", "This model is not configured for the current API")
            try:
                saved = LLMConfigManager.update(LLMProvider.from_dict(apply_profile(provider, profile)), expected_revision)
            except ValueError as exc:
                raise OperationError(str(exc), "API configuration changed; reopen model selection") from None
            return {"status": "selected", "provider_id": provider_id, "model_id": saved.model_id}

    def provider_effort_options(self) -> dict:
        from backend.core.provider_effort import effort_options
        current = self.provider_model_options()
        profile = next((p for p in current["models"] if p["model_id"] == current["model_id"]), None)
        return {key: value for key, value in current.items() if key != "models"} | (
            effort_options(profile) if profile else {"levels": [], "state": "unconfigured", "effort": "", "source": ""})

    def provider_effort_select(self, provider_id: str, model_id: str, effort: str, expected_revision: str) -> dict:
        from backend.core.llm_config import LLMConfigManager, LLMProvider
        from backend.core.provider_profiles import profiles, apply_profile
        from backend.core.provider_effort import effort_options
        from backend.core.loop.provider_preferences import validate_preferences
        with LLMConfigManager._lock:
            config = LLMConfigManager.load()
            provider = next((p for p in config["providers"] if p["id"] == config.get("active_provider")), None)
            if not provider or provider["id"] != provider_id or provider["model_id"] != model_id:
                raise OperationError("ACTIVE_PROVIDER_CHANGED", "The current API or model changed; reopen effort selection")
            if not expected_revision:
                raise OperationError("PROVIDER_REVISION_REQUIRED", "Reopen effort selection before saving")
            items = profiles(provider)
            profile = next(p for p in items if p["model_id"] == model_id)
            options = effort_options(profile)
            if not isinstance(effort, str) or effort and effort not in options["levels"]:
                raise OperationError("EFFORT_NOT_DECLARED", "This effort level is not declared for the current model")
            preferences = dict(profile.get("request_preferences", {}))
            preferences.pop("reasoning_effort", None)
            if effort:
                preferences["reasoning_effort"] = effort
            try:
                profile["request_preferences"] = validate_preferences(preferences, profile["protocol"])
            except ValueError as exc:
                raise OperationError(str(exc), "Check conflicting reasoning preferences in advanced configuration") from None
            profile.pop("verification", None)
            profile["capabilities"] = {}
            updated = apply_profile(provider, profile)
            updated.setdefault("configuration", {})["models"] = items
            try:
                saved = LLMConfigManager.update(LLMProvider.from_dict(updated), expected_revision)
            except ValueError as exc:
                raise OperationError(str(exc), "API configuration changed; reopen effort selection") from None
            return {"status": "selected", "provider_id": provider_id, "model_id": saved.model_id, "effort": effort,
                    "revision": self.provider_model_options()["revision"], "effective_from": "next_model_request"}

    def provider_delete(self, provider_id: str) -> dict:
        from backend.core.llm_config import LLMConfigManager
        if not LLMConfigManager.delete(provider_id):
            raise OperationError("PROVIDER_NOT_FOUND", f"Provider '{provider_id}' was not found")
        return {"status": "deleted", "provider_id": provider_id,
                "credential_cleanup": LLMConfigManager.credential_cleanup_status(),
                "active_provider": LLMConfigManager.load().get("active_provider", "")}

    def provider_presets(self) -> dict:
        from backend.core.provider_catalog import presets
        return presets()

    def _provider_draft(self, draft: dict, require_model: bool = False) -> dict:
        from backend.core.llm_config import LLMConfigManager
        from backend.core.loop.provider_policy import provider_endpoint
        from backend.core.loop.provider_protocol import ProviderProtocol
        if not isinstance(draft, dict):
            raise OperationError("INVALID_PROVIDER_DRAFT", "Expected connection draft")
        existing = next((p for p in LLMConfigManager.get_providers() if p.id == draft.get("provider_id")), None)
        if draft.get("provider_id") and existing is None:
            raise OperationError("PROVIDER_NOT_FOUND", "Provider no longer exists")
        key = str(draft.get("api_key", ""))
        if existing and draft.get("retain_api_key") and not key:
            if str(draft.get("base_url", "")).rstrip("/") != existing.base_url.rstrip("/"):
                raise OperationError("API_KEY_DECISION_REQUIRED", "Enter a key explicitly for the changed endpoint")
            key = existing.api_key
        kind = str(draft.get("kind", "remote"))
        auth = str(draft.get("auth_mode", "api_key"))
        try:
            base = provider_endpoint(str(draft.get("base_url", "")), kind=kind, auth_mode=auth)
            protocol = ProviderProtocol(draft.get("protocol", "openai_chat")).value
        except ValueError:
            raise OperationError("INVALID_PROVIDER_CONNECTION", "Check connection URL, location, authentication and protocol") from None
        if auth == "api_key" and not key and require_model:
            raise OperationError("MISSING_API_KEY", "Enter a key before discovery or testing")
        model = str(draft.get("model_id", "")).strip() if require_model else ""
        if require_model and not model:
            raise OperationError("MISSING_MODEL", "Select or enter a model before testing")
        result = dict(base_url=base, api_key=key if auth == "api_key" else "", model_id=model,
                      protocol=protocol, kind=kind, auth_mode=auth)
        if require_model:
            from backend.core.loop.provider_preferences import validate_preferences
            try:
                result["request_preferences"] = validate_preferences(draft.get("request_preferences", {}), protocol)
            except ValueError as exc:
                raise OperationError(str(exc), "Check advanced request preferences") from None
        return result

    def _provider_preview(self, action, draft, request_id, timeout):
        from backend.core.loop.provider_transport import ProviderTransportCancelled
        from backend.core.loop.llm import StreamCancelledError
        prepared = self._provider_draft(draft, action in {"probe", "connect"})
        try:
            return self.provider_drafts.run(action, prepared, request_id, max(1, min(float(timeout), 60)))
        except (ProviderTransportCancelled, StreamCancelledError):
            raise OperationError("PROVIDER_REQUEST_CANCELLED", "Request cancelled or its total time limit expired; draft retained") from None
        except ValueError as exc:
            code = str(exc)
            raise OperationError(code if code.isupper() else "PROVIDER_PREVIEW_FAILED",
                                 "Discovery/test unavailable; check connection or enter the model manually. Draft retained") from None
        except Exception:
            raise OperationError("PROVIDER_PREVIEW_FAILED", "Connection test failed; check URL, protocol, credentials and model. Draft retained") from None

    def provider_models(self, draft: dict, request_id: str, timeout: int = 15) -> dict:
        return self._provider_preview("models", draft, request_id, timeout)

    def provider_probe(self, draft: dict, request_id: str, timeout: int = 45) -> dict:
        return self._provider_preview("probe", draft, request_id, timeout)

    def provider_connection(self, draft: dict, request_id: str, timeout: int = 30) -> dict:
        return self._provider_preview("connect", draft, request_id, timeout)

    def provider_cancel(self, request_id: str) -> dict:
        if not request_id or len(request_id) > 128:
            raise OperationError("INVALID_REQUEST_ID", "Expected configuration request ID")
        return self.provider_drafts.cancel(request_id)

    def provider_test(self, provider_id: str, timeout: int = 30) -> dict:
        from backend.core.llm_config import LLMConfigManager
        import uuid
        provider = next((p for p in LLMConfigManager.get_providers() if p.id == provider_id), None)
        if provider is None:
            raise OperationError("PROVIDER_NOT_FOUND", "Provider was not found")
        # Legacy entry point is also a preview: only provider.save commits.
        result = self.provider_probe({**provider.to_dict(), "request_preferences": provider.configuration.get("request_preferences", {})}, uuid.uuid4().hex, timeout)
        return {"ok": True, "provider_id": provider_id, "response": result["text"], "probe": result}

    def project_export(self, project: str, minimal: bool = False,
                       include_identity: bool = False, output_path: str = "",
                       output_format: str = "json") -> dict:
        from backend.core.governance import collect_state_bundle
        session = self._sync_session(project, scan=True, trial=True)
        bundle = collect_state_bundle(
            session, minimal=minimal, include_identity=include_identity,
        )
        # Empty output_path preserves the transport-neutral API used by older
        # clients.  The formal dashboard supplies a destination explicitly (or
        # accepts its workspace-local default) and receives a durable receipt.
        if not str(output_path or "").strip():
            return bundle
        format_name = str(output_format or "json").strip().lower()
        suffixes = {"json": ".json", "yaml": ".yaml", "markdown": ".md"}
        if format_name not in suffixes:
            raise OperationError(
                "EXPORT_FORMAT_UNSUPPORTED",
                "output_format must be json, yaml, or markdown",
                details={"supported": sorted(suffixes)},
            )
        raw_target = Path(str(output_path)).expanduser()
        target = (
            raw_target if raw_target.is_absolute()
            else Path(session.workspace_path) / raw_target
        ).resolve(strict=False)
        if not target.suffix:
            target = target.with_suffix(suffixes[format_name])
        if target.exists():
            raise OperationError(
                "EXPORT_DESTINATION_EXISTS",
                "The export destination already exists; choose a new path.",
                details={"path": str(target)},
            )
        try:
            if format_name == "json":
                payload = json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"
            elif format_name == "yaml":
                import yaml
                payload = yaml.safe_dump(bundle, allow_unicode=True, sort_keys=False)
            else:
                payload = self._state_bundle_markdown(project, bundle)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, target)
        except OperationError:
            raise
        except (ImportError, OSError, TypeError, ValueError) as exc:
            try:
                temporary.unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass
            raise OperationError(
                "EXPORT_WRITE_FAILED", str(exc),
                details={"path": str(target), "format": format_name},
            ) from exc
        raw = target.read_bytes()
        return {
            "ok": True, "project": project, "path": str(target),
            "format": format_name, "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "scope": "minimal" if minimal else "full",
        }

    @staticmethod
    def _state_bundle_markdown(project: str, bundle: dict) -> str:
        """Lossless-enough human view; nested data remains fenced JSON."""
        lines = [f"# Gitgo export: {project}", ""]
        for key, value in bundle.items():
            lines.extend([
                f"## {str(key).replace('_', ' ').title()}", "",
                "```json",
                json.dumps(value, ensure_ascii=False, indent=2),
                "```", "",
            ])
        return "\n".join(lines)
