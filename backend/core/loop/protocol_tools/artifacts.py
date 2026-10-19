"""artifacts protocol tools."""
from __future__ import annotations
from backend.core.loop.execution_contract import HOST_COMPUTE, data_broker
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.models import ProcessStatus

def install(ctx):
    process = ctx.process
    tools = ctx.tools
    manager = ctx.manager
    _owned_child = ctx.owned_child
    def _owned_child(process_id: str):
        target = manager.get(process_id) if manager else None
        if manager is None or not manager.owns_child(process, target):
            return None
        return target


    def read_child_artifact(args: dict) -> dict:
        if manager is None:
            return {"error": "AgentProcessManager is unavailable"}
        process_id = str(args.get("process_id") or "")
        child = _owned_child(process_id)
        if child is None:
            raise PermissionError("artifact process must be an owned child task")
        if child.status != ProcessStatus.COMPLETED:
            raise ValueError("child result is not complete")
        if child.worktree.get("isolated"):
            return manager.read_sealed_artifact(
                process,
                process_id,
                str(args.get("path") or ""),
                sha256=str(args.get("sha256") or ""),
                offset=int(args.get("offset", 0) or 0),
                max_chars=int(args.get("max_chars", 24000) or 24000),
            )
        # Non-Git projects deliberately use the shared workspace.  Keep
        # the same owner/completion boundary, then reuse the canonical
        # bounded artifact reader instead of advertising an immutable
        # worktree API that can only fail in this mode.
        result = tools["artifact_read"].execute({
            "path": str(args.get("path") or ""),
            "sha256": str(args.get("sha256") or ""),
            "offset": int(args.get("offset", 0) or 0),
            "max_chars": int(args.get("max_chars", 24000) or 24000),
        })
        return {
            "process_id": child.process_id,
            "authority": "shared_workspace_current",
            **result,
        }


    tools["read_child_artifact"] = AgentTool(
        execution_contract=data_broker("host.runtime.read_child_artifact"),
        name="read_child_artifact",
        description=(
            "Read a file produced by an owned completed subprocess before review. "
            "The Host uses its immutable sealed result in isolated Git worktrees and "
            "the current bounded workspace artifact in shared non-Git projects."
        ),
        parameters={
            "type": "object",
            "properties": {
                "process_id": {"type": "string"},
                "path": {"type": "string"},
                "sha256": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 1000},
            },
            "required": ["process_id", "path"],
        },
        execute=read_child_artifact,
        read_only=True,
        effect=ToolEffect.READ,
        idempotent=True,
    )


    ctx.owned_child = _owned_child
