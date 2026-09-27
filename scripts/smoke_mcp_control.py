"""Read-only stdio acceptance for Gitgo's optional control MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def smoke(project: str) -> dict:
    repository = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command="cmd.exe",
        args=["/d", "/c", str(repository / "run_mcp_control.bat")],
        cwd=str(repository),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            names = sorted(item.name for item in listing.tools)
            expected = {
                "gitgo_control_status", "gitgo_control_chat", "gitgo_control_decide",
                "gitgo_control_feedback", "gitgo_control_stop",
                "gitgo_control_compact", "gitgo_control_trace",
            }
            if set(names) != expected:
                raise RuntimeError(f"unexpected control surface: {names}")
            status = await session.call_tool(
                "gitgo_control_status", {"project": project},
            )
            if status.isError:
                raise RuntimeError(f"status failed: {status.content}")
            return {"tools": names, "status_read": True, "project": project}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="gitgo")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(smoke(args.project)), indent=2))
