#!/usr/bin/env python
"""Restricted local control MCP for Gitgo's canonical Native Host."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP
from mcp_tools.control import register, shutdown


mcp = FastMCP(
    "gitgo-control",
    instructions=(
        "Control Gitgo only through its canonical Native Host. Read status or "
        "Trace before guessing. Never fabricate a decision id, silently grant "
        "permissions, or treat MCP as the product UI. Use the formal Dashboard "
        "for visual acceptance."
    ),
)
register(mcp)


if __name__ == "__main__":
    try:
        mcp.run(transport="stdio")
    finally:
        shutdown()
