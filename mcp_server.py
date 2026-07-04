#!/usr/bin/env python
"""MCP Server for Gitgo — exposes workflow tools to AI agents via MCP protocol.

Usage:
    python mcp_server.py                    # stdio transport (Claude Desktop)
    python mcp_server.py --sse              # SSE transport (web UIs)
    python mcp_server.py --http             # Streamable HTTP transport

Configure Claude Desktop (`claude_desktop_config.json`):
    {
      "mcpServers": {
        "gitgo": {
          "command": "python",
          "args": ["mcp_server.py"],
          "cwd": "/path/to/gitgo"
        }
      }
    }
"""

from __future__ import annotations

import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from mcp.server.fastmcp import FastMCP
from mcp_tools import register_all

mcp = FastMCP(
    "gitgo",
    instructions="Gitgo 工作区同步工具 — 扫描文件变更、创建正式提交、同步到备份仓库并推送到远程。",
)

register_all(mcp)

if __name__ == "__main__":
    import argparse
    import signal

    def _on_shutdown(signum=None, frame=None):
        try:
            from mcp_tools.daemon_registry import shutdown_all
            shutdown_all()
        except Exception:
            pass
        import sys as _sys
        _sys.exit(0)

    signal.signal(signal.SIGTERM, _on_shutdown)
    signal.signal(signal.SIGINT, _on_shutdown)

    parser = argparse.ArgumentParser(description="Gitgo MCP Server")
    parser.add_argument("--sse", action="store_true", help="Use SSE transport")
    parser.add_argument("--http", action="store_true", help="Use streamable HTTP transport")
    args = parser.parse_args()

    if args.sse:
        mcp.run(transport="sse")
    elif args.http:
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
