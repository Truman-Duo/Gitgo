from backend.core.loop.execution_contract import NATIVE_PROCESS, data_broker
from pathlib import Path

import pytest

from backend.core.storage import StorageRuntime
from backend.core.tools.dynamic_tools import validate_authored_definition
from backend.core.loop.agent_tool import (
    AgentTool, CancellationMode, ToolEffect, normalize_tool_parameters,
)
from backend.core.loop.event_bus import EventBus
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import RingLevel
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tools import ToolRegistry


def _definition(workspace: Path, *, description: str = "Count values", version: int = 1) -> dict:
    return validate_authored_definition({
        "name": "count_values",
        "description": description,
        "source_path": "count_values.py",
        "parameters": {
            "type": "object",
            "properties": {"values": {"type": "array"}},
            "required": ["values"],
        },
        "tests": [{"input": {"values": [1, 2]}, "expected": {"count": 2}}],
        "_version": version,
    }, str(workspace))


def test_authored_parameter_shorthand_is_normalized_for_provider_contract(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    source_path = tmp_path / "convert.py"
    source_path.write_text("def run(args):\n    return {'path': args['path']}\n", encoding="utf-8")
    spec = validate_authored_definition({
        "name": "convert_file",
        "description": "Convert one file",
        "source_path": "convert.py",
        # This is the exact legacy/model-authored shape that reached DeepSeek
        # with a null top-level type in project 2920.
        "parameters": {"path": {"type": "string"}},
        "tests": [{"input": {"path": "a"}, "expected": {"path": "a"}}],
    }, str(tmp_path))
    assert spec["parameters"] == {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    }
    tool = AgentTool(
        execution_contract=data_broker("test.fixture"), name="legacy_saved", description="legacy", parameters={"path": {"type": "string"}},
        execute=lambda args: args,
    )
    assert tool.to_openai_function()["function"]["parameters"]["type"] == "object"


def test_tool_parameter_schema_rejects_required_names_without_properties():
    with pytest.raises(ValueError, match="missing from properties"):
        normalize_tool_parameters({"type": "object", "required": ["path"]})


def test_custom_tool_versions_survive_source_changes_and_archive(tmp_path_factory: Path, runner_transport_only):
    tmp_path = tmp_path_factory
    source_path = tmp_path / "count_values.py"
    source_path.write_text(
        "def run(args):\n    return {'count': len(args['values'])}\n",
        encoding="utf-8",
    )
    storage = StorageRuntime(tmp_path, state_home=tmp_path / "state")
    try:
        first = _definition(tmp_path)
        source = str(first.pop("_source"))
        saved = storage.save_custom_tool(first, source)
        assert saved["version"] == 1

        # The reusable asset is immutable and no longer depends on the draft.
        source_path.unlink()
        loaded = storage.load_custom_tool("count_values")
        assert loaded["source"] == source
        assert loaded["spec"]["source_sha256"] == first["source_sha256"]
        manager = AgentProcessManager()
        process = manager.fork(
            parent_id=None, role="worker", tool_registry=ToolRegistry(["count_values"]),
            max_steps=2, ring_level=RingLevel.RING_3,
            workspace_path=str(tmp_path), task_id="saved-tool-use",
        )
        runtime_spec = {**loaded["spec"], "source_ref": loaded["source_ref"]}
        tool = AgentTool(
            execution_contract=NATIVE_PROCESS, name="count_values", description="Count values",
            parameters=runtime_spec["parameters"], execute=lambda _args: {},
            read_only=True, effect=ToolEffect.READ,
            cancellation=CancellationMode.ISOLATED_PROCESS,
            composite_spec=runtime_spec,
        )
        result = ToolPipeline().execute(
            {"name": "count_values", "args": {"values": [1, 2, 3]}}, tool,
            ExecutionContext(
                process=process, session=process.session,
                workspace_path=str(tmp_path), event_bus=EventBus(),
                cancellation=process.cancellation_event, storage=storage,
            ), "saved-tool", 1,
        )
        assert result.data == {"count": 3}

        source_path.write_text(
            "def run(args):\n    return {'count': sum(1 for value in args['values'] if value)}\n",
            encoding="utf-8",
        )
        second = _definition(tmp_path, description="Count truthy values", version=2)
        second_source = str(second.pop("_source"))
        replaced = storage.save_custom_tool(second, second_source, replace=True)
        assert replaced["version"] == 2
        catalog = storage.list_custom_tools(include_archived=True)
        assert catalog["tools"][0]["version_count"] == 2
        assert catalog["tools"][0]["description"] == "Count truthy values"

        storage.set_custom_tool_archived("count_values", True)
        assert storage.list_custom_tools()["count"] == 0
        with pytest.raises(ValueError, match="CUSTOM_TOOL_ARCHIVED"):
            storage.load_custom_tool("count_values")
        storage.set_custom_tool_archived("count_values", False)
        assert storage.load_custom_tool("count_values")["version"] == 2
    finally:
        storage.close()


def test_custom_tool_source_is_privacy_scanned_before_cas(tmp_path_factory: Path):
    tmp_path = tmp_path_factory
    source_path = tmp_path / "count_values.py"
    source_path.write_text(
        (
            "def run(args):\n    token = 'ghp_"
            + "abcdefghijklmnopqrstuvwxyz1234567890"
            + "'\n    return {'token': token}\n"
        ),
        encoding="utf-8",
    )
    storage = StorageRuntime(tmp_path, state_home=tmp_path / "state")
    try:
        spec = validate_authored_definition({
            "name": "unsafe_token",
            "description": "Unsafe fixture",
            "source_path": "count_values.py",
            "parameters": {"type": "object"},
            "tests": [{"input": {}, "expected": {"token": "redacted"}}],
        }, str(tmp_path))
        source = str(spec.pop("_source"))
        with pytest.raises(Exception, match="CUSTOM_TOOL_PRIVACY_BLOCKED"):
            storage.save_custom_tool(spec, source)
        assert storage.list_custom_tools(include_archived=True)["count"] == 0
    finally:
        storage.close()
