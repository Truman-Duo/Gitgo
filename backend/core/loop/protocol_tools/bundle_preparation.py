"""bundle preparation protocol tools."""
from __future__ import annotations

from backend.core.prompt_templates import load_prompt
from backend.core.loop.execution_contract import HOST_COMPUTE, data_broker
import json
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.code_dossier import suggest_task_shards

def install(ctx):
    tools = ctx.tools
    code_dossier = ctx.code_dossier
    dossier_store = ctx.dossier_store
    prepare_task_bundle = ctx.prepare_task_bundle
    def prepare_task_bundle(args: dict) -> dict:
        if code_dossier is None or dossier_store is None:
            return {"error": "WORKSPACE_REQUIRED", "message": "Task bundles require a bound workspace."}
        goal = str(args.get("goal", "")).strip()
        if not goal:
            return {"error": "goal is required"}
        dossier_result = code_dossier(args)
        dossier = json.loads(
            dossier_store.resolve(dossier_result["pinned"]).content
        )
        plan = suggest_task_shards(
            dossier,
            goal=goal,
            max_tokens_per_shard=int(
                args.get("max_tokens_per_shard", 12000) or 12000
            ),
            max_files_per_shard=int(
                args.get("max_files_per_shard", 24) or 24
            ),
            max_shards=int(args.get("max_shards", 8) or 8),
        )
        return {**dossier_result, "bundle": plan}


    tools["prepare_task_bundle"] = AgentTool(
        execution_contract=data_broker("host.runtime.prepare_task_bundle"),
        name="prepare_task_bundle",
        description=(
            load_prompt('tools/prepare_task_bundle')
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "target_files": {"type": "array", "items": {"type": "string"}},
                "max_files": {"type": "integer"},
                "max_tokens_per_shard": {"type": "integer"},
                "max_files_per_shard": {"type": "integer"},
                "max_shards": {"type": "integer"},
            },
            "required": ["goal", "target_files"],
        },
        execute=prepare_task_bundle,
        effect=ToolEffect.READ,
        idempotent=True,
    )


    ctx.prepare_task_bundle = prepare_task_bundle
