"""Deletion shares candidate checks, exposure facts and writer locks with edits."""
import hashlib
import os
from pathlib import Path

import pytest

from backend.core.knowledge import prewrite
from backend.core.knowledge.feedback import observe_provider_input
from backend.core.knowledge.manager import LessonManager
from backend.core.knowledge.models import Lesson
from backend.core.storage.maintenance import StorageLease
from backend.core.tools.file_mutation import commit_file_mutation
from backend.core.tools.workspace_tools import delete_file, project_file_mutation
from tests.test_file_feedback_contract import context
from backend.core.tools.catalog import build_workspace_tools
from backend.core.loop.tool_pipeline import ToolPipeline


def arguments(ws, raw=b"original\r\n"):
    (ws / "cache.py").write_bytes(raw)
    return {"_workspace": str(ws), "path": "cache.py",
            "expected_sha256": hashlib.sha256(raw).hexdigest(),
            "_lesson_context": {"project_name": "p"}}


def lesson(ws, **extra):
    fields = dict(id="deletion", project_name="p", trigger="cache.py",
                  rule="Keep the cache registry until its consumers are migrated")
    fields.update(extra)
    LessonManager.save(ws, Lesson(**fields))


def test_deletion_projection_is_pure_and_absence_is_not_an_empty_file(tmp_path_factory):
    ws = tmp_path_factory
    args = arguments(ws, b"")
    candidate = project_file_mutation("delete_file", args)
    assert not (ws / ".gitgo").exists()
    assert candidate.before_sha256 == hashlib.sha256(b"").hexdigest()
    assert candidate.after_sha256 == ""
    assert (ws / "cache.py").exists()
    result = commit_file_mutation(candidate, args)
    assert result["after_sha256"] == result["candidate_sha256"] == ""
    assert result["recoverable"] and not (ws / "cache.py").exists()
    assert (ws / result["trash_ref"]).read_bytes() == b""


def test_stale_deletion_cannot_remove_newer_content(tmp_path_factory):
    ws = tmp_path_factory
    args = arguments(ws)
    candidate = project_file_mutation("delete_file", args)
    (ws / "cache.py").write_bytes(b"another writer")
    result = commit_file_mutation(candidate, args)
    assert result["error"] == "FILE_CHANGED"
    assert result["effect_state"] == "not_committed"
    assert (ws / "cache.py").read_bytes() == b"another writer"
    assert not (ws / ".gitgo/trash").exists()


def test_advice_requires_real_provider_exposure_before_deletion(tmp_path_factory):
    ws = tmp_path_factory
    args = arguments(ws)
    lesson(ws)
    public = {k: v for k, v in args.items() if not k.startswith("_")}
    ctx = context(ws, ["delete_file"])
    ctx.process.update_context_snapshot(lambda s: {**s, "project_name": "p"})
    tools = build_workspace_tools(ws)
    def call(values, index):
        return ToolPipeline().execute({"name": "delete_file", "id": f"c{index}", "args": values},
                                      tools["delete_file"], ctx, f"delete-{index}")
    first = call(public, 0)
    assert first.data["error"] == "LESSON_FEEDBACK_REQUIRED"
    assert first.receipt["effect_state"] == "not_committed"
    assert (ws / "cache.py").exists() and not (ws / ".gitgo/trash").exists()
    reference = first.data["lesson_feedback"]["feedback_id"]
    public["lesson_feedback_id"] = reference
    assert call(public, 1).is_error
    ctx.session.append_tool_result(first.formatted, tool_name="delete_file", tool_call_id="c0",
                                   data=first.data, receipt=first.receipt)
    observe_provider_input(ctx.process, [{"role": "assistant", "content": reference}])
    assert call(public, 2).is_error
    observe_provider_input(ctx.process, [{"role": "tool", "content": first.formatted}])
    # An exposed ID does not authorize a different file version.
    (ws / "cache.py").write_bytes(b"revised")
    revised = {**public, "expected_sha256": hashlib.sha256(b"revised").hexdigest()}
    changed = call(revised, 3)
    assert changed.data["error"] == "LESSON_FEEDBACK_REQUIRED"
    assert changed.data["lesson_feedback"]["continuation_state"] == "binding_changed"
    (ws / "cache.py").write_bytes(b"original\r\n")
    committed = call(public, 4)
    assert not committed.is_error and committed.receipt["effect_state"] == "committed"
    assert committed.receipt["after_sha256"] == ""
    assert committed.receipt["lesson_advisory_state"] == "continuation_requested"
    assert (ws / committed.data["trash_ref"]).read_bytes() == b"original\r\n"


