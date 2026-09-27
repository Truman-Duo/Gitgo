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
    "context_window_exceeded", "task_bundle_delegated", "agent_dag_admitted",
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
            return cfg, project
    raise OperationError("PROJECT_NOT_FOUND", f"Project '{project_name}' was not found")


def _public_provider(provider: dict) -> dict:
    result = {k: v for k, v in provider.items() if k not in {"api_key", "secret_ref"}}
    key = str(provider.get("api_key", ""))
    result["api_key_present"] = bool(key)
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
        from .deletion import DeletionService
        self.deletion = DeletionService(prepare_runtime=prepare_deletion)
        self._operations: dict[str, Callable[..., Any]] = {
            "project.list": self.project_list,
            "project.create": self.project_create,
            "project.list_archived": self.project_list_archived,
            "project.archive": self.project_archive,
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
            "config.set": self.config_set,
            "config.web_search.test": self.config_web_search_test,
            "publish.get": self.publish_get,
            "publish.set": self.publish_set,
            "lesson.list": self.lesson_list,
            "lesson.search": self.lesson_search,
            "lesson.verify": self.lesson_verify,
            "lesson.harvest.preview": self.lesson_harvest_preview,
            "lesson.harvest.resolve": self.lesson_harvest_resolve,
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
        return [
            {
                "name": p.name,
                "workspace": p.workspace_path,
                "backup": p.backup_path,
                "commit_prefix": p.commit_format.get("prefix", ""),
                "daemonOnline": self._daemon_online(p.name),
            }
            for p in cfg.projects if not p.archived
        ]

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
        if workspace == Path(workspace.anchor):
            raise OperationError(
                "UNSAFE_WORKSPACE_PATH", "A filesystem root cannot be used as a project workspace",
            )
        with self._write_lock:
            cfg = ConfigManager.load()
            if any(p.name.lower() == clean_name.lower() for p in cfg.projects):
                raise OperationError("DUPLICATE_NAME", f"Project '{clean_name}' already exists")
            for existing in cfg.projects:
                if existing.workspace_path and Path(existing.workspace_path).resolve(strict=False) == workspace:
                    raise OperationError(
                        "WORKSPACE_ALREADY_REGISTERED",
                        f"Workspace is already registered as project '{existing.name}'",
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

    def project_archive(self, name: str = "") -> dict:
        with self._write_lock:
            cfg = ConfigManager.load()
            if not name:
                archived = [p.name for p in cfg.projects if p.archived]
                return {"archived_projects": archived, "count": len(archived)}
            for project in cfg.projects:
                if project.name.lower() == name.lower():
                    project.archived = not project.archived
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

    def config_get(self, key: str = "") -> dict:
        cfg = ConfigManager.load()
        if not key:
            return {"safety": cfg.safety, "language": cfg.language,
                    "verbose": cfg.verbose, "auto_compact": cfg.auto_compact,
                    "agent_routing": cfg.agent_routing,
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

    def config_set(self, key: str, value: Any) -> dict:
        with self._write_lock:
            cfg = ConfigManager.load()
            parts = key.split(".")
            if len(parts) == 2 and parts[0] == "safety":
                if parts[1] in {"delete_delay_minutes", "process_delete_delay_minutes"}:
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
            elif key == "auto_compact":
                cfg.auto_compact = bool(value)
            elif key == "agent_routing":
                if value not in ("owner", "fresh"):
                    raise OperationError("INVALID_VALUE", "agent_routing must be 'owner' or 'fresh'")
                cfg.agent_routing = value
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
                    terminal = str(value or "auto").strip().lower()
                    if terminal not in {"auto", "current", "windows_terminal", "custom"}:
                        raise OperationError(
                            "INVALID_TERMINAL_MODE",
                            "Terminal must be auto, current, windows_terminal, or custom",
                        )
                    cfg.launcher["terminal"] = terminal
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

    def publish_get(self, project: str) -> dict:
        _cfg, proj = _project(project)
        remote = proj.release.remote.url if proj.release.remote else ""
        return {
            "project": proj.name,
            "commit_format": dict(proj.commit_format or {}),
            "privacy": dict(proj.outbound_policy or {}),
            "remote_url": str(remote or ""),
            "stages": {"trial_configured": bool(proj.trial_path),
                       "release_configured": bool(remote or proj.backup_path)},
        }

    def publish_set(self, project: str, section: str, value: Any) -> dict:
        from backend.core.config import normalize_outbound_policy
        from backend.models import RemoteTarget
        clean_section = str(section or "").strip().lower()
        with self._write_lock:
            cfg, proj = _project(project)
            if clean_section == "privacy":
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
                raise OperationError("INVALID_PUBLISH_SECTION", "section must be privacy, commit_format or remote_url")
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
            return [{**item.to_dict(), "applicability": assess_lesson(item, workspace)}
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
                    workspace, query, project_name=project, tech_stack=tech_stack)]

    def lesson_verify(self, project: str, lesson_id: str) -> dict:
        from backend.core.knowledge.lesson import LessonManager
        cfg, proj = _project(project)
        from backend.core.sync_session import SyncSession
        workspace = Path(SyncSession(proj, cfg).workspace_path)
        result = LessonManager.verify(workspace, lesson_id, project_name=project)
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
        llm = LLMProvider(
            configured.base_url, configured.api_key, configured.model_id,
            protocol=configured.protocol,
            capabilities=configured.runtime_capabilities(),
        )
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
                lessons = harvest_llm_summary(
                    batch, llm, str(workspace), project, raise_on_error=True,
                )
            except Exception as exc:
                fail_harvest(project, [signal_id], str(exc))
                raise OperationError("LESSON_HARVEST_FAILED", str(exc)) from exc
            if not lessons:
                resolve_harvest_proposal(
                    project,
                    stage_harvest_proposal(project, [signal_id], [])["proposal_id"],
                    accepted=False,
                )
                raise OperationError(
                    "LESSON_NOT_ACTIONABLE",
                    "The observation did not contain a reusable, testable lesson.",
                )
            proposal = stage_harvest_proposal(project, [signal_id], lessons)
        return {
            **proposal,
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
        with HistoryManager.workspace_scope(str(workspace)):
            proposal = get_harvest_proposal(project, proposal_id)
            if proposal is None:
                raise OperationError(
                    "LESSON_HARVEST_PROPOSAL_NOT_FOUND",
                    "The harvest proposal is missing or was already resolved.",
                )
            saved_ids: list[str] = []
            if clean_action == "accept":
                for raw in proposal["candidates"]:
                    lesson = Lesson.from_dict(dict(raw or {}))
                    LessonManager.save_pending(workspace, lesson)
                    saved_ids.append(lesson.id)
            resolved = resolve_harvest_proposal(
                project, proposal_id,
                accepted=clean_action == "accept", lesson_ids=saved_ids,
            )
        return {**resolved, "count": len(saved_ids)}

    def contract_show(self, project: str) -> dict:
        from backend.core.contract import ContractManager, detect_drift
        from backend.core.sync_session import SyncSession
        cfg, proj = _project(project)
        session = SyncSession(proj, cfg)
        workspace = Path(session.workspace_path)
        contract = ContractManager.load(workspace)
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
        return {"snapped": result.get("snapped", []), "timestamp": result.get("timestamp", "")}

    def memory_list(self, project: str) -> list[dict]:
        from backend.core.identity.snapshot import list_memory_snapshots
        from backend.core.sync_session import SyncSession
        cfg, proj = _project(project)
        session = SyncSession(proj, cfg)
        if not session.backup_path:
            raise OperationError("NO_BACKUP_CONFIGURED", "No backup is configured")
        return list_memory_snapshots(session.backup_path)

    def memory_restore(self, project: str, ts: str | None = None) -> dict:
        from backend.core.identity.snapshot import restore_tool_memories
        from backend.core.sync_session import SyncSession
        cfg, proj = _project(project)
        session = SyncSession(proj, cfg)
        if not session.backup_path:
            raise OperationError("NO_BACKUP_CONFIGURED", "No backup is configured")
        return restore_tool_memories(session.backup_path, session.workspace_path,
                                     snapshot_timestamp=ts)

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
        config = LLMConfigManager.load()
        return {"providers": [_public_provider(p) for p in config.get("providers", [])],
                "active_provider": config.get("active_provider", ""),
                "failover_enabled": config.get("failover_enabled", False),
                "failover_order": config.get("failover_order", [])}

    def provider_save(self, provider_id: str = "", name: str = "", base_url: str = "",
                      api_key: str = "", model_id: str = "",
                      retain_api_key: bool = False,
                      protocol: str = "openai_chat",
                      context_window: int = 128000,
                      max_output_tokens: int = 4096) -> dict:
        from backend.core.llm_config import LLMConfigManager, LLMProvider
        from backend.core.loop.provider_protocol import ProviderProtocol
        name = str(name or "").strip()
        model_id = str(model_id or "").strip()
        if not name or not base_url or not model_id:
            raise OperationError("MISSING_FIELDS", "name, base_url, and model_id are required")
        base_url = _normalize_provider_base_url(base_url)
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
        if existing and retain_api_key:
            api_key = existing.api_key
        if not api_key:
            if existing:
                raise OperationError(
                    "API_KEY_DECISION_REQUIRED",
                    "Explicitly retain the existing key or provide a replacement",
                )
            raise OperationError("MISSING_API_KEY", "api_key is required for a new provider")
        provider = LLMProvider(id=provider_id, name=name, base_url=base_url,
                               api_key=api_key, model_id=model_id,
                               protocol=protocol,
                                capabilities=(
                                    existing.capabilities
                                    if existing and existing.protocol == protocol else {}
                                ),
                                context_window=context_window,
                                max_output_tokens=max_output_tokens,
                                limits_source="configured",
                                created_at=existing.created_at if existing else "")
        saved = LLMConfigManager.update(provider) if existing else LLMConfigManager.add(provider)
        if saved is None:
            raise OperationError("UPDATE_FAILED", f"Provider '{provider_id}' could not be updated")
        return {"status": "updated" if existing else "created",
                "provider": _public_provider(saved.to_dict())}

    def provider_switch(self, provider_id: str) -> dict:
        from backend.core.llm_config import LLMConfigManager
        provider = LLMConfigManager.switch(provider_id)
        if provider is None:
            raise OperationError("PROVIDER_NOT_FOUND", f"Provider '{provider_id}' was not found")
        return {"status": "switched", "active_provider": provider_id,
                "provider": _public_provider(provider.to_dict())}

    def provider_delete(self, provider_id: str) -> dict:
        from backend.core.llm_config import LLMConfigManager
        if not LLMConfigManager.delete(provider_id):
            raise OperationError("PROVIDER_NOT_FOUND", f"Provider '{provider_id}' was not found")
        return {"status": "deleted", "provider_id": provider_id,
                "active_provider": LLMConfigManager.load().get("active_provider", "")}

    def provider_test(self, provider_id: str, timeout: int = 30) -> dict:
        from backend.core.llm_config import LLMConfigManager
        from backend.core.loop.provider_probe import ProviderProbe
        provider = next((p for p in LLMConfigManager.get_providers()
                         if p.id == provider_id), None)
        if provider is None:
            raise OperationError("PROVIDER_NOT_FOUND", f"Provider '{provider_id}' was not found")
        try:
            result = ProviderProbe().probe(
                base_url=provider.base_url, api_key=provider.api_key,
                model_id=provider.model_id, protocol=provider.protocol,
                timeout=max(1, min(int(timeout), 60)),
            )
        except Exception as exc:
            raise OperationError("PROVIDER_PROBE_FAILED", str(exc)) from exc
        provider.protocol = result.protocol
        provider.capabilities = result.capabilities.to_dict()
        provider.capabilities["context_window"] = provider.context_window
        provider.capabilities["max_output_tokens"] = provider.max_output_tokens
        LLMConfigManager.update(provider)
        probe = result.to_dict()
        probe["capabilities"] = dict(provider.capabilities)
        return {
            "ok": True, "provider_id": provider_id,
            "response": result.text, "probe": probe,
        }

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
