"""MCP tools — project listing, status, scan, overview, create, archive, delete."""

import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path


def _sweep_pending_deletes(cfg):
    """Lazily remove projects whose pending hard-delete deadline has passed.

    Hard delete with a delay only stamps `pending_hard_delete_at`; nothing ever
    executed the rmtree. Dashboard polls list_projects / list_archived_projects,
    so running the sweep at the top of those tools makes the deletion actually
    happen once the deadline arrives.
    """
    from backend.core.config import ConfigManager

    changed = False
    now = datetime.now()
    for p in list(cfg.projects):
        if not p.pending_hard_delete_at:
            continue
        try:
            deadline = datetime.fromisoformat(p.pending_hard_delete_at)
        except ValueError:
            continue
        if now < deadline:
            continue
        wsp = (
            Path(p.workspace.file_access.path)
            if p.workspace and p.workspace.file_access.path
            else None
        )
        if wsp and wsp.exists():
            shutil.rmtree(wsp, ignore_errors=True)
        cfg.projects.remove(p)
        changed = True

    if changed:
        ConfigManager.save(cfg)
    return cfg


def register(mcp):
    """Register project tools on FastMCP instance."""

    @mcp.tool(description="列出所有已配置的 Gitgo 项目（活跃项目，不含已归档）")
    def gitgo_list_projects() -> list[dict]:
        from backend.core.config import ConfigManager
        cfg = _sweep_pending_deletes(ConfigManager.load())
        return [
            {"name": p.name, "workspace": p.workspace.file_access.path,
             "backup": p.backup_path,
             "commit_prefix": p.commit_format.get("prefix", ""),
             "daemonOnline": False}
            for p in cfg.projects
            if not p.archived
        ]

    @mcp.tool(description="创建新项目。workspace_path 必填，release_url 和 llm_provider 可选。")
    def gitgo_create_project(
        name: str,
        workspace_path: str,
        release_url: str = "",
        llm_provider: str = "",
    ) -> dict:
        from backend.core.config import ConfigManager, ProjectConfig
        from backend.models import FileAccessKind, RepoNode

        cfg = ConfigManager.load()

        # Check for duplicate name
        for p in cfg.projects:
            if p.name.lower() == name.lower():
                return {"error": "DUPLICATE_NAME", "message": f"Project '{name}' already exists"}

        # Validate workspace path
        wsp = Path(workspace_path).resolve()
        if not wsp.exists():
            try:
                wsp.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {"error": "INVALID_PATH", "message": f"Cannot create workspace: {e}"}

        proj = ProjectConfig(
            name=name,
            workspace=RepoNode(
                file_access_kind=FileAccessKind.LOCAL,
                path=str(wsp),
            ),
        )

        if release_url:
            proj.release = RepoNode(
                git_url=release_url,
                file_access_kind=FileAccessKind.LOCAL,
            )

        cfg.projects.append(proj)
        ConfigManager.save(cfg)
        return {
            "ok": True,
            "name": name,
            "workspace": str(wsp),
            "release_url": release_url or "",
            "message": f"Project '{name}' created",
        }

    @mcp.tool(description="切换项目归档状态。无参时列出已归档项目，带 name 参数时切换对应项目的归档状态。")
    def gitgo_archive_project(name: str = "") -> dict:
        from backend.core.config import ConfigManager
        cfg = ConfigManager.load()

        if not name:
            archived = [p.name for p in cfg.projects if p.archived]
            return {"archived_projects": archived, "count": len(archived)}

        for p in cfg.projects:
            if p.name.lower() == name.lower():
                p.archived = not p.archived
                ConfigManager.save(cfg)
                state = "archived" if p.archived else "restored"
                return {"ok": True, "name": p.name, "state": state}
        return {"error": "PROJECT_NOT_FOUND", "name": name}

    @mcp.tool(description="列出所有已归档的项目及其详情。")
    def gitgo_list_archived_projects() -> list[dict]:
        from backend.core.config import ConfigManager
        cfg = _sweep_pending_deletes(ConfigManager.load())
        return [
            {
                "name": p.name,
                "workspace": p.workspace.file_access.path,
                "release_url": p.release.git_url or p.release.file_access.path,
                "archived": True,
                "pending_hard_delete_at": p.pending_hard_delete_at or "",
            }
            for p in cfg.projects
            if p.archived
        ]

    @mcp.tool(description="删除项目。mode='soft' 删除治理数据/配置/索引保留项目文件，mode='hard' 全删（延迟执行）。")
    def gitgo_delete_project(name: str, mode: str = "soft") -> dict:
        from backend.core.config import ConfigManager
        cfg = ConfigManager.load()

        for p in cfg.projects:
            if p.name.lower() == name.lower():
                wsp = Path(p.workspace.file_access.path) if p.workspace.file_access.path else None

                if mode == "hard":
                    delay = cfg.safety.get("delete_delay_minutes", 10)
                    if delay == 0:
                        # Immediate hard delete
                        if wsp and wsp.exists():
                            shutil.rmtree(wsp, ignore_errors=True)
                        cfg.projects.remove(p)
                        ConfigManager.save(cfg)
                        return {"ok": True, "name": name, "mode": "hard", "immediate": True}
                    else:
                        p.pending_hard_delete_at = (
                            datetime.now() + timedelta(minutes=delay)
                        ).isoformat()
                        ConfigManager.save(cfg)
                        return {
                            "ok": True, "name": name, "mode": "hard",
                            "pending": True, "delay_minutes": delay,
                            "delete_at": p.pending_hard_delete_at,
                        }

                # Soft delete: remove governance data, keep project files
                if wsp and wsp.exists():
                    gitgo_dir = wsp / ".gitgo"
                    if gitgo_dir.exists():
                        shutil.rmtree(gitgo_dir, ignore_errors=True)
                cfg.projects.remove(p)
                ConfigManager.save(cfg)
                return {"ok": True, "name": name, "mode": "soft"}

        return {"error": "PROJECT_NOT_FOUND", "name": name}

    @mcp.tool(description="取消项目的延迟硬删除。")
    def gitgo_cancel_pending_delete(name: str) -> dict:
        from backend.core.config import ConfigManager
        cfg = ConfigManager.load()

        for p in cfg.projects:
            if p.name.lower() == name.lower():
                if p.pending_hard_delete_at:
                    p.pending_hard_delete_at = ""
                    ConfigManager.save(cfg)
                    return {"ok": True, "name": name, "message": "Pending delete cancelled"}
                return {"ok": False, "name": name, "message": "No pending delete"}
        return {"error": "PROJECT_NOT_FOUND", "name": name}

    @mcp.tool(description="读取全局配置（safety.delete_delay_minutes 等）。key 为空时返回全部。")
    def gitgo_config_get(key: str = "") -> dict:
        from backend.core.config import ConfigManager
        cfg = ConfigManager.load()
        if key:
            keys = key.split(".")
            val = cfg
            for k in keys:
                if isinstance(val, dict):
                    val = val.get(k)
                elif hasattr(val, k):
                    val = getattr(val, k)
                else:
                    return {"error": "INVALID_KEY", "key": key}
            return {"key": key, "value": val}
        return {
            "safety": cfg.safety,
            "language": cfg.language,
            "theme": cfg.theme,
            "animation": cfg.animation,
        }

    @mcp.tool(description="设置全局配置项。例如 key='safety.delete_delay_minutes', value=10。")
    def gitgo_config_set(key: str, value: any) -> dict:
        from backend.core.config import ConfigManager
        cfg = ConfigManager.load()
        parts = key.split(".")
        if parts[0] == "safety":
            cfg.safety[parts[1]] = value
            ConfigManager.save(cfg)
            return {"ok": True, "key": key, "value": value}
        if parts[0] == "language":
            cfg.language = str(value)
            ConfigManager.save(cfg)
            return {"ok": True, "key": key, "value": str(value)}
        if parts[0] == "theme":
            cfg.theme = str(value)
            ConfigManager.save(cfg)
            return {"ok": True, "key": key, "value": str(value)}
        return {"error": "UNKNOWN_KEY", "key": key}

    @mcp.tool(description="获取项目完整状态，包含语义分析。layered=True 时使用三层显式结构。")
    def gitgo_status(project: str, layered: bool = False) -> dict:
        from backend.core.config import ConfigManager
        from backend.core.sync_session import SyncSession
        cfg = _sweep_pending_deletes(ConfigManager.load())
        for p in cfg.projects:
            if p.name == project:
                session = SyncSession(p, cfg)
                session.step_scan()
                session.step_load_commits()
                session.step_check_trial()
                return session.status_dict(semantic=True, layered=layered)
        return {"error": "PROJECT_NOT_FOUND", "project": project}

    @mcp.tool(description="扫描工作区文件变更，返回变更条目列表和语义状态。")
    def gitgo_scan(project: str) -> dict:
        from backend.core.config import ConfigManager
        from backend.core.sync_session import SyncSession
        cfg = ConfigManager.load()
        for p in cfg.projects:
            if p.name == project:
                session = SyncSession(p, cfg)
                entries = session.step_scan()
                session.step_load_commits()
                return {
                    "entries_total": len(entries),
                    "entries_changed": sum(1 for e in entries if e.selected),
                    "entries": [{"path": e.path, "status": e.status, "selected": e.selected} for e in entries],
                    "semantic": session.status_dict(semantic=True).get("semantic", {}),
                }
        return {"error": "PROJECT_NOT_FOUND", "project": project}

    @mcp.tool(description="轻量级项目概览。不触发完整 SHA256 扫描，适合 Dashboard 快速检查。")
    def gitgo_overview(project: str) -> dict:
        from backend.core.config import ConfigManager
        from backend.core.sync_session import SyncSession
        from backend.core.state_reader import StateReader
        cfg = _sweep_pending_deletes(ConfigManager.load())
        for p in cfg.projects:
            if p.name == project:
                session = SyncSession(p, cfg)
                session.step_load_commits()
                session.step_check_trial()
                ws = p.workspace.file_access.path if p.workspace else ""
                fcs = StateReader.get_formal_commits(project, workspace_path=ws) if ws else []
                return {"project": project, "status": session.status_dict(semantic=True), "formal_commits": fcs}
        return {"error": "PROJECT_NOT_FOUND", "project": project}
