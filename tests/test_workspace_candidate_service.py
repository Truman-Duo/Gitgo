"""Execution-view semantics; mock producers and real native evidence are separate."""
import json
from pathlib import Path

import pytest

from tests.test_file_feedback_contract import context
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.tools.catalog import build_workspace_tools
from backend.core.tools.workspace_projection import capture, project
from backend.core.knowledge.manager import LessonManager
from backend.core.knowledge.models import Lesson
from backend.core.knowledge.feedback import observe_provider_input


def make(ws, name="exec_command"):
    ctx = context(ws, [name])
    ctx.process.update_context_snapshot(lambda s: {**s, "project_name": "p"})
    tool = build_workspace_tools(ws)[name]
    pipeline = ToolPipeline()
    def call(args, index=0):
        return pipeline.execute({"name": name, "args": args}, tool, ctx, "workspace-" + str(index))
    return ctx, call


def fake_producer(monkeypatch, ws, change=None, *, output=None):
    calls = []
    def run(self, name, args, timeout, **kwargs):
        stage = Path(args["_workspace"])
        assert stage != ws and not stage.is_relative_to(ws)
        assert not (stage / ".git").exists() and not (stage / ".gitgo").exists()
        calls.append(stage)
        if change:
            change(stage)
        kwargs["runtime_metadata"].update(launch_state="started", exit_code=0, cleanup={"complete": True, "mode": "test_fixture"})
        return output or {"stdout": "completed", "exit_code": 0, "success": True}
    monkeypatch.setattr(ToolPipeline, "_execute_isolated", run)
    monkeypatch.setattr("backend.core.sandbox_access.WorkspaceAccess.prepare", lambda self: self)
    monkeypatch.setattr("backend.core.sandbox_access.WorkspaceAccess.close", lambda self: None)
    return calls


