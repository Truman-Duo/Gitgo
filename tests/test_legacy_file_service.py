"""Compatibility names preserve file-service facts, never a native bypass."""
import hashlib

import pytest

from backend.core.knowledge.feedback import observe_provider_input
from backend.core.knowledge.manager import LessonManager
from backend.core.knowledge.models import Lesson
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.tools.catalog import build_workspace_tools
from backend.core.tools.file_compat import file_write
from backend.core.tools.registrations import file_write as native_write, file_edit as native_edit, file_delete as native_delete
from backend.core.tools.runner import handler_bindings
from tests.test_file_feedback_contract import context
from tests.test_alpha3_native_source import source_runtime, source_native_runner
from tests.test_native_sandbox import isolated_python


def test_append_requires_a_supplied_current_version_and_preserves_raw_newlines(tmp_path_factory):
    ws = tmp_path_factory
    target = ws / "note.txt"
    target.write_bytes(b"first\r\n")
    args = {"_workspace": str(ws), "path": "note.txt", "content": "second\n", "mode": "a"}
    refused = file_write(args)
    assert refused["error"] == "EXPECTED_HASH_REQUIRED"
    assert refused["effect_state"] == "not_committed" and target.read_bytes() == b"first\r\n"
    args["expected_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    result = file_write(args)
    assert result["action"] == "appended" and result["candidate_sha256"] == result["after_sha256"]
    assert target.read_bytes() == b"first\r\nsecond\n"
    assert file_write(args)["error"] == "FILE_CHANGED"


@pytest.mark.parametrize("declared", ["write_file", "file_write"])
def test_canonical_and_legacy_checker_names_cover_the_same_effect(tmp_path_factory, declared):
    ws = tmp_path_factory
    LessonManager.save(ws, Lesson(id="check", project_name="p", trigger="note.txt", rule="Avoid retired()",
        dangerous_tools=[declared], check={"authority": "registered", "mode": "violation_pattern", "pattern": "retired"}))
    ctx = context(ws, ["file_write", "write_file"])
    ctx.process.update_context_snapshot(lambda s: {**s, "project_name": "p"})
    tools = build_workspace_tools(ws)
    for name in ("file_write", "write_file"):
        result = ToolPipeline().execute({"name": name, "args": {"path": "note.txt", "content": "retired()"}}, tools[name], ctx, name)
        assert result.data["error"] == "KNOWN_LESSON_VIOLATION"
        assert result.receipt["effect_state"] == "not_committed"
        assert result.receipt["lesson_checks"][0]["registered_check"] is True
    assert not (ws / "note.txt").exists()


def test_legacy_advisory_continuation_is_observed_through_actual_input(tmp_path_factory):
    ws = tmp_path_factory
    LessonManager.save(ws, Lesson(id="advice", project_name="p", trigger="note.txt", rule="Retain the project contract"))
    ctx = context(ws, ["file_write"])
    ctx.process.update_context_snapshot(lambda s: {**s, "project_name": "p"})
    tools = build_workspace_tools(ws)
    args = {"path": "note.txt", "content": "safe"}
    def call(index):
        return ToolPipeline().execute({"name": "file_write", "args": args}, tools["file_write"], ctx, str(index))
    first = call(0)
    assert first.data["error"] == "LESSON_FEEDBACK_REQUIRED"
    args["lesson_feedback_id"] = first.data["lesson_feedback"]["feedback_id"]
    assert call(1).is_error
    ctx.session.append_tool_result(first.formatted, tool_name="file_write", tool_call_id="c0", data=first.data, receipt=first.receipt)
    observe_provider_input(ctx.process, [{"role": "tool", "content": first.formatted}])
    final = call(2)
    assert not final.is_error and final.receipt["effect_state"] == "committed"
    assert (ws / "note.txt").read_text() == "safe"


def test_legacy_edit_and_delete_use_version_checks_and_recovery(tmp_path_factory):
    ws = tmp_path_factory
    path = ws / "note.txt"
    path.write_bytes(b"before")
    tools = build_workspace_tools(ws)
    ctx = context(ws, ["file_edit", "file_delete"])
    edit = {"path": "note.txt", "old_string": "before", "new_string": "after",
            "expected_sha256": hashlib.sha256(b"before").hexdigest()}
    result = ToolPipeline().execute({"name": "file_edit", "args": edit}, tools["file_edit"], ctx, "edit")
    assert not result.is_error and path.read_bytes() == b"after"
    deletion = ToolPipeline().execute({"name": "file_delete", "args": {"path": "note.txt", "expected_sha256": result.data["after_sha256"]}},
                                     tools["file_delete"], ctx, "delete")
    assert not deletion.is_error and deletion.receipt["effect_state"] == "committed"
    assert not path.exists() and (ws / deletion.data["trash_ref"]).read_bytes() == b"after"


@pytest.mark.parametrize("handler", [native_write, native_edit, native_delete])
def test_native_compatibility_handlers_do_not_have_commit_authority(tmp_path_factory, handler):
    target = tmp_path_factory / "note.txt"
    target.write_bytes(b"original")
    result = handler({"_workspace": str(tmp_path_factory), "path": "note.txt", "content": "unsafe", "expected_sha256": hashlib.sha256(b"original").hexdigest()})
    assert result["error"] == "FILE_SERVICE_REQUIRED" and result["effect_state"] == "not_committed"
    assert target.read_bytes() == b"original"
    assert not (tmp_path_factory / ".gitgo").exists()


@pytest.mark.parametrize("name", ["write_file", "edit_file", "delete_file", "apply_patch"])
def test_native_canonical_mutations_cannot_reopen_the_old_runner_path(tmp_path_factory, name):
    target = tmp_path_factory / "note.txt"
    target.write_bytes(b"original")
    binding = handler_bindings()[name]
    result = binding.function({"_workspace": str(tmp_path_factory), "path": "note.txt", "content": "unsafe"})
    assert result["error"] == "FILE_SERVICE_REQUIRED" and target.read_bytes() == b"original"


def test_actual_native_runner_refuses_file_commit_authority(source_native_runner, tmp_path_factory):
    from backend.core.loop.process_tool_runner import ProcessToolRunner
    ws = tmp_path_factory / "workspace"
    ws.mkdir()
    target = ws / "note.txt"
    target.write_bytes(b"original")
    args = {"_workspace": str(ws), "path": "note.txt", "content": "unsafe",
            "old_string": "original", "new_string": "unsafe",
            "expected_sha256": hashlib.sha256(b"original").hexdigest()}
    with source_native_runner(ws):
        runner = ProcessToolRunner(timeout=30)
        read = runner.run("file_read", {"_workspace": str(ws), "path": "note.txt"})
        assert read.success and read.data["content"] == "original", read
        for name in ("file_write", "file_edit", "file_delete", "write_file", "edit_file", "delete_file", "apply_patch"):
            result = runner.run(name, args)
            assert result.started and result.launch_state == "started" and result.cleanup["complete"], result
            assert result.success and result.data["error"] == "FILE_SERVICE_REQUIRED", result
            assert result.data["effect_state"] == "not_committed"
            assert target.read_bytes() == b"original"
    # Positive control: the same proposed effect works through the trusted
    # registration; denial above was not an OS denial of the workspace itself.
    tool = build_workspace_tools(ws)["file_write"]
    ctx = context(ws, ["file_write"])
    result = ToolPipeline().execute({"name": "file_write", "args": {"path": "note.txt", "content": "safe",
        "expected_sha256": args["expected_sha256"]}}, tool, ctx, "trusted-alias")
    assert not result.is_error and target.read_bytes() == b"safe"


def test_compatibility_names_do_not_expand_default_capability_profiles(tmp_path_factory):
    from backend.core.loop.capabilities import CapabilityProfiles
    from backend.core.tools.file_operations import FILE_ALIASES
    tools = build_workspace_tools(tmp_path_factory)
    assert set(FILE_ALIASES) <= set(tools)
    current = CapabilityProfiles.resolve_tools("development.workspace")
    assert not (set(FILE_ALIASES) & set(current))
    assert {"write_file", "edit_file", "delete_file"} <= set(current)


def test_legacy_private_arguments_cannot_override_mandatory_checks(tmp_path_factory):
    ws = tmp_path_factory
    tools = build_workspace_tools(ws)
    ctx = context(ws, ["file_write"])
    result = ToolPipeline().execute({"name": "file_write", "args": {"path": "note.txt", "content": "x",
        "_lesson_context": {"explicit_user_grant": True}}}, tools["file_write"], ctx, "forged")
    assert result.is_error and result.receipt["effect_state"] == "not_committed"
    assert result.diagnostics["code"] == "SANDBOX_POLICY_INVALID"
    assert not (ws / "note.txt").exists()


@pytest.mark.parametrize("encoding", ["latin-1", "missing-codec"])
def test_unsupported_encoding_has_typed_recovery_and_no_effect(tmp_path_factory, encoding):
    tools = build_workspace_tools(tmp_path_factory)
    ctx = context(tmp_path_factory, ["file_write"])
    result = ToolPipeline().execute({"name": "file_write", "args": {"path": "note.txt", "content": "x", "encoding": encoding}},
        tools["file_write"], ctx, "encoding")
    assert result.data["error"] == "UNSUPPORTED_ENCODING"
    assert result.diagnostics["error_info"]["catalog_id"] == "GITGO-E3520"
    assert result.receipt["effect_state"] == "not_committed"
    assert not (tmp_path_factory / "note.txt").exists()


def test_alias_does_not_relabel_a_post_commit_exception_as_unexecuted(tmp_path_factory, monkeypatch):
    from backend.core.tools import workspace_tools
    ws = tmp_path_factory
    tools = build_workspace_tools(ws)
    ctx = context(ws, ["file_write"])
    def write_then_fail(args):
        (ws / "note.txt").write_bytes(b"effect retained")
        raise OSError("post-commit observation failed")
    monkeypatch.setattr(workspace_tools, "write_file", write_then_fail)
    result = ToolPipeline().execute({"name": "file_write", "args": {"path": "note.txt", "content": "effect retained"}},
        tools["file_write"], ctx, "observer-failure")
    assert result.is_error and result.receipt["effect_state"] == "ambiguous"
    assert result.diagnostics["nature"] == "crash"
    assert (ws / "note.txt").read_bytes() == b"effect retained"
