from __future__ import annotations

from backend.core.loop.execution_contract import NATIVE_PROCESS, data_broker

import json

from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import AgentProcess, ProcessStatus, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.context_store import build_context_tools
from backend.core.storage import StorageRuntime


def _process() -> AgentProcess:
    return AgentProcess(
        process_id="spill-process",
        role="worker",
        ring_level=RingLevel.RING_0,
        status=ProcessStatus.RUNNING,
        active_task_id="spill-task",
        session=AgentSession(session_id="spill-session"),
    )


def test_oversized_tool_result_uses_cas_locator_and_pages_without_recursion(tmp_path_factory):
    storage = StorageRuntime(tmp_path_factory)
    try:
        process = _process()
        tool = AgentTool(
            execution_contract=data_broker("test.fixture"), name="large_result",
            description="fixture",
            parameters={"type": "object", "properties": {}},
            execute=lambda _args: {"items": [f"row-{i}-" + "x" * 120 for i in range(600)]},
            effect=ToolEffect.READ,
        )
        ctx = ExecutionContext(
            process=process,
            session=process.session,
            workspace_path=str(tmp_path_factory),
            storage=storage,
        )
        result = ToolPipeline().execute(
            {"name": "large_result", "args": {}}, tool, ctx, "execution", 0,
        )
        assert result.truncated is True
        assert result.persist_path is None
        assert result.spill["locator"].startswith("tool-result:sha256:")
        assert result.receipt["tool_result_locator"] == result.spill["locator"]
        assert "TOOL_RESULT_SPILL" in result.formatted
        assert ".gitgo\\tool-results" not in result.formatted

        page = storage.read_tool_result(result.spill["locator"], max_chars=4000)
        assert page["next_offset"] is not None
        assert len(json.dumps(page, ensure_ascii=False)) <= 28_000
        second = storage.read_tool_result(
            result.spill["locator"], offset=page["next_offset"], max_chars=4000,
        )
        assert second["offset"] == page["next_offset"]
    finally:
        storage.close()


def test_tool_result_can_be_narrowed_by_pointer_and_query(tmp_path_factory):
    storage = StorageRuntime(tmp_path_factory)
    try:
        content = json.dumps({"rows": [{"name": "alpha"}, {"name": "needle"}]})
        descriptor = storage.put_tool_result(content, process_id="p", task_id="t", tool_name="x")
        pointed = storage.read_tool_result(descriptor["locator"], json_pointer="/rows/1")
        assert "needle" in pointed["content"]
        matched = storage.read_tool_result(descriptor["locator"], query="needle")
        assert matched["match"] is not None
        missing = storage.read_tool_result(descriptor["locator"], query="absent")
        assert missing["match"] is None
    finally:
        storage.close()


def test_invalid_spill_locator_uses_recovery_catalog(tmp_path_factory):
    storage = StorageRuntime(tmp_path_factory)
    try:
        manager = AgentProcessManager()
        manager.storage = storage
        process = manager.fork(
            parent_id=None, role="supervisor", tool_registry=ToolRegistry([]),
            max_steps=1, ring_level=RingLevel.RING_0,
            workspace_path=str(tmp_path_factory), task_id="spill-open",
        )
        tool = build_context_tools(process, str(tmp_path_factory))["tool_result_open"]
        result = tool.execute({"locator": "tool-result:sha256:not-a-digest"})
        assert result["error"] == "TOOL_RESULT_LOCATOR_INVALID"
        assert result["error_info"]["catalog_id"] == "GITGO-E5205"
        assert result["error_info"]["next_actions"][1]["action"] == "rerun_source_tool"
    finally:
        storage.close()


def test_isolated_runtime_binds_result_storage_without_coordination_manager(tmp_path_factory):
    from backend.core.loop.runtime import AgentRuntimeFactory, RuntimeSpec
    storage = StorageRuntime(tmp_path_factory)
    try:
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="btw-sidecar", ring_level=RingLevel.RING_3,
            tool_registry=ToolRegistry(["tool_result_open"]), max_steps=2,
            task_id="isolated", workspace_path=str(tmp_path_factory),
            actor_kind="supervisor", capability_profile_id="supervisor.answer",
            storage=storage,
        ))
        assert not hasattr(process, "_manager")
        assert process.bound_storage is storage
        descriptor = storage.put_tool_result("isolated evidence", process_id=process.process_id,
                                             task_id="isolated", tool_name="search_text")
        tool = build_context_tools(process, str(tmp_path_factory))["tool_result_open"]
        assert "isolated evidence" in tool.execute({"locator": descriptor["locator"]})["content"]
    finally:
        storage.close()
