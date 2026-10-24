"""Project-bound discovery; the existing review/store owns lessons."""
from pathlib import Path

from backend.core.loop.agent_tool import AgentTool
from backend.core.loop.execution_contract import data_broker
from backend.core.prompt_templates import load_prompt


def build_lesson_harvest_tool(workspace, project_name):
    root = str(Path(workspace))

    def prepare(args):
        return {**dict(args or {}), "workspace_path": root, "project_name": project_name}

    def discover(args):
        from backend.core.loop.tool_wrappers import lesson_harvest
        return lesson_harvest(args)

    return AgentTool(
        name="lesson_harvest", description=load_prompt("tools/lesson_harvest"),
        parameters={"type": "object", "properties": {
            "workspace_path": {"type": "string"}, "project_name": {"type": "string"},
            "tech_stack": {"type": "string"}}, "required": []},
        execute=discover, prepare_args=prepare,
        execution_contract=data_broker("knowledge.lesson_harvest"),
        read_only=False, resources=["knowledge:observations"], timeout=120,
    )
