"""MCP tool — gitgo_cache_stats: 文件哈希缓存统计查询。"""

from __future__ import annotations


def register(mcp):
    """Register cache stats tool on FastMCP instance."""

    @mcp.tool(description="查询项目文件哈希缓存的 hit/miss 统计和条目数")
    def gitgo_cache_stats(project: str) -> dict:
        """Get file hash cache statistics for a project.

        Returns hit count, miss count, hit rate, entry counts.
        Requires daemon to be running for the project.
        """
        try:
            from mcp_tools.daemon_registry import get_client
            client = get_client(project)
            if client.is_running():
                result = client.send_command({"cmd": "cache_stats"})
                return {"project": project, **result}
        except Exception:
            pass

        return {
            "project": project,
            "daemon_online": False,
            "message": "Daemon not running. Start daemon to get live cache stats.",
        }