def test_plain_command_writes_only_private_view_until_file_service(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    (ws / "old.txt").write_bytes(b"old\r\n")
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("private metadata")
    def change(stage):
        assert (stage / "old.txt").read_bytes() == b"old\r\n"
        (stage / "old.txt").write_bytes(b"new\r\n")
        (stage / "new.bin").write_bytes(b"\0binary")
        assert (ws / "old.txt").read_bytes() == b"old\r\n"
        assert not (ws / "new.bin").exists()
    calls = fake_producer(monkeypatch, ws, change)
    ctx, call = make(ws)
    result = call({"argv": ["python", "task.py"]})
    assert not result.is_error, result.formatted
    assert result.receipt["effect_state"] == "committed"
    assert {r["path"] for r in result.receipt["file_mutations"]} == {"old.txt", "new.bin"}
    assert result.receipt["file_service_result"]
    assert (ws / "old.txt").read_bytes() == b"new\r\n" and (ws / "new.bin").read_bytes() == b"\0binary"
    assert len(calls) == 1 and not calls[0].exists()
    assert "launch_state" not in result.receipt
    assert result.receipt["runtime_cleanup"]["complete"]


def test_command_advice_freezes_output_across_model_roundtrip_and_storage_reopen(tmp_path_factory, monkeypatch):
    from backend.core.storage import close_owned_storage
    ws = tmp_path_factory
    LessonManager.save(ws, Lesson(id="advice", project_name="p", trigger="report.txt", rule="Verify report sources"))
    calls = fake_producer(monkeypatch, ws, lambda stage: (stage / "report.txt").write_text("report"))
    ctx, call = make(ws)
    args = {"argv": ["python", "build.py"]}
    first = call(args)
    assert first.data["error"] == "LESSON_FEEDBACK_REQUIRED", first.formatted
    assert first.receipt["effect_state"] == "not_committed" and not (ws / "report.txt").exists()
    resumed = {**args, "candidate_ref": first.data["candidate_ref"], "lesson_feedback_id": first.data["lesson_feedback"]["feedback_id"]}
    assert call(resumed, 1).is_error and len(calls) == 1
    ctx.session.append_tool_result(first.formatted, tool_name="exec_command", tool_call_id="c0", data=first.data, receipt=first.receipt)
    observe_provider_input(ctx.process, [{"role": "tool", "content": first.formatted}])
    close_owned_storage()
    final = call(resumed, 2)
    assert not final.is_error, final.formatted
    assert final.data["candidate_source"] == "frozen" and len(calls) == 1
    assert (ws / "report.txt").read_text() == "report"
    assert "runtime_cleanup" not in final.receipt and "launch_state" not in final.receipt


def test_mandatory_output_check_blocks_every_file(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    LessonManager.save(ws, Lesson(id="required", project_name="p", trigger="retired", rule="Avoid retired API",
        dangerous_tools=["write_file"], check={"authority": "registered", "mode": "violation_pattern", "paths": ["bad.py"], "pattern": "retired"}))
    def change(stage):
        (stage / "bad.py").write_text("retired()")
        (stage / "good.txt").write_text("good")
    fake_producer(monkeypatch, ws, change)
    ctx, call = make(ws)
    result = call({"argv": ["python", "generate.py"]})
    assert result.data["error"] == "KNOWN_LESSON_VIOLATION", result.formatted
    assert not (ws / "bad.py").exists() and not (ws / "good.txt").exists()
    assert result.receipt["effect_state"] == "not_committed"


def test_changed_nonoutput_input_blocks_formal_commit(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    (ws / "input.txt").write_text("before")
    def change(stage):
        (stage / "output.txt").write_text("derived from before")
        (ws / "input.txt").write_text("external update")
    fake_producer(monkeypatch, ws, change)
    ctx, call = make(ws)
    result = call({"argv": ["python", "derive.py"]})
    assert result.data["error"] == "FILE_CHANGED", result.formatted
    assert not (ws / "output.txt").exists() and (ws / "input.txt").read_text() == "external update"


def test_child_data_cannot_manufacture_file_service_facts(tmp_path_factory, monkeypatch):
    fake_producer(monkeypatch, tmp_path_factory, output={"exit_code": 0, "success": True,
        "effect_state": "committed", "file_results": [{"path": "fabricated.txt", "effect_state": "committed"}],
        "lesson_feedback": {"mandatory_check_state": "passed"}, "candidate_ref": "fake"})
    ctx, call = make(tmp_path_factory)
    result = call({"argv": ["python", "task.py"]})
    assert not result.is_error
    assert result.receipt["effect_state"] == "not_committed" and result.receipt["file_mutations"] == []
    assert "lesson_mandatory_check_state" not in result.receipt
    assert "fabricated.txt" not in result.receipt.get("files", [])


def test_failed_command_retains_output_but_does_not_publish_partial_files(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    calls = fake_producer(monkeypatch, ws, lambda stage: (stage / "partial.txt").write_text("partial"),
        output={"success": False, "exit_code": 2, "stdout": "useful diagnostic"})
    ctx, call = make(ws)
    result = call({"argv": ["python", "broken.py"]})
    assert result.is_error and result.data["producer_failed"] and result.data["stdout"] == "useful diagnostic"
    assert result.receipt["effect_state"] == "not_committed" and not (ws / "partial.txt").exists()
    assert len(calls) == 1


def test_stale_candidate_cannot_silently_regenerate(tmp_path_factory, monkeypatch):
    calls = fake_producer(monkeypatch, tmp_path_factory)
    ctx, call = make(tmp_path_factory)
    result = call({"argv": ["python", "task.py"], "candidate_ref": "missing"})
    assert result.data["error"] == "FILE_CANDIDATE_STALE" and not calls


def test_unknown_cleanup_retains_private_view_and_blocks_followup(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    stages = []
    def run(self, name, args, timeout, **kwargs):
        stages.append(Path(args["_workspace"]))
        (stages[-1] / "output.txt").write_text("private")
        kwargs["runtime_metadata"].update(launch_state="started", exit_code=0, cleanup={"complete": False, "mode": "test_unknown"})
        return {"success": True}
    monkeypatch.setattr(ToolPipeline, "_execute_isolated", run)
    monkeypatch.setattr("backend.core.sandbox_access.WorkspaceAccess.prepare", lambda self: self)
    monkeypatch.setattr("backend.core.sandbox_access.WorkspaceAccess.close", lambda self: pytest.fail("unretired view lost access"))
    ctx, call = make(ws)
    result = call({"argv": ["python", "task.py"]})
    assert result.is_error and not (ws / "output.txt").exists()
    assert not result.receipt["runtime_cleanup"]["complete"] and stages[0].exists()
    follow = call({"argv": ["python", "other.py"]}, 1)
    assert follow.is_error and len(stages) == 1


def test_view_capacity_rejected_before_native_launch(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    (ws / "one").write_text("one")
    (ws / "two").write_text("two")
    monkeypatch.setattr("backend.core.tools.workspace_projection.MAX_FILES", 1)
    calls = fake_producer(monkeypatch, ws)
    ctx, call = make(ws)
    result = call({"argv": ["python", "task.py"]})
    assert result.data["error"] == "WORKSPACE_CANDIDATE_UNAVAILABLE" and not calls


def test_directory_only_output_is_not_falsely_reported_as_committed(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    fake_producer(monkeypatch, ws, lambda stage: (stage / "empty").mkdir())
    ctx, call = make(ws)
    result = call({"argv": ["python", "task.py"]})
    assert result.data["error"] == "WORKSPACE_CANDIDATE_UNAVAILABLE" and not (ws / "empty").exists()


from tests.test_alpha3_native_source import source_runtime
from tests.test_native_sandbox import isolated_python


from tests.native_source_runtime import workspace_native_launcher


def test_real_native_command_can_write_view_but_not_formal_workspace(tmp_path_factory, workspace_native_launcher):
    ws = tmp_path_factory
    (ws / "input.txt").write_bytes(b"raw-input\r\n")
    original = (ws / "input.txt").read_bytes()
    code = ("from pathlib import Path; "
            "assert Path('input.txt').read_bytes()==" + repr(original) + "; "
            "Path('result.txt').write_bytes(b'computed-output'); "
            "exec(" + repr("try:\n Path(" + repr(str(ws / "input.txt")) + ").write_bytes(b'forbidden')\nexcept PermissionError:\n print('formal-denied')\nelse:\n raise RuntimeError('formal write unexpectedly allowed')") + ")")
    ctx, call = make(ws)
    result = call({"argv": ["python", "-c", code], "timeout": 60})
    assert not result.is_error, result.formatted
    assert result.data["stdout"].strip() == "formal-denied"
    assert result.receipt["effect_state"] == "committed"
    assert (ws / "input.txt").read_bytes() == original
    assert (ws / "result.txt").read_bytes() == b"computed-output"
    assert result.data["candidate_render"]["launch_state"] == "started"
    assert result.data["candidate_render"]["access_retired"] is True
    assert result.receipt["runtime_cleanup"]["complete"] is True


def test_real_native_powershell_output_uses_same_checked_commit(tmp_path_factory, workspace_native_launcher):
    from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
    ws = tmp_path_factory
    ctx, call = make(ws, "shell_script")
    args = {"script": "Set-Content -LiteralPath shell.txt -Value native-shell -NoNewline -ErrorAction Stop; [Console]::WriteLine('done')",
            "purpose": "verify private PowerShell workspace candidate", "shell": "powershell", "timeout": 90}
    tools = build_workspace_tools(ws)
    create_permission_request(ctx.process, {"purpose": "Run this exact test script", "tool_name": "shell_script", "arguments": args}, tools, str(ws))
    grant_from_decision(ctx.process, ctx.process.pending_decision, "allow_once")
    result = call(args)
    assert not result.is_error and not result.suspended, json.dumps({"formatted":result.formatted,"data":result.data,"receipt":result.receipt,"grants":ctx.process.approval_grants},default=str)
    assert result.receipt["effect_state"] == "committed" and (ws / "shell.txt").read_text() == "native-shell", json.dumps({"data":result.data,"receipt":result.receipt},default=str)
    assert result.data["candidate_render"]["access_retired"]
    assert result.receipt["runtime_cleanup"]["complete"]


@pytest.mark.parametrize("kind", ["cancel", "task", "tool"])
def test_rebound_or_cancelled_producer_cannot_publish_even_an_empty_result(tmp_path_factory, monkeypatch, kind):
    ws = tmp_path_factory
    ctx, call = make(ws)
    def change(stage):
        if kind == "cancel":
            ctx.cancel()
        elif kind == "task":
            ctx.process.active_task_id = "different-task"
        else:
            from backend.core.loop.workspace_candidate_runtime import WorkspaceCandidateRuntime
            monkeypatch.setattr(WorkspaceCandidateRuntime, "is_current", lambda self: False)
    fake_producer(monkeypatch, ws, change)
    result = call({"argv": ["python", "empty.py"]})
    assert result.is_error and result.receipt["effect_state"] == "not_committed"
    assert result.data["error"] in {"TOOL_CANCELLED", "FILE_CANDIDATE_STALE"}
    assert not result.receipt["command_completed"]


def test_cleanup_complete_with_issues_is_still_unconfirmed(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    def run(self, name, args, timeout, **kwargs):
        kwargs["runtime_metadata"].update(launch_state="started", exit_code=0,
            cleanup={"complete": True, "mode": "fixture", "issues": ["job_close_failed"]})
        (Path(args["_workspace"]) / "private.txt").write_text("private")
        return {"success": True, "exit_code": 0}
    monkeypatch.setattr(ToolPipeline, "_execute_isolated", run)
    monkeypatch.setattr("backend.core.sandbox_access.WorkspaceAccess.prepare", lambda self: self)
    monkeypatch.setattr("backend.core.sandbox_access.WorkspaceAccess.close", lambda self: pytest.fail("must keep access for an unretired child"))
    ctx, call = make(ws)
    result = call({"argv": ["python", "task.py"]})
    assert result.data["error"] == "SANDBOX_CLEANUP_PENDING"
    assert not result.receipt["runtime_cleanup"]["complete"]
    assert not (ws / "private.txt").exists()


def test_existing_file_writer_lock_also_protects_nonoutput_execution_inputs(tmp_path_factory, monkeypatch):
    from backend.core.tools.file_mutation_batch import file_writer_locks
    from backend.core.storage import get_storage
    ws = tmp_path_factory
    (ws / "input.txt").write_text("input")
    fake_producer(monkeypatch, ws, lambda stage: (stage / "output.txt").write_text("derived"))
    ctx, call = make(ws)
    storage = get_storage(ws)
    with file_writer_locks(ws, [ws / "input.txt"], {"_mutation_lock_root":str(storage.paths.state_home / "mutation-locks")}):
        result = call({"argv": ["python", "task.py"]})
    assert result.data["error"] == "FILE_MUTATION_BUSY", result.formatted
    assert not (ws / "output.txt").exists()


def test_candidate_bound_includes_nonoutput_source_versions_after_advice(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    (ws / "input.txt").write_text("input")
    LessonManager.save(ws, Lesson(id="advice", project_name="p", trigger="output.txt", rule="Verify output"))
    calls = fake_producer(monkeypatch, ws, lambda stage: (stage / "output.txt").write_text("derived"))
    ctx, call = make(ws)
    args = {"argv": ["python", "task.py"]}
    first = call(args)
    ctx.session.append_tool_result(first.formatted, tool_name="exec_command", tool_call_id="c0", data=first.data, receipt=first.receipt)
    observe_provider_input(ctx.process, [{"role":"tool", "content":first.formatted}])
    assert first.receipt["producer_completed"] and not first.receipt["command_completed"]
    assert first.receipt["output_publication"] == "withheld"
    (ws / "input.txt").write_text("changed")
    result = call({**args, "candidate_ref":first.data["candidate_ref"], "lesson_feedback_id":first.data["lesson_feedback"]["feedback_id"]},1)
    assert result.data["error"] == "FILE_CHANGED" and len(calls) == 1
    assert result.receipt["input_applicability"] == "changed" and not (ws / "output.txt").exists()


def test_red_command_without_private_changes_is_observed_but_never_a_formal_write(tmp_path_factory, monkeypatch):
    fake_producer(monkeypatch, tmp_path_factory, output={"success":False,"exit_code":1,"stderr":"red assertion"})
    ctx, call = make(tmp_path_factory)
    result = call({"argv":["python","regression.py"]})
    assert result.is_error and result.receipt["producer_completed"] and result.receipt["command_completed"]
    assert result.receipt["command_exit_code"] == 1 and result.receipt["effect_state"] == "not_committed"
    assert result.receipt["output_publication"] == "not_required"


def test_candidate_capacity_and_protected_outputs_cannot_be_partly_published(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    monkeypatch.setattr("backend.core.tools.workspace_projection.MAX_CANDIDATE_BYTES", 4)
    fake_producer(monkeypatch, ws, lambda stage:(stage/"large.txt").write_bytes(b"larger"))
    ctx, call = make(ws)
    result = call({"argv":["python","task.py"]})
    assert result.data["error"] == "WORKSPACE_CANDIDATE_UNAVAILABLE" and not (ws/"large.txt").exists()


def test_partial_file_service_failure_never_reexecutes_the_native_producer(tmp_path_factory, monkeypatch):
    from backend.core.tools import file_mutation_batch
    ws = tmp_path_factory
    def change(stage):
        (stage/"a.txt").write_text("a")
        (stage/"b.txt").write_text("b")
    calls = fake_producer(monkeypatch, ws, change)
    original = file_mutation_batch.commit_checked_file
    def commit(candidate, feedback):
        if candidate.path.name == "b.txt":
            return {"error":"FILE_MUTATION_FAILED","effect_state":"ambiguous"}
        return original(candidate,feedback)
    monkeypatch.setattr(file_mutation_batch,"commit_checked_file",commit)
    ctx,call=make(ws)
    args={"argv":["python","task.py"]}
    first=call(args)
    assert first.is_error and first.receipt["effect_state"] == "ambiguous"
    assert (ws/"a.txt").exists() and not (ws/"b.txt").exists()
    result=call({**args,"candidate_ref":first.data["candidate_ref"]},1)
    assert result.is_error and len(calls)==1 and not (ws/"b.txt").exists()


@pytest.mark.parametrize("failure", ["timeout", "protocol"])
def test_started_wrapper_failure_never_claims_command_did_not_start(tmp_path_factory, monkeypatch, failure):
    def run(self,name,args,timeout,**kwargs):
        kwargs["runtime_metadata"].update(launch_state="started",exit_code=-1,cleanup={"complete":True,"mode":"fixture"})
        if failure == "timeout":
            raise TimeoutError("fixture timeout")
        raise RuntimeError("fixture lost protocol")
    monkeypatch.setattr(ToolPipeline,"_execute_isolated",run)
    monkeypatch.setattr("backend.core.sandbox_access.WorkspaceAccess.prepare",lambda self:self)
    monkeypatch.setattr("backend.core.sandbox_access.WorkspaceAccess.close",lambda self:None)
    ctx,call=make(tmp_path_factory)
    result=call({"argv":["python","task.py"]})
    assert result.is_error and result.receipt["effect_state"] == "not_committed"
    assert result.receipt["producer_launch_state"] == "started"
    assert result.receipt["producer_completed"] is None and not result.receipt["command_completed"]
    assert result.receipt["execution_state"] != "not_started"
    assert result.data["error_info"]["details"]["producer_launch_state"] == "started"
    assert result.receipt["runtime_cleanup"]["complete"]


def test_real_native_command_with_deep_state_location(tmp_path_factory, workspace_native_launcher, monkeypatch):
    # The original project-root/candidate_workspaces layout placed private
    # TEMP beyond MAX_PATH and blocked nested Windows CreateProcess.
    from backend.core.storage import close_owned_storage
    ws = tmp_path_factory
    state = ws.parent / ("deep-state-" + "s" * 45) / ("t" * 30)
    close_owned_storage()
    monkeypatch.setenv("GITGO_STATE_HOME", str(state))
    try:
        ctx, call = make(ws)
        result = call({"argv": ["python", "-c", "from pathlib import Path; Path('deep.txt').write_text('done')"], "timeout": 15})
        assert not result.is_error, result.formatted
        assert (ws / "deep.txt").read_text() == "done"
        assert result.receipt["runtime_cleanup"]["complete"]
    finally:
        close_owned_storage()
        # Only this fixture's state; no product data outside it is removed.
        import shutil
        shutil.rmtree(state.parent, ignore_errors=False)
