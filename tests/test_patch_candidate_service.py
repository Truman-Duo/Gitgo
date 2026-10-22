"""Private projection and all-before-effects checks through the real file broker."""
import difflib
import hashlib
from pathlib import Path

import pytest

from backend.core.knowledge import prewrite
from backend.core.knowledge.feedback import observe_provider_input
from backend.core.knowledge.manager import LessonManager
from backend.core.knowledge.models import Lesson
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.tools.catalog import build_workspace_tools
from backend.core.tools.file_mutation_batch import commit_file_batch
from backend.core.tools.patch_projection import project_patch
from backend.core.tools.workspace_tools import apply_patch
from tests.test_file_feedback_contract import context


def diff(path, old, new):
    return "".join(difflib.unified_diff((old or "").splitlines(True), (new or "").splitlines(True),
        fromfile="/dev/null" if old is None else "a/" + path,
        tofile="/dev/null" if new is None else "b/" + path))


def fixture(ws):
    (ws / "a.txt").write_bytes(b"first\n")
    (ws / "cache.py").write_bytes(b"old\n")
    patch = diff("a.txt", "first\n", "changed\n") + diff("cache.py", "old\n", "new\n")
    return {"_workspace": str(ws), "_lesson_context": {"project_name": "p"}, "patch": patch}


def test_projection_has_no_formal_file_or_management_effects(tmp_path_factory):
    ws = tmp_path_factory
    args = fixture(ws)
    batch = project_patch(args)
    assert not isinstance(batch, dict), batch
    assert [c.path.name for c in batch.members] == ["a.txt", "cache.py"]
    assert (ws / "a.txt").read_bytes() == b"first\n"
    assert not (ws / ".gitgo").exists()
    args["patch"] = "not the inspected patch"
    result = commit_file_batch(batch, args)
    assert result["effect_state"] == "committed"
    assert result["candidate_sha256"] == result["after_sha256"]
    assert (ws / "cache.py").read_bytes() == b"new\n"


def test_later_mandatory_violation_prevents_all_writes(tmp_path_factory):
    ws = tmp_path_factory
    args = fixture(ws)
    LessonManager.save(ws, Lesson(id="check", project_name="p", trigger="cache.py", rule="Avoid new",
        dangerous_tools=["write_file"], check={"authority": "registered", "mode": "violation_pattern", "pattern": "new"}))
    result = apply_patch(args)
    assert result["error"] == "KNOWN_LESSON_VIOLATION"
    assert result["effect_state"] == "not_committed"
    assert (ws / "a.txt").read_bytes() == b"first\n"
    assert (ws / "cache.py").read_bytes() == b"old\n"


def test_batch_advice_exposure_binds_even_unalerted_files(tmp_path_factory):
    ws = tmp_path_factory
    args = fixture(ws)
    LessonManager.save(ws, Lesson(id="advice", project_name="p", trigger="cache.py", rule="Keep the cache contract"))
    ctx = context(ws, ["apply_patch"])
    ctx.process.update_context_snapshot(lambda s: {**s, "project_name": "p"})
    tool = build_workspace_tools(ws)["apply_patch"]
    def call(patch, identifier="", index=0):
        return ToolPipeline().execute({"name": "apply_patch", "id": f"c{index}",
            "args": {"patch": patch, **({"lesson_feedback_id": identifier} if identifier else {})}}, tool, ctx, f"patch-{index}")
    first = call(args["patch"])
    assert first.data["error"] == "LESSON_FEEDBACK_REQUIRED"
    identifier = first.data["lesson_feedback"]["feedback_id"]
    assert call(args["patch"], identifier, 1).is_error
    ctx.session.append_tool_result(first.formatted, tool_name="apply_patch", tool_call_id="c0", data=first.data, receipt=first.receipt)
    observe_provider_input(ctx.process, [{"role": "tool", "content": first.formatted}])
    altered = args["patch"].replace("+changed", "+different")
    changed = call(altered, identifier, 2)
    assert changed.data["error"] == "LESSON_FEEDBACK_REQUIRED"
    assert changed.data["lesson_feedback"]["continuation_state"] == "binding_changed"
    assert (ws / "a.txt").read_bytes() == b"first\n"
    final = call(args["patch"], identifier, 3)
    assert not final.is_error and final.receipt["effect_state"] == "committed"
    assert final.receipt["files"] == ["a.txt", "cache.py"]
    assert final.receipt["lesson_advisory_state"] == "continuation_requested"


def test_mid_commit_failure_keeps_prior_effects_and_marks_remaining_not_started(tmp_path_factory, monkeypatch):
    from backend.core.tools import workspace_tools
    ws = tmp_path_factory
    args = fixture(ws)
    args["patch"] += diff("z.txt", "", "created\n").replace("--- a/z.txt", "--- /dev/null")
    original = workspace_tools._atomic_write
    def fail(path, raw):
        if path.name == "cache.py":
            raise OSError("simulated replace failure")
        return original(path, raw)
    monkeypatch.setattr(workspace_tools, "_atomic_write", fail)
    result = apply_patch(args)
    assert result["error"] == "FILE_MUTATION_FAILED" and result["effect_state"] == "ambiguous"
    assert [r["effect_state"] for r in result["file_results"]] == ["committed", "ambiguous", "not_committed"]
    assert (ws / "a.txt").read_bytes() == b"changed\n"
    assert (ws / "cache.py").read_bytes() == b"old\n"
    assert not (ws / "z.txt").exists()


