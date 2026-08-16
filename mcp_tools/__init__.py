"""MCP tools — register all tool modules."""

import atexit


def register_all(mcp):
    """Register all tool groups on the FastMCP instance."""
    from mcp_tools.project import register as reg_project
    from mcp_tools.sync import register as reg_sync
    from mcp_tools.governance import register as reg_governance
    from mcp_tools.knowledge import register as reg_knowledge
    from mcp_tools.memory import register as reg_memory

    from mcp_tools.loop import register as reg_loop
    from mcp_tools.llm_config import register as reg_llm
    from mcp_tools.cache_stats import register as reg_cache

    reg_project(mcp)
    reg_sync(mcp)
    reg_governance(mcp)
    reg_knowledge(mcp)
    reg_memory(mcp)
    reg_loop(mcp)
    reg_llm(mcp)
    reg_cache(mcp)

    # Ensure daemon subprocesses are cleaned up on exit
    atexit.register(_shutdown_daemons)


def _shutdown_daemons():
    """Stop all daemon subprocesses managed by daemon_registry."""
    try:
        from mcp_tools.daemon_registry import shutdown_all
        shutdown_all()
    except Exception:
        pass
