"""Candidate/cache fault tests plus real native Office/PDF production."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from tests.test_alpha3_native_source import source_runtime
from tests.test_native_sandbox import isolated_python
from tests.test_file_feedback_contract import context
from backend.core.tools.catalog import build_workspace_tools
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.candidate_workspace import candidate_workspace_root
from backend.core.knowledge.manager import LessonManager
from backend.core.knowledge.models import Lesson
from backend.core.knowledge.feedback import observe_provider_input


@pytest.fixture(scope="module")
def document_native_runtime(source_runtime):
    for name in ("docx", "pptx", "openpyxl", "et_xmlfile", "lxml", "PIL", "reportlab", "charset_normalizer", "xlsxwriter"):
        spec = importlib.util.find_spec(name)
        if spec is None:
            continue
        if spec.submodule_search_locations:
            original = Path(next(iter(spec.submodule_search_locations)))
            shutil.copytree(original, source_runtime / name, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for sibling in original.parent.glob(name + ".libs"):
                shutil.copytree(sibling, source_runtime / sibling.name, dirs_exist_ok=True)
        elif spec.origin:
            shutil.copy2(spec.origin, source_runtime / Path(spec.origin).name)
    return source_runtime


@pytest.fixture
def document_native_launcher(document_native_runtime, monkeypatch):
    from backend.core.child_process import tool_runner_command
    command = tool_runner_command(document_native_runtime)
    command[0] = str(document_native_runtime / "python.exe")
    monkeypatch.setattr("backend.core.loop.process_tool_runner.tool_runner_command", lambda: command)
    monkeypatch.setattr("backend.core.loop.process_tool_runner.owned_child_cwd", lambda _: document_native_runtime)
    monkeypatch.setattr("backend.core.sandbox_access.native_runtime_access", lambda: ([document_native_runtime], []))
    return document_native_runtime


def make(ws):
    ctx = context(ws, ["document_create"])
    ctx.process.update_context_snapshot(lambda s: {**s, "project_name": "p"})
    tool = build_workspace_tools(ws)["document_create"]
    def call(args, index=0):
        return ToolPipeline().execute({"name": "document_create", "args": args}, tool, ctx, f"doc-{index}")
    return ctx, call


def fake_producer(monkeypatch, payload=b"PK synthetic immutable binary"):
    calls = []
    def render(runtime, spec, suffix):
        calls.append((spec, suffix))
        runtime.last_render = {"producer": "unit_fixture", "cleanup": {"complete": True}, "access_retired": True}
        return payload
    monkeypatch.setattr("backend.core.loop.document_candidate_runtime.DocumentCandidateRuntime.render", render)
    return calls


def test_binary_advice_uses_one_frozen_candidate_across_model_roundtrip(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    calls = fake_producer(monkeypatch)
    LessonManager.save(ws, Lesson(id="advice", project_name="p", trigger="report.docx", rule="Check the report source"))
    ctx, call = make(ws)
    args = {"path": "report.docx", "content": "report"}
    first = call(args)
    assert first.data["error"] == "LESSON_FEEDBACK_REQUIRED" and not (ws / "report.docx").exists()
    reference = first.data["lesson_feedback"]["feedback_id"]
    args["lesson_feedback_id"] = reference
    assert call(args, 1).is_error
    assert len(calls) == 1
    ctx.session.append_tool_result(first.formatted, tool_name="document_create", tool_call_id="c0", data=first.data, receipt=first.receipt)
    observe_provider_input(ctx.process, [{"role": "tool", "content": first.formatted}])
    result = call(args, 2)
    assert not result.is_error and result.receipt["effect_state"] == "committed"
    assert result.data["candidate_source"] == "frozen" and len(calls) == 1
    assert result.data["candidate_ref"] == first.data["candidate_ref"]
    assert (ws / "report.docx").read_bytes() == b"PK synthetic immutable binary"
    from backend.core.storage import get_storage
    assert get_storage(ws).list_state_refs("document_candidates.v1", prefix="") == []


def test_uninspectable_binary_mandatory_check_cannot_be_overridden(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    fake_producer(monkeypatch)
    LessonManager.save(ws, Lesson(id="required", project_name="p", trigger="report.docx", rule="Required content check",
        dangerous_tools=["write_file"], check={"authority": "registered", "mode": "violation_pattern", "paths": ["report.docx"], "pattern": "retired"}))
    ctx, call = make(ws)
    result = call({"path": "report.docx", "content": "apparently safe"})
    assert result.data["error"] == "LESSON_CHECK_UNAVAILABLE"
    assert result.receipt["effect_state"] == "not_committed" and not (ws / "report.docx").exists()
    assert result.receipt["lesson_mandatory_check_state"] == "failed"


def test_renamed_specification_cannot_reuse_a_stale_binary_feedback_reference(tmp_path_factory, monkeypatch):
    calls = fake_producer(monkeypatch)
    LessonManager.save(tmp_path_factory, Lesson(id="advice", project_name="p", trigger="report.docx", rule="Check sources"))
    ctx, call = make(tmp_path_factory)
    first = call({"path": "report.docx", "content": "one"})
    changed = call({"path": "report.docx", "content": "two", "lesson_feedback_id": first.data["lesson_feedback"]["feedback_id"]}, 1)
    assert changed.data["error"] == "FILE_CANDIDATE_STALE" and len(calls) == 1
    assert not (tmp_path_factory / "report.docx").exists()


def test_task_rebinding_during_generation_cannot_commit(tmp_path_factory, monkeypatch):
    ctx, call = make(tmp_path_factory)
    def render(runtime, spec, suffix):
        ctx.process.active_task_id = "replacement-task"
        runtime.last_render = {"cleanup": {"complete": True}, "access_retired": True}
        return b"binary"
    monkeypatch.setattr("backend.core.loop.document_candidate_runtime.DocumentCandidateRuntime.render", render)
    result = call({"path": "report.docx"})
    assert result.data["error"] == "FILE_CANDIDATE_STALE"
    assert result.receipt["effect_state"] == "not_committed"
    assert not (tmp_path_factory / "report.docx").exists()


def test_state_reference_release_is_compare_and_swap(tmp_path_factory):
    from backend.core.storage import get_storage
    storage = get_storage(tmp_path_factory)
    first, second = storage.put_blob(b"one"), storage.put_blob(b"two")
    storage.put_state_ref("candidate-test", "head", second)
    assert not storage.release_state_ref("candidate-test", "head", expected_ref=first)
    assert storage.get_state_ref("candidate-test", "head") == second
    assert storage.release_state_ref("candidate-test", "head", expected_ref=second)
    assert storage.get_state_ref("candidate-test", "head") is None


def test_real_office_candidate_is_reused_after_advice_and_storage_reopen(tmp_path_factory, document_native_launcher, monkeypatch):
    import base64
    from io import BytesIO
    from docx import Document
    from backend.core.storage import get_storage, close_owned_storage
    ws = tmp_path_factory
    LessonManager.save(ws, Lesson(id="source", project_name="p", trigger="report.docx", rule="Verify report sources"))
    ctx, call = make(ws)
    args = {"path": "report.docx", "content": "A real native Office candidate"}
    first = call(args)
    assert first.data["error"] == "LESSON_FEEDBACK_REQUIRED", first.formatted
    assert first.data["candidate_render"]["launch_state"] == "started"
    assert first.data["candidate_render"]["cleanup"]["complete"] and first.data["candidate_render"]["access_retired"]
    assert first.receipt["runtime_cleanup"]["complete"] is True
    assert "launch_state" not in first.receipt
    raw = base64.b64decode(json.loads(get_storage(ws).read_blob(first.data["candidate_ref"]))["bytes"])
    paragraphs = [p.text for p in Document(BytesIO(raw)).paragraphs]
    assert paragraphs[0] == "report"
    assert "A real native Office candidate" in "\n".join(paragraphs)
    assert not (ws / "report.docx").exists()
    assert not list((candidate_workspace_root(get_storage(ws).paths)).iterdir())
    ctx.session.append_tool_result(first.formatted, tool_name="document_create", tool_call_id="c0", data=first.data, receipt=first.receipt)
    observe_provider_input(ctx.process, [{"role": "tool", "content": first.formatted}])
    close_owned_storage()
    def must_not_render(*args):
        pytest.fail("frozen document was regenerated")
    monkeypatch.setattr("backend.core.loop.document_candidate_runtime.DocumentCandidateRuntime.render", must_not_render)
    final = call({**args, "lesson_feedback_id": first.data["lesson_feedback"]["feedback_id"]}, 1)
    assert not final.is_error, final.formatted
    assert final.data["candidate_source"] == "frozen" and (ws / "report.docx").read_bytes() == raw
    assert final.data["sha256"] == hashlib.sha256(raw).hexdigest()
    assert final.receipt["effect_state"] == "committed"
    assert "runtime_cleanup" not in final.receipt and "launch_state" not in final.receipt


def test_binary_unknown_check_stays_blocked_with_an_exact_user_grant(tmp_path_factory, monkeypatch):
    from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
    ws = tmp_path_factory
    fake_producer(monkeypatch)
    LessonManager.save(ws, Lesson(id="required", project_name="p", trigger="report.docx", rule="Required content check",
        dangerous_tools=["write_file"], check={"authority": "registered", "mode": "violation_pattern", "paths": ["report.docx"], "pattern": "retired"}))
    ctx, call = make(ws)
    args = {"path": "report.docx", "content": "safe"}
    tools = build_workspace_tools(ws)
    create_permission_request(ctx.process, {"purpose": "Use this exact candidate", "tool_name": "document_create", "arguments": args}, tools, str(ws))
    grant_from_decision(ctx.process, ctx.process.pending_decision, "allow_once")
    result = call(args)
    assert result.data["error"] == "LESSON_CHECK_UNAVAILABLE" and not (ws / "report.docx").exists()
    assert result.receipt["lesson_user_override"] is True


def test_explicit_other_path_check_does_not_block_binary_output(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    fake_producer(monkeypatch)
    LessonManager.save(ws, Lesson(id="code", project_name="p", trigger="retired", rule="Code-only check",
        dangerous_tools=["write_file"], check={"authority": "registered", "mode": "violation_pattern", "paths": ["cache.py"], "pattern": "retired"}))
    ctx, call = make(ws)
    result = call({"path": "report.docx"})
    assert not result.is_error and result.receipt["lesson_mandatory_check_state"] == "not_required"


def test_cancel_after_render_retains_candidate_but_never_commits(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    ctx, call = make(ws)
    def render(runtime, spec, suffix):
        ctx.cancel()
        runtime.last_render = {"cleanup": {"complete": True}, "access_retired": True}
        return b"binary"
    monkeypatch.setattr("backend.core.loop.document_candidate_runtime.DocumentCandidateRuntime.render", render)
    result = call({"path": "report.docx"})
    assert result.data["error"] == "TOOL_CANCELLED" and result.receipt["effect_state"] == "not_committed"
    assert not (ws / "report.docx").exists()


def test_cache_retirement_failure_cannot_relabel_a_committed_file(tmp_path_factory, monkeypatch):
    from backend.core.storage import StorageRuntime
    fake_producer(monkeypatch)
    def fail(*args, **kwargs):
        raise OSError("retirement write failed")
    monkeypatch.setattr(StorageRuntime, "release_state_ref", fail)
    ctx, call = make(tmp_path_factory)
    result = call({"path": "report.docx"})
    assert not result.is_error and result.receipt["effect_state"] == "committed"
    assert result.data["warnings"][0]["code"] == "DOCUMENT_CANDIDATE_RETIRE_FAILED"
    assert (tmp_path_factory / "report.docx").exists()


@pytest.mark.parametrize("fault", ["producer_cleanup", "access_retirement"])
def test_unknown_native_cleanup_retains_stage_and_refuses_commit(tmp_path_factory, document_native_launcher, monkeypatch, fault):
    from backend.core.storage import get_storage
    from backend.core.sandbox_access import revoke_record, WorkspaceAccess
    from backend.core.sandbox import SandboxDenied
    ws = tmp_path_factory
    ctx, call = make(ws)
    calls = []
    def incomplete(pipeline, name, args, timeout, **kwargs):
        calls.append(name)
        metadata = kwargs["runtime_metadata"]
        metadata.update(launch_state="started", exit_code=0,
            cleanup={"complete": fault != "producer_cleanup", "mode": "injected_unknown"})
        if fault == "access_retirement":
            # Synthetic producer protocol, not evidence of an OS process.
            (Path(args["_workspace"]) / args["path"]).write_bytes(b"fixture")
        return {}
    close_access = WorkspaceAccess.close
    def fail_retirement(access):
        raise SandboxDenied("SANDBOX_CLEANUP_PENDING", "Injected access retirement failure")
    if fault == "access_retirement":
        monkeypatch.setattr(WorkspaceAccess, "close", fail_retirement)
    monkeypatch.setattr(ToolPipeline, "_execute_isolated", incomplete)
    storage = get_storage(ws)
    try:
        result = call({"path": "report.docx"})
        assert result.data["error"] == "SANDBOX_CLEANUP_PENDING" and result.receipt["effect_state"] == "not_committed"
        assert result.data["candidate_render"]["cleanup"]["complete"] is False
        assert result.receipt["runtime_cleanup"]["complete"] is False
        assert ctx.process.recovery["resume_forbidden"] is True
        stages = list((candidate_workspace_root(storage.paths)).iterdir())
        retry = call({"path": "another.docx"}, 1)
        assert retry.data["error"] == "SANDBOX_CLEANUP_PENDING"
        assert retry.receipt["effect_state"] == "not_committed"
        assert calls == ["document_render"]
        assert list((candidate_workspace_root(storage.paths)).iterdir()) == stages
        assert not (ws / "report.docx").exists()
        assert list((candidate_workspace_root(storage.paths)).iterdir())
    finally:
        # Protocol fault fixture launched no process. Retire its real owned ACL
        # journal explicitly; this is not a forged product cleanup observation.
        monkeypatch.setattr(WorkspaceAccess, "close", close_access)
        for record in (storage.paths.project_root / "sandbox_access").glob("d-*.json"):
            revoke_record(record)
        from backend.core.loop.read_approval import _RETAINED_ADMISSIONS
        for owners in _RETAINED_ADMISSIONS.pop((ctx.process.process_id, "doc-0", 0), []):
            owners.close()


def test_post_commit_observation_error_is_not_a_preflight_read_failure(tmp_path_factory, monkeypatch):
    from backend.core.tools import document_service
    ctx, call = make(tmp_path_factory)
    def commit_then_fail(candidate, args):
        candidate.path.write_bytes(b"effect")
        raise OSError("observation failed")
    monkeypatch.setattr(document_service, "commit_file_mutation", commit_then_fail)
    result = call({"path": "note.md", "content": "effect"})
    assert result.is_error and result.receipt["effect_state"] == "ambiguous"
    assert (tmp_path_factory / "note.md").read_bytes() == b"effect"


def test_direct_binary_creation_does_not_run_a_trusted_format_library(tmp_path_factory):
    from backend.core.tools.document_tools import document_create
    result = document_create({"_workspace": str(tmp_path_factory), "path": "report.docx"})
    assert result["error"] == "FILE_SERVICE_REQUIRED" and not (tmp_path_factory / "report.docx").exists()


def test_approved_document_grant_expires_when_only_service_code_changes(tmp_path_factory, monkeypatch):
    from backend.core.loop.permission_broker import create_permission_request, grant_from_decision, matching_grant
    ctx, _ = make(tmp_path_factory)
    tools = build_workspace_tools(tmp_path_factory)
    args = {"path": "note.md", "content": "approved"}
    create_permission_request(ctx.process, {"purpose": "Exact document effect", "tool_name": "document_create", "arguments": args}, tools, str(tmp_path_factory))
    grant = grant_from_decision(ctx.process, ctx.process.pending_decision, "allow_once")
    assert grant is not None
    assert matching_grant(ctx.process, "document_create", args, per_invocation=False, tool=tools["document_create"]) is not None
    original = Path.read_bytes
    def changed(path):
        raw = original(path)
        return raw + b"\n# simulated new service revision\n" if path.name == "document_service.py" else raw
    monkeypatch.setattr(Path, "read_bytes", changed)
    assert matching_grant(ctx.process, "document_create", args, per_invocation=False, tool=tools["document_create"]) is None


def test_pending_candidate_is_a_gc_root_then_releases_exactly(tmp_path_factory, monkeypatch):
    from backend.core.storage import get_storage
    fake_producer(monkeypatch)
    LessonManager.save(tmp_path_factory, Lesson(id="advice", project_name="p", trigger="report.docx", rule="Verify sources"))
    ctx, call = make(tmp_path_factory)
    first = call({"path": "report.docx"})
    storage = get_storage(tmp_path_factory)
    inventory = storage._cas_reference_inventory().require_complete()
    assert first.data["candidate_ref"].removeprefix("sha256:") in inventory


def test_text_document_uses_existing_registered_content_checker(tmp_path_factory):
    LessonManager.save(tmp_path_factory, Lesson(id="text", project_name="p", trigger="report.md", rule="Avoid retired",
        dangerous_tools=["write_file"], check={"authority": "registered", "mode": "violation_pattern", "paths": ["report.md"], "pattern": "retired"}))
    ctx, call = make(tmp_path_factory)
    result = call({"path": "report.md", "content": "retired"})
    assert result.data["error"] == "KNOWN_LESSON_VIOLATION"
    assert not (tmp_path_factory / "report.md").exists()


def test_model_cannot_add_an_isolation_opt_out(tmp_path_factory, monkeypatch):
    calls = fake_producer(monkeypatch)
    ctx, call = make(tmp_path_factory)
    result = call({"path": "report.docx", "disable_sandbox": True})
    assert result.is_error and result.receipt["effect_state"] == "not_committed"
    assert not calls and not (tmp_path_factory / "report.docx").exists()


def test_explicit_extra_field_contract_allows_host_fields_and_pattern_keys():
    from backend.core.loop.tool_pipeline import _validate_args
    schema = {"properties": {"path": {"type": "string"}}, "patternProperties": {"^x-": {}}, "additionalProperties": False}
    _validate_args({"path": "note.md", "x-meta": "bound", "_workspace": "host-selected"}, schema)
    with pytest.raises(ValueError, match="Unsupported public arguments"):
        _validate_args({"path": "note.md", "unexpected": "ignored before"}, schema)


def test_text_document_changed_excerpts_reach_existing_followup_capture(tmp_path_factory):
    from backend.core.loop.loop_bookkeeping import capture_lesson_outcome
    from backend.core.loop.tool_pipeline import ToolResult
    from backend.core.history import HistoryManager
    from backend.core.knowledge.harvest import get_unprocessed_signals
    ws = tmp_path_factory
    ctx, call = make(ws)
    failure = ToolResult(id="failed", tool_name="exec_command", data={"stderr": "source evidence missing"},
        receipt={"receipt_id": "failed", "command_completed": True, "command_exit_code": 1, "files": ["report.md"]})
    capture_lesson_outcome(ctx.process, failure, str(ws))
    result = call({"path": "report.md", "content": "Corrected source evidence\n"})
    assert not result.is_error
    capture_lesson_outcome(ctx.process, result, str(ws))
    with HistoryManager.workspace_scope(str(ws)):
        followup = get_unprocessed_signals("p", "execution_followup")[0]
    assert followup["knowledge_excerpts"][0]["path"] == "report.md"
    assert followup["knowledge_excerpts"][0]["observed_file_sha256"] == result.data["sha256"]
