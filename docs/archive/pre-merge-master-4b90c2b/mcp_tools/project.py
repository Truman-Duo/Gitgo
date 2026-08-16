"""MCP tools — project listing, status, scan, and overview."""


def register(mcp):
    """Register project tools on FastMCP instance."""

    @mcp.tool(description="列出所有已配置的 Gitgo 项目")
    def gitgo_list_projects() -> list[dict]:
        from backend.core.config import ConfigManager
        cfg = ConfigManager.load()
        return [
            {"name": p.name, "workspace": p.workspace.file_access.path,
             "backup": p.backup_path,
             "commit_prefix": p.commit_format.get("prefix", "")}
            for p in cfg.projects
        ]

    @mcp.tool(description="获取项目完整状态，包含语义分析。layered=True 时使用三层显式结构。")
    def gitgo_status(project: str, layered: bool = False) -> dict:
        from backend.core.config import ConfigManager
        from backend.core.sync_session import SyncSession
        cfg = ConfigManager.load()
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
        cfg = ConfigManager.load()
        for p in cfg.projects:
            if p.name == project:
                session = SyncSession(p, cfg)
                session.step_load_commits()
                session.step_check_trial()
                ws = p.workspace.file_access.path if p.workspace else ""
                fcs = StateReader.get_formal_commits(project, workspace_path=ws) if ws else []
                return {"project": project, "status": session.status_dict(semantic=True), "formal_commits": fcs}
        return {"error": "PROJECT_NOT_FOUND", "project": project}