def test_a_version_change_during_inspection_prevents_all_commits(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    args = fixture(ws)
    original = prewrite.check_candidate
    def check(candidate, args):
        if candidate.path.name == "cache.py":
            (ws / "a.txt").write_bytes(b"concurrent")
        return original(candidate, args)
    monkeypatch.setattr(prewrite, "check_candidate", check)
    result = apply_patch(args)
    assert result["error"] == "FILE_CHANGED" and result["effect_state"] == "not_committed"
    assert (ws / "a.txt").read_bytes() == b"concurrent"
    assert (ws / "cache.py").read_bytes() == b"old\n"


def test_patch_deletion_uses_recoverable_file_service(tmp_path_factory):
    ws = tmp_path_factory
    (ws / "a.txt").write_bytes(b"old\n")
    patch = "--- a/a.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
    result = apply_patch({"_workspace": str(ws), "patch": patch})
    assert result["effect_state"] == "committed" and not (ws / "a.txt").exists()
    row = result["file_results"][0]
    assert row["candidate_sha256"] == row["after_sha256"] == ""
    assert (ws / row["trash_ref"]).read_bytes() == b"old\n"


@pytest.mark.parametrize("path", ["../../outside.txt", ".git/config", ".gitgo/state.json", "C:/outside.txt"])
def test_unsafe_or_management_paths_are_rejected_before_effects(tmp_path_factory, path):
    result = apply_patch({"_workspace": str(tmp_path_factory), "patch": diff(path, "old\n", "new\n")})
    assert result["error"] == "PATCH_CHECK_FAILED" and result["effect_state"] == "not_committed"
    assert not (tmp_path_factory / ".gitgo").exists()


def test_removed_content_resembling_a_header_is_not_an_extra_target(tmp_path_factory):
    ws = tmp_path_factory
    old = "-- a/fake.txt\n++ b/fake.txt\n"
    (ws / "real.txt").write_bytes(old.encode())
    result = apply_patch({"_workspace": str(ws), "patch": diff("real.txt", old, "real\n")})
    assert result["applied"] and result["files"] == ["real.txt"]
    assert not (ws / "fake.txt").exists()


@pytest.mark.parametrize("name", ["a b.txt", "中文.txt"])
def test_utf8_and_spaced_paths_are_preserved(tmp_path_factory, name):
    (tmp_path_factory / name).write_bytes(b"old\n")
    result = apply_patch({"_workspace": str(tmp_path_factory), "patch": diff(name, "old\n", "new\n")})
    assert result["applied"] and result["files"] == [name]
    assert (tmp_path_factory / name).read_bytes() == b"new\n"


def test_project_git_configuration_and_inherited_git_overrides_are_not_used(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    args = fixture(ws)
    (ws / ".git").mkdir()
    (ws / ".git/config").write_text("[include]\n path = nonexistent-broken-config\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "invalid")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "broken")
    monkeypatch.setenv("GIT_DIR", str(ws / ".git"))
    result = apply_patch(args)
    assert result["applied"]
    assert (ws / ".git/config").read_text().startswith("[include]")


def test_unsupported_mode_patch_is_explicitly_uncommitted(tmp_path_factory):
    ws = tmp_path_factory
    patch = "diff --git a/a b/a\nnew file mode 120000\n--- /dev/null\n+++ b/a\n@@ -0,0 +1 @@\n+outside\n"
    result = apply_patch({"_workspace": str(ws), "patch": patch})
    assert result["error"] == "PATCH_UNSUPPORTED" and result["effect_state"] == "not_committed"
    assert not (ws / "a").exists()


def test_partial_conflict_is_distinct_from_unknown_commit(tmp_path_factory, monkeypatch):
    from backend.core.tools import workspace_tools
    ws = tmp_path_factory
    args = fixture(ws)
    original = workspace_tools._atomic_write
    def concurrent(path, raw):
        original(path, raw)
        if path.name == "a.txt":
            (ws / "cache.py").write_bytes(b"external")
    monkeypatch.setattr(workspace_tools, "_atomic_write", concurrent)
    result = apply_patch(args)
    assert result["error"] == "FILE_CHANGED" and result["effect_state"] == "partial"
    assert [row["effect_state"] for row in result["file_results"]] == ["committed", "not_committed"]
    assert (ws / "cache.py").read_bytes() == b"external"


def test_user_activated_path_checker_is_reused_by_patch_member_writes(tmp_path_factory):
    from backend.core.knowledge.checker import request_change, apply_decision
    from backend.core.knowledge.review import content_version
    from backend.core.loop.permission_broker import grant_from_decision
    from backend.core.storage import get_storage
    from tests.test_lesson_preaction import setup
    ws = tmp_path_factory
    p, tools, ctx = setup(ws)
    storage = get_storage(ws)
    record = storage.get_lesson_record("advice")[1]
    args = {"lesson_id": "advice", "expected_version": content_version(record),
            "action": "enable", "violation_literal": "retired()", "paths": ["cache.py"]}
    request_change(p, storage, ws, args)
    pending = p.pending_decision
    grant_from_decision(p, pending, "allow_once")
    apply_decision(p, pending, "allow_once", storage)
    (ws / "cache.py").write_bytes(b"current()\n")
    result = apply_patch({"_workspace": str(ws), "_lesson_context": {"project_name": "p"},
        "patch": diff("cache.py", "current()\n", "retired()\n")})
    assert result["error"] == "KNOWN_LESSON_VIOLATION"
    assert result["lesson_feedback"]["mandatory_check_state"] == "violation"
    assert (ws / "cache.py").read_bytes() == b"current()\n"


def test_private_projection_uses_descriptor_read_not_a_path_reopen(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    args = fixture(ws)
    original = Path.read_bytes
    def no_workspace_reopen(path):
        if path.is_relative_to(ws):
            pytest.fail("projection reopened workspace source by path")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", no_workspace_reopen)
    batch = project_patch(args)
    assert not isinstance(batch, dict), batch
    assert len(batch.members) == 2


def test_batch_receipt_retains_narrow_per_file_effects_and_excerpts(tmp_path_factory):
    ws = tmp_path_factory
    args = fixture(ws)
    ctx = context(ws, ["apply_patch"])
    tool = build_workspace_tools(ws)["apply_patch"]
    result = ToolPipeline().execute({"name": "apply_patch", "args": {"patch": args["patch"]}}, tool, ctx, "receipt")
    assert not result.is_error
    assert len(result.receipt["file_mutations"]) == 2
    assert all(row["effect_state"] == "committed" for row in result.receipt["file_mutations"])
    assert {item["path"] for item in result.data["knowledge_excerpts"]} == {"a.txt", "cache.py"}


def test_multiple_advice_versions_beyond_display_limit_still_bind_the_batch(tmp_path_factory):
    ws = tmp_path_factory
    args = fixture(ws)
    for index in range(22):
        LessonManager.save(ws, Lesson(id=f"advice-{index:02}", project_name="p", trigger="cache.py", rule=f"Cache constraint {index}"))
    first = apply_patch(args)
    identifier = first["lesson_feedback"]["feedback_id"]
    assert first["lesson_feedback"]["diagnostic_coverage"] == "partial"
    record = LessonManager.load_instance(ws, "p")
    last = next(l for l in record if l.id == "advice-21")
    last.rule = "A revised constraint outside the displayed diagnostics"
    LessonManager.save(ws, last)
    args["lesson_feedback_id"] = identifier
    args["_lesson_context"]["feedback_in_provider_input"] = [identifier]
    changed = apply_patch(args)
    assert changed["error"] == "LESSON_FEEDBACK_REQUIRED"
    assert changed["lesson_feedback"]["continuation_state"] == "binding_changed"
    assert (ws / "a.txt").read_bytes() == b"first\n"


def test_runtime_cancellation_after_projection_prevents_formal_commit(tmp_path_factory, monkeypatch):
    from backend.core.tools import patch_projection
    ws = tmp_path_factory
    args = fixture(ws)
    ctx = context(ws, ["apply_patch"])
    original = patch_projection.project_patch
    def cancel(args):
        batch = original(args)
        ctx.cancel()
        return batch
    monkeypatch.setattr(patch_projection, "project_patch", cancel)
    tool = build_workspace_tools(ws)["apply_patch"]
    result = ToolPipeline().execute({"name": "apply_patch", "args": {"patch": args["patch"]}}, tool, ctx, "cancel-before-commit")
    assert result.data["error"] == "TOOL_CANCELLED" and result.receipt["effect_state"] == "not_committed"
    assert (ws / "a.txt").read_bytes() == b"first\n"
    assert (ws / "cache.py").read_bytes() == b"old\n"


def test_runtime_cancellation_between_files_retains_completed_effects(tmp_path_factory, monkeypatch):
    from backend.core.tools import workspace_tools
    ws = tmp_path_factory
    args = fixture(ws)
    ctx = context(ws, ["apply_patch"])
    original = workspace_tools._atomic_write
    def cancel(path, raw):
        original(path, raw)
        if path.name == "a.txt":
            ctx.cancel()
    monkeypatch.setattr(workspace_tools, "_atomic_write", cancel)
    tool = build_workspace_tools(ws)["apply_patch"]
    result = ToolPipeline().execute({"name": "apply_patch", "args": {"patch": args["patch"]}}, tool, ctx, "cancel-partial")
    assert result.data["error"] == "TOOL_CANCELLED" and result.receipt["effect_state"] == "partial"
    assert [r["effect_state"] for r in result.receipt["file_mutations"]] == ["committed", "not_committed"]
    assert result.receipt["file_mutations"][1]["execution_state"] == "not_started"
    assert (ws / "a.txt").read_bytes() == b"changed\n"
    assert (ws / "cache.py").read_bytes() == b"old\n"
