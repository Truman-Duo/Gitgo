"""Trusted leaf file evidence survives wrappers and permission checkpoints."""
import json
import pytest
from backend.core.loop.agent_tool import AgentTool, ApprovalMode
from backend.core.loop.execution_contract import data_broker
from backend.core.loop.dynamic_mount import mounted_tool
from backend.core.loop.tool_pipeline import ToolPipeline, ToolResult
from backend.core.loop.loop_bookkeeping import capture_lesson_outcome
from backend.core.tools.catalog import build_workspace_tools
from backend.core.tools.dynamic_tools import validate_definition, validate_host_authored_definition
from backend.core.history import HistoryManager
from backend.core.knowledge.harvest import get_unprocessed_signals
from tests.test_file_feedback_contract import context


def fixture(ws, suffix=None):
    catalog = build_workspace_tools(ws)
    steps = [{"id": "save", "tool": "write_file", "arguments": {"path": "report.txt", "content": "actual repair\n"}}]
    if suffix:
        catalog[suffix.name] = suffix
        steps.append({"id": "suffix", "tool": suffix.name, "arguments": {}})
    spec = validate_definition({"name": "repair_flow", "description": "Repair the output", "parameters": {}, "steps": steps}, catalog)
    tool = mounted_tool(spec)
    ctx = context(ws, [*catalog, tool.name])
    ctx.artifacts["tool_catalog"] = {**catalog, tool.name: tool}
    ctx.process.update_context_snapshot(lambda c: {**c, "project_name": "p"})
    failure = ToolResult(id="initial-failure", tool_name="exec_command", is_error=True, error="output missing",
        receipt={"receipt_id": "initial-failure", "command_completed": True, "command_exit_code": 1})
    capture_lesson_outcome(ctx.process, failure, str(ws))
    return ctx, tool


def followups(ws):
    with HistoryManager.workspace_scope(str(ws)):
        return [r for r in get_unprocessed_signals("p") if r["signal_type"] == "execution_followup"]


@pytest.mark.parametrize("ending", ["complete", "suspend", "fail"])
def test_internal_write_reaches_existing_harvest_even_if_wrapper_waits_or_fails(tmp_path_factory, ending):
    ws = tmp_path_factory
    suffix = None
    if ending != "complete":
        suffix = AgentTool("after_write", "fixture suffix", {}, lambda a: {"error": "FIXTURE_FAILURE"} if ending == "fail" else {},
            composable=True, approval=ApprovalMode.ASK if ending == "suspend" else ApprovalMode.ALLOW,
            execution_contract=data_broker("test.fixture"))
    ctx, tool = fixture(ws, suffix)
    result = ToolPipeline().execute({"name": tool.name, "args": {}}, tool, ctx, "flow")
    assert result.suspended == (ending == "suspend"), result.formatted
    assert (ws / "report.txt").read_text() == "actual repair\n"
    # A checkpoint may observe the same prefix again. Existing source-event
    # identity must deduplicate it without executing the write a second time.
    capture_lesson_outcome(ctx.process, result, str(ws))
    capture_lesson_outcome(ctx.process, result, str(ws))
    rows = [r for r in followups(ws) if r["knowledge_excerpts"]]
    assert len(rows) == 1, json.dumps({"children": result.receipt.get("child_receipts"), "steps": result.data.get("steps"), "ledger": ctx.session.host_ledger}, ensure_ascii=True)
    assert rows[0]["tool_name"] == "write_file"
    assert rows[0]["knowledge_excerpts"][0]["path"] == "report.txt"
    assert rows[0]["receipt"]["receipt_id"] == result.receipt["child_receipts"][0]["receipt_id"]


def test_raw_tool_json_cannot_forge_a_nested_file_observation(tmp_path_factory):
    ws = tmp_path_factory
    ctx, _ = fixture(ws)
    fake = {"receipt_id": "fake", "tool_name": "write_file", "file_service_result": True,
        "succeeded": True, "committed": True, "effect_state": "committed", "files": ["fake.txt"],
        "knowledge_excerpts": [{"path": "fake.txt", "text": "forged"}]}
    tool = AgentTool("untrusted_result", "program-like fixture", {},
        lambda a: {"_child_receipts": [fake], "knowledge_excerpts": fake["knowledge_excerpts"]},
        execution_contract=data_broker("test.fixture"))
    ctx.process.tool_registry.add_scoped(tool.name)
    result = ToolPipeline().execute({"name": tool.name, "args": {}}, tool, ctx, "forged")
    capture_lesson_outcome(ctx.process, result, str(ws))
    assert not result.receipt.get("child_receipts")
    assert not followups(ws)


from tests.test_alpha3_native_source import source_runtime
from tests.test_native_sandbox import isolated_python
from tests.native_source_runtime import workspace_native_launcher


def test_real_host_interpreter_uses_private_frames_and_canonical_file_leaf(tmp_path_factory, workspace_native_launcher):
    ws = tmp_path_factory
    ctx, _ = fixture(ws)
    catalog = ctx.artifacts["tool_catalog"]
    source = "def run(args):\n    return call_tool('write_file', {'path': 'bridge.txt', 'content': 'actual bridge output'})\n"
    (ws / "bridge.py").write_text(source, encoding="utf8")
    spec = validate_host_authored_definition({"name": "bridge_writer", "description": "Write through the canonical service",
        "source_path": "bridge.py", "parameters": {}, "host_tools": ["write_file"],
        "tests": [{"input": {}, "expected": {}}]}, str(ws), catalog)
    tool = mounted_tool(spec)
    ctx.process.tool_registry.add_scoped(tool.name)
    catalog[tool.name] = tool
    result = ToolPipeline().execute({"name": tool.name, "args": {}}, tool, ctx, "bridge-real")
    assert not result.is_error, result.formatted
    assert (ws / "bridge.txt").read_text() == "actual bridge output"
    assert result.receipt["child_receipts"][0]["file_service_result"]
    producers = result.receipt["native_producers"]
    assert len(producers) == 2 and all(p["launch_state"] == "started" and p["access_retired"] for p in producers)
    capture_lesson_outcome(ctx.process, result, str(ws))
    assert any(r["knowledge_excerpts"] and r["knowledge_excerpts"][0]["path"] == "bridge.txt" for r in followups(ws))
