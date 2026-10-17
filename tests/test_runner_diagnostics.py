"""Captured transport diagnostics reach ordinary model and trace results."""
import pytest
from backend.core.loop.agent_tool import AgentTool
from backend.core.loop.execution_contract import NATIVE_PROCESS
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.process_tool_runner import ProcessToolRunner, SubprocessResult
from backend.core.loop.runner_diagnostics import MAX_STDERR_CHARS, capture_runner_diagnostics
from backend.core.storage.redaction import redact_for_persistence


@pytest.mark.parametrize("case", ["success", "business", "crash", "timeout", "cancelled"])
def test_captured_stderr_survives_each_pipeline_result(tmp_path_factory, monkeypatch, case):
    result = SubprocessResult(success=True, data={"ok": True}, exit_code=0,
                              duration_ms=12, stderr="public diagnostic marker")
    if case == "business":
        result.data = {"error": "SANDBOX_EXECUTION_FAILED"}
        result.exit_code = 17
    elif case != "success":
        result.success = False
        result.error = "tool cancelled" if case == "cancelled" else "transport failure"
        result.timed_out = case == "timeout"
        result.effect_state = "ambiguous"
        result.stderr_partial = True
    monkeypatch.setattr(ProcessToolRunner, "run", lambda *args, **kwargs: result)
    process = AgentProcess(process_id="diagnostic", role="worker", ring_level=RingLevel.RING_0,
                           worktree_path=str(tmp_path_factory), session=AgentSession())
    ctx = ExecutionContext(process=process, session=process.session, workspace_path=str(tmp_path_factory))
    tool = AgentTool("exec_command", "test", {}, lambda args: pytest.fail("inline execution"),
                     execution_contract=NATIVE_PROCESS)
    events = []
    ctx.event_bus.subscribe("ToolResultReady", lambda event: events.append(event.data))
    actual = ToolPipeline().execute({"name": tool.name, "args": {}}, tool, ctx, "stderr-test")
    assert actual.is_error == (case != "success")
    assert "public diagnostic marker" in actual.formatted
    diag = actual.diagnostics["runner"]
    assert diag["stderr"] == "public diagnostic marker"
    assert diag["exit_code"] == result.exit_code and diag["timed_out"] == result.timed_out
    assert diag["stderr_partial"] == result.stderr_partial
    assert "effect_state" not in diag and "committed" not in diag
    if result.success:
        assert actual.data is result.data
    if case == "success":
        assert events[0]["diagnostics"]["runner"] == diag
        assert "public diagnostic marker" in events[0]["formatted"]


def test_captured_stderr_masks_tokens_before_truncating_and_strips_terminal_controls():
    key = "sk-" + "S" * 30
    github = "ghp_" + "G" * 30
    token = "Bearer " + "B" * 30
    text = "visible\n\x1b[31m" + key + "\x1b[0m " + github + " " + token
    text += "\x1b]0;unsafe-title\x07\x00\r " + "x" * MAX_STDERR_CHARS
    diag = capture_runner_diagnostics(SubprocessResult(True, stderr=text))
    assert key not in diag["stderr"] and github not in diag["stderr"] and token not in diag["stderr"]
    assert "unsafe-title" not in diag["stderr"] and "\x1b" not in diag["stderr"]
    assert "\x00" not in diag["stderr"] and "\r" not in diag["stderr"]
    assert diag["stderr"].startswith("visible\n") and "REDACTED" in diag["stderr"]
    assert len(diag["stderr"]) == MAX_STDERR_CHARS and diag["stderr_truncated"]


@pytest.mark.parametrize("prefix", ["ghp_", "github_pat_"])
def test_shared_live_and_storage_redaction_masks_github_tokens(prefix):
    token = prefix + "T" * 30
    assert token not in redact_for_persistence({"stderr": token})["stderr"]


@pytest.mark.parametrize("case", ["timeout", "malformed", "exit"])
def test_transport_failure_retains_already_captured_stderr(tmp_path_factory, runner_transport_only, monkeypatch, case):
    import sys
    source = "import sys,time;sys.stderr.write('captured-before-failure\\n');sys.stderr.flush();"
    source += "time.sleep(60)" if case == "timeout" else (
        "print('invalid-json')" if case == "malformed" else "sys.exit(29)"
    )
    monkeypatch.setattr("backend.core.loop.process_tool_runner.tool_runner_command", lambda: [sys.executable, "-c", source])
    result = ProcessToolRunner(timeout=2).run("exec_command", {"_workspace": str(tmp_path_factory)})
    assert "captured-before-failure" in result.stderr, result
    if case == "timeout":
        assert result.timed_out and result.stderr_partial
    elif case == "malformed":
        assert not result.success and result.stderr_partial
    else:
        assert result.exit_code == 29 and result.data["error"] == "SANDBOX_EXECUTION_FAILED"
