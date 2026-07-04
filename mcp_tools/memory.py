"""MCP tools — memory snapshots, session management, and identity."""

from mcp_tools.helpers import get_config, get_project


def register(mcp):
    """Register memory/session/identity tools on FastMCP instance."""

    @mcp.tool(description="手动触发工具记忆快照（.claude/.codex/.codebuddy），保存到 backup 的 .gitgo/memories/。sync 时自动执行。")
    def gitgo_memory_snapshot(project: str) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        from backend.core.identity.snapshot import snapshot_tool_memories
        session = SyncSession(proj, cfg)
        if not session.backup_path:
            return {"error": "NO_BACKUP_CONFIGURED"}
        result = snapshot_tool_memories(session.workspace_path, session.backup_path, proj)
        return {"snapped": result.get("snapped", []), "timestamp": result.get("timestamp", "")}

    @mcp.tool(description="从 backup 恢复工具记忆到 workspace。默认使用最新快照，可通过 ts 指定时间戳。")
    def gitgo_memory_restore(project: str, ts: str | None = None) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        from backend.core.identity.snapshot import restore_tool_memories
        session = SyncSession(proj, cfg)
        if not session.backup_path:
            return {"error": "NO_BACKUP_CONFIGURED"}
        return restore_tool_memories(session.backup_path, session.workspace_path, snapshot_timestamp=ts)

    @mcp.tool(description="列出 backup 中所有可用的工具记忆快照。")
    def gitgo_memory_list(project: str) -> list[dict]:
        cfg, proj = get_project(project)
        if proj is None:
            return [{"error": "PROJECT_NOT_FOUND", "project": project}]
        from backend.core.sync_session import SyncSession
        from backend.core.identity.snapshot import list_memory_snapshots
        session = SyncSession(proj, cfg)
        if not session.backup_path:
            return [{"error": "NO_BACKUP_CONFIGURED"}]
        return list_memory_snapshots(session.backup_path)

    @mcp.tool(description="管理 session 状态：保存当前 session、查看状态、恢复 session。")
    def gitgo_session(project: str, action: str = "status") -> dict:
        if action not in ("status", "save", "resume"):
            return {"error": "INVALID_ACTION", "action": action, "valid": ["status", "save", "resume"]}
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        session = SyncSession(proj, cfg)
        if action == "save":
            path = session.save_session()
            return {"saved": True, "path": str(path)}
        elif action == "resume":
            restored = SyncSession.load_session(proj, cfg)
            fc_count = len(restored.formal_commits) if restored else 0
            return {"resumed": restored is not None, "formal_commits_restored": fc_count}
        else:
            return session.status_dict(semantic=True)

    @mcp.tool(description="导出项目完整状态快照（State Bundle），含 governance summary 和最近历史。minimal=True 时不含 history。")
    def gitgo_export(project: str, minimal: bool = False, include_identity: bool = False) -> dict:
        cfg, proj = get_project(project)
        if proj is None:
            return {"error": "PROJECT_NOT_FOUND", "project": project}
        from backend.core.sync_session import SyncSession
        from backend.core.governance import collect_state_bundle
        session = SyncSession(proj, cfg)
        session.step_scan()
        session.step_load_commits()
        session.step_check_trial()
        return collect_state_bundle(session, minimal=minimal, include_identity=include_identity)
