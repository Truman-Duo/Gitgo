"""Expected file results survive isolation without weakening crash/effect boundaries."""
from pathlib import Path

import pytest

from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.error_taxonomy import classify_tool_error
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.tool_pipeline import ToolPipeline, ToolExecutionCancelled
from backend.core.loop.tool_execution import ToolExecution
from backend.core.tools.catalog import build_workspace_tools
from backend.core.tools.workspace_tools import read_file
from backend.core.loop.recovery_guidance import lookup_guidance


def context(root, names):
    p = AgentProcess("file-feedback", "worker", RingLevel.RING_3, active_task_id="task",
        worktree_path=str(root), session=AgentSession(), tool_registry=ToolRegistry(names))
    return ExecutionContext(process=p, session=p.session, workspace_path=str(root))


def test_missing_read_survives_real_runner_and_does_not_rollback_next_read(tmp_path_factory):
    root = tmp_path_factory
    (root / "actual.txt").write_text("current evidence", encoding="utf-8")
    tools = build_workspace_tools(root)
    ctx = context(root, ["read_file"])
    ctx.artifacts["tool_catalog"] = tools
    execution = ToolExecution(execution_id="read-recovery", ctx=ctx, tool_calls=[
        {"name": "read_file", "args": {"path": "absent.txt"}},
        {"name": "read_file", "args": {"path": "actual.txt"}},
    ])
    execution.begin()
    results = execution.execute_batch(tools)
    assert len(results) == 2 and not execution._rolled_back
    missing, actual = results
    assert missing.is_error and missing.diagnostics["nature"] == "business"
    assert missing.diagnostics["code"] == "FILE_NOT_FOUND"
    assert missing.diagnostics["error_info"]["catalog_id"] == "GITGO-E3510"
    assert missing.receipt["effect_state"] == "not_committed"
    assert not actual.is_error and actual.data["content"] == "current evidence"
    assert not (root / "absent.txt").exists()
    help_result = lookup_guidance("GITGO-E3510")
    assert help_result["instructions_format"] == "markdown"
    assert "## Correct the target or reader" in help_result["instructions"]


@pytest.mark.parametrize("target,code", [(".", "IS_DIRECTORY"), ("binary.dat", "BINARY_FILE")])
def test_expected_read_target_types_are_business_results_after_isolation(tmp_path_factory, target, code):
    (tmp_path_factory / "binary.dat").write_bytes(b"binary\x00data")
    tools = build_workspace_tools(tmp_path_factory)
    ctx = context(tmp_path_factory, ["read_file"])
    result = ToolPipeline().execute({"name": "read_file", "args": {"path": target}}, tools["read_file"], ctx, "target")
    assert result.is_error and result.diagnostics["code"] == code
    assert result.diagnostics["nature"] == "business"
    assert result.receipt["effect_state"] == "not_committed"
    assert result.diagnostics["error_info"]["guidance_ref"]["arguments"]["focus"] == "guidance"


def test_read_failure_diagnostic_preserves_io_cause_and_confinement(tmp_path_factory, monkeypatch):
    target = tmp_path_factory / "locked.txt"
    target.write_text("original", encoding="utf-8")
    original = Path.read_bytes
    def denied(path):
        if path == target:
            raise OSError("test reader unavailable")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", denied)
    result = read_file({"_workspace": str(tmp_path_factory), "path": "locked.txt"})
    assert result["error"] == "READ_ERROR"
    assert result["error_info"]["details"]["detail"] == "test reader unavailable"
    assert result["effect_state"] == "not_committed"
    with pytest.raises(PermissionError):
        read_file({"_workspace": str(tmp_path_factory), "path": "../outside-missing.txt"})


@pytest.mark.parametrize("effect", [ToolEffect.READ, ToolEffect.WORKSPACE_WRITE])
def test_typed_timeout_keeps_effect_uncertainty_without_auto_replay(tmp_path_factory, effect):
    calls = []
    def timeout(_):
        calls.append(1)
        if effect == ToolEffect.WORKSPACE_WRITE:
            (tmp_path_factory / "partial.txt").write_text("effect before timeout", encoding="utf-8")
        raise TimeoutError("arbitrary message with no timeout keyword")
    tool = AgentTool(name="probe", description="probe", parameters={"type": "object"}, execute=timeout, effect=effect)
    ctx = context(tmp_path_factory, ["probe"])
    result = ToolPipeline().execute({"name": "probe", "args": {}}, tool, ctx, "timeout")
    assert result.is_error and result.diagnostics["code"] == "TOOL_TIMEOUT"
    assert result.diagnostics["nature"] == "crash" and len(calls) == 1
    expected = "ambiguous" if effect == ToolEffect.WORKSPACE_WRITE else "not_committed"
    assert result.receipt["effect_state"] == expected
    assert result.diagnostics["error_info"]["effect_state"] == expected
    assert result.diagnostics["error_info"]["catalog_id"] == "GITGO-E3810"
    if effect == ToolEffect.WORKSPACE_WRITE:
        assert (tmp_path_factory / "partial.txt").read_text() == "effect before timeout"


def test_internal_missing_resource_remains_a_crash():
    # The generic classifier must not relabel every missing implementation file as a bad user path.
    assert classify_tool_error(FileNotFoundError("missing internal executable")).code == "TOOL_CRASH"


def test_cancelled_mutation_preserves_partial_effect_in_diagnostic_and_receipt(tmp_path_factory):
    def cancel(_):
        (tmp_path_factory / "partial.txt").write_text("retained", encoding="utf-8")
        raise ToolExecutionCancelled("cancelled after possible effect")
    tool = AgentTool(name="probe", description="probe", parameters={"type": "object"}, execute=cancel,
        effect=ToolEffect.WORKSPACE_WRITE)
    ctx = context(tmp_path_factory, ["probe"])
    result = ToolPipeline().execute({"name": "probe", "args": {}}, tool, ctx, "cancelled")
    assert result.diagnostics["code"] == "TOOL_CANCELLED"
    assert result.diagnostics["error_info"]["retryable"] is False
    assert result.diagnostics["error_info"]["effect_state"] == result.receipt["effect_state"] == "ambiguous"
    assert (tmp_path_factory / "partial.txt").read_text() == "retained"


def test_isolated_runner_timeout_flag_reaches_the_same_typed_diagnostic(tmp_path_factory, monkeypatch):
    from backend.core.loop.process_tool_runner import ProcessToolRunner, SubprocessResult
    monkeypatch.setattr(ProcessToolRunner, "run", lambda self, *args, **kwargs:
        SubprocessResult(success=False, timed_out=True, error="deadline ended"))
    tool = AgentTool(name="probe", description="probe", parameters={"type": "object"}, execute=lambda _: {},
        effect=ToolEffect.READ, isolated=True, runner_name="probe")
    ctx = context(tmp_path_factory, ["probe"])
    result = ToolPipeline().execute({"name": "probe", "args": {}}, tool, ctx, "isolated-timeout")
    assert result.diagnostics["code"] == "TOOL_TIMEOUT"
    assert result.diagnostics["error_info"]["catalog_id"] == "GITGO-E3810"
    assert result.diagnostics["error_info"]["effect_state"] == result.receipt["effect_state"] == "not_committed"