@pytest.mark.parametrize("failure", ["inventory", "broken", "stale", "violation"])
def test_mandatory_deletion_failure_does_not_create_trash(tmp_path_factory, monkeypatch, failure):
    ws = tmp_path_factory
    args = arguments(ws)
    if failure == "inventory":
        def fail(*a):
            raise OSError("inventory unavailable")
        monkeypatch.setattr(prewrite, "_load", fail)
    else:
        lesson(ws, dangerous_tools=["delete_file"],
               expires_at="2000-01-01T00:00:00Z" if failure == "stale" else "",
               check={"authority": "registered", "mode": "violation_pattern",
                      "pattern": "[" if failure == "broken" else "^$"})
    result = delete_file(args)
    assert result["error"] == ("KNOWN_LESSON_VIOLATION" if failure == "violation" else "LESSON_CHECK_UNAVAILABLE")
    assert result["effect_state"] == "not_committed"
    assert (ws / "cache.py").read_bytes() == b"original\r\n"
    assert not (ws / ".gitgo/trash").exists()


def test_deletion_rechecks_version_after_checks(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    args = arguments(ws)
    def mutate(candidate, args):
        candidate.path.write_bytes(b"concurrent")
        return {"blocked": False, "diagnostics": []}
    monkeypatch.setattr(prewrite, "check_candidate", mutate)
    result = delete_file(args)
    assert result["error"] == "FILE_CHANGED"
    assert (ws / "cache.py").read_bytes() == b"concurrent"
    assert not (ws / ".gitgo/trash").exists()


def test_deletion_contends_on_the_same_lock_as_writes(tmp_path_factory):
    ws = tmp_path_factory
    args = arguments(ws)
    key = hashlib.sha256(str(ws / "cache.py").casefold().encode()).hexdigest()
    root = ws / ".gitgo/mutation-locks" / key
    root.mkdir(parents=True)
    with StorageLease(root, exclusive=True):
        result = delete_file(args)
    assert result["error"] == "FILE_MUTATION_BUSY"
    assert (ws / "cache.py").exists()


def test_post_rename_read_failure_is_uncertain_and_retains_recovery_reference(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    args = arguments(ws, b"binary\x00\xff")
    original = Path.read_bytes
    def unavailable(path):
        if "trash" in path.parts:
            raise OSError("retained object cannot be read")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", unavailable)
    result = delete_file(args)
    assert result["error"] == "FILE_MUTATION_FAILED"
    assert result["effect_state"] == "ambiguous" and result["recovery_state"] == "unknown"
    assert not (ws / "cache.py").exists()
    assert original(ws / result["trash_ref"]) == b"binary\x00\xff"


def test_external_change_in_final_rename_window_is_not_reported_as_checked_deletion(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    args = arguments(ws)
    original = os.rename
    def race(source, target):
        Path(source).write_bytes(b"external version")
        original(source, target)
    monkeypatch.setattr(os, "rename", race)
    result = delete_file(args)
    assert result["error"] == "FILE_CHANGED_AFTER_COMMIT" and result["effect_state"] == "ambiguous"
    assert (ws / result["trash_ref"]).read_bytes() == b"external version"


def test_missing_delete_target_is_not_an_uncertain_effect(tmp_path_factory):
    result = delete_file({"_workspace": str(tmp_path_factory), "path": "missing.txt", "expected_sha256": "0" * 64})
    assert result["error"] == "FILE_NOT_FOUND" and result["effect_state"] == "not_committed"
    assert not (tmp_path_factory / ".gitgo").exists()


def test_trash_preparation_failure_is_not_an_attempted_deletion(tmp_path_factory):
    ws = tmp_path_factory
    args = arguments(ws)
    (ws / ".gitgo").mkdir()
    # A regular file at trash prevents preparation without touching the target.
    (ws / ".gitgo/trash").write_bytes(b"blocked")
    result = delete_file(args)
    assert result["error"] == "FILE_MUTATION_FAILED" and result["effect_state"] == "not_committed"
    assert result["execution_state"] == "not_started" and result["action"] == "delete_failed"
    assert (ws / "cache.py").read_bytes() == b"original\r\n"
