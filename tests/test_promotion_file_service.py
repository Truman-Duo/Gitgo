"""Formal promotion checks: actual Git/SQLite/Pipeline; no model/API claims."""
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

import pytest

from tests.test_agent_lifecycle_closeout import _cleanup
from tests.test_agent_dag_worktree import _repo, _process, _git
from tests.promotion_native_fixture import authorize_project, observe_formal_apply, fail_committed_ack
from backend.core.knowledge.manager import LessonManager
from backend.core.knowledge.models import Lesson
from backend.core.knowledge.feedback import observe_provider_input
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import ProcessStatus
from backend.core.loop.protocol_tools.registry import build_internal_tools
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.worktree import AgentWorktreeManager, WorktreeError
from backend.core.storage import StorageRuntime
from backend.core.tools.catalog import build_workspace_tools


@pytest.fixture
def manager_factory(tmp_path_factory, monkeypatch, request):
    def make(repo, store):
        snapshot = ""
        if sys.platform == "darwin":
            authorize_project(repo, monkeypatch, request, tmp_path_factory / "broker-fixture")
            snapshot = _git(repo, "rev-parse", "HEAD")
        return AgentProcessManager(worktree_manager=AgentWorktreeManager(repo, store),
                                   project_snapshot_commit=snapshot)
    return make


@pytest.fixture
def case(tmp_path_factory, manager_factory):
    repo = _repo(tmp_path_factory)
    store = StorageRuntime(repo, state_home=tmp_path_factory / "state")
    manager = manager_factory(repo, store)
    root = _process(manager, task_id="root")
    children = []
    LessonManager.bind_storage(repo, store)
    root.update_context_snapshot(lambda s: {**s, "project_name": "p"})
    try:
        a = _process(manager, parent_id=root.process_id, task_id="a")
        children.append(a)
        path = Path(manager.materialize_process_worktree(a))
        (path / "app.txt").write_text("upstream\n")
        a.status = ProcessStatus.COMPLETED
        manager.seal_process_worktree(a)
        b = _process(manager, parent_id=root.process_id, task_id="b", depends_on=[a.process_id])
        children.append(b)
        path = Path(manager.materialize_process_worktree(b))
        (path / "result.txt").write_text("consumer\n")
        b.status = ProcessStatus.COMPLETED
        manager.seal_process_worktree(b)
        for child in children:
            root.child_reviews[child.process_id] = {"verdict": "approved"}
        yield repo, store, manager, root, a, b
    finally:
        _cleanup(store, manager, *children)


def lesson(repo, path, *, required=False, literal="consumer"):
    value = Lesson(id="formal-check", project_name="p", trigger=path, rule="Inspect the formal delivery delta")
    if required:
        value.dangerous_tools = ["write_file"]
        value.check = {"authority": "registered", "mode": "violation_pattern", "paths": [path], "pattern": literal}
    LessonManager.save(repo, value)


def pipeline(case, *, approve=True):
    repo, store, manager, root, a, b = case
    root.tool_registry = ToolRegistry(["promote_agent_changes"])
    tools = build_internal_tools(root, build_workspace_tools(repo), str(repo), None, None, None)
    tool = tools["promote_agent_changes"]
    ctx = ExecutionContext(root, root.session, str(repo), storage=store, cancellation=root.cancellation_event)
    ctx.artifacts["tool_catalog"] = tools
    def call(args, index=0):
        if manager.project_snapshot_commit and approve:
            from backend.core.loop.permission_broker import automatic_permission_request, grant_from_decision, matching_grant
            if matching_grant(root, tool.name, args, per_invocation=True, tool=tool) is None:
                automatic_permission_request(root, tool, args, str(repo))
                grant_from_decision(root, root.pending_decision, "allow_once")
                root.pending_decision = None
        return ToolPipeline().execute({"name": tool.name, "args": args}, tool, ctx, f"promotion-{index}")
    return ctx, call


def native_advice_roundtrip(case, *, changed_group=False):
    """Exercise internal file-service advice, separate from explicit approval.

    Configured projects require user approval in ToolPipeline; approval is an
    intentional override of known advice. This service-level test keeps that
    boundary intact and supplies the Host receipt as a caller fixture.
    """
    repo, store, manager, root, a, b = case
    args = {"process_ids": [a.process_id if changed_group else b.process_id]}
    _, public_call = pipeline(case, approve=False)
    unapproved = public_call(args)
    assert unapproved.is_error and "SENSITIVE_TOOL_APPROVAL_REQUIRED" in unapproved.formatted
    assert (repo / "app.txt").read_text() == "base\n" and not (repo / "result.txt").exists()

    def service(arguments):
        return manager.promote_process_results(root, arguments["process_ids"], file_service_args={
            key: value for key, value in arguments.items() if key != "process_ids"
        })

    with pytest.raises(WorktreeError) as first:
        service(args)
    diagnostic = first.value.diagnostic
    assert diagnostic["error"] == "LESSON_FEEDBACK_REQUIRED"
    assert diagnostic["effect_state"] == "not_committed" and not a.worktree["promoted"]
    assert not store.list_state_refs("agent_promotion", prefix="")
    feedback_id = diagnostic["lesson_feedback"]["feedback_id"]
    followup = {**args, "lesson_feedback_id": feedback_id}
    with pytest.raises(WorktreeError) as hidden:
        service(followup)
    assert hidden.value.diagnostic["error"] == "LESSON_FEEDBACK_REQUIRED"
    assert hidden.value.diagnostic["lesson_feedback"]["continuation_state"] == "reference_not_exposed"
    content = json.dumps(diagnostic)
    root.session.append_assistant("", tool_calls=[{"id": "c0", "type": "function", "function": {
        "name": "promote_agent_changes", "arguments": json.dumps(args)}}])
    root.session.append_tool_result(content, tool_name="promote_agent_changes", tool_call_id="c0",
        data=diagnostic, receipt={
            "tool_name": "promote_agent_changes", "task_id": root.active_task_id,
            "process_id": root.process_id, "effect": "workspace_write",
            "effect_state": "not_committed", "file_service_result": True,
            "lesson_feedback_id": feedback_id,
        })
    projection = root.session.to_provider_messages()
    assert any(m.get("role") == "tool" and m.get("content") == content for m in projection)
    observe_provider_input(root, projection)
    if changed_group:
        with pytest.raises(WorktreeError) as different:
            service({**followup, "process_ids": [b.process_id]})
        assert different.value.diagnostic["error"] == "LESSON_FEEDBACK_REQUIRED"
        assert different.value.diagnostic["lesson_feedback"]["continuation_state"] == "binding_changed"
        assert (repo / "app.txt").read_text() == "base\n" and not (repo / "result.txt").exists()
    else:
        time.sleep(1.05)
        final = service(followup)
        assert final["promoted"] and final["effect_state"] == "committed"
        assert final["lesson_feedback"]["advisory_state"] == "continuation_requested"
        assert (repo / "app.txt").read_text() == "upstream\n" and (repo / "result.txt").read_text() == "consumer\n"
        assert a.worktree["promoted"] and b.worktree["promoted"]
        assert {r["path"] for r in final["file_results"]} == {"app.txt", "result.txt"}
        assert final["knowledge_excerpts"]


def test_late_formal_checker_blocks_the_whole_dag_before_prepared_record(case):
    repo, store, manager, root, a, b = case
    lesson(repo, "result.txt", required=True)
    with pytest.raises(WorktreeError) as caught:
        manager.promote_process_results(root, [b.process_id])
    assert caught.value.diagnostic["error"] == "KNOWN_LESSON_VIOLATION"
    assert caught.value.diagnostic["effect_state"] == "not_committed"
    assert (repo / "app.txt").read_text() == "base\n" and not (repo / "result.txt").exists()
    assert not a.worktree["promoted"] and not b.worktree["promoted"]
    assert store.list_state_refs("agent_promotion", prefix="") == []


def test_advice_roundtrip_reuses_complete_projection_without_timestamp_churn(case):
    repo, store, manager, root, a, b = case
    lesson(repo, "result.txt")
    if manager.project_snapshot_commit:
        return native_advice_roundtrip(case)
    ctx, call = pipeline(case)
    args = {"process_ids": [b.process_id]}
    first = call(args)
    assert first.data["error"] == "LESSON_FEEDBACK_REQUIRED", first.formatted
    assert first.receipt["effect_state"] == "not_committed" and not a.worktree["promoted"]
    assert store.list_state_refs("agent_promotion", prefix="") == []
    followup = {**args, "lesson_feedback_id": first.data["lesson_feedback"]["feedback_id"]}
    assert call(followup, 1).data["error"] == "LESSON_FEEDBACK_REQUIRED"
    root.session.append_assistant("", tool_calls=[{"id": "c0", "type": "function", "function": {
        "name": "promote_agent_changes", "arguments": json.dumps(args)}}])
    root.session.append_tool_result(first.formatted, tool_name="promote_agent_changes", tool_call_id="c0", data=first.data, receipt=first.receipt)
    projection = root.session.to_provider_messages()
    assert any(m.get("role") == "tool" and m.get("content") == first.formatted for m in projection)
    observe_provider_input(root, projection)
    time.sleep(1.05)  # Actual Git integration commit timestamp is now different.
    final = call(followup, 2)
    assert not final.is_error, final.formatted
    assert final.receipt["effect_state"] == "committed" and final.data["promoted"]
    assert final.receipt["lesson_advisory_state"] == "continuation_requested"
    assert (repo / "app.txt").read_text() == "upstream\n" and (repo / "result.txt").read_text() == "consumer\n"
    assert a.worktree["promoted"] and b.worktree["promoted"]
    assert {r["path"] for r in final.data["file_results"]} == {"app.txt", "result.txt"}
    assert final.data["knowledge_excerpts"]


def test_feedback_for_a_subset_cannot_accept_the_complete_changed_group(case):
    repo, store, manager, root, a, b = case
    lesson(repo, "app.txt")
    if manager.project_snapshot_commit:
        return native_advice_roundtrip(case, changed_group=True)
    ctx, call = pipeline(case)
    first = call({"process_ids": [a.process_id]})
    root.session.append_assistant("", tool_calls=[{"id": "c0", "type": "function", "function": {
        "name": "promote_agent_changes", "arguments": json.dumps({"process_ids": [a.process_id]})}}])
    root.session.append_tool_result(first.formatted, tool_name="promote_agent_changes", tool_call_id="c0", data=first.data, receipt=first.receipt)
    projection = root.session.to_provider_messages()
    assert any(m.get("role") == "tool" and m.get("content") == first.formatted for m in projection)
    observe_provider_input(root, projection)
    different = call({"process_ids": [b.process_id], "lesson_feedback_id": first.data["lesson_feedback"]["feedback_id"]}, 1)
    assert different.data["error"] == "LESSON_FEEDBACK_REQUIRED"
    assert different.data["lesson_feedback"]["continuation_state"] == "binding_changed"
    assert (repo / "app.txt").read_text() == "base\n" and not (repo / "result.txt").exists()


def test_inventory_failure_cannot_turn_into_an_empty_promotion_check(case, monkeypatch):
    repo, store, manager, root, a, b = case
    monkeypatch.setattr("backend.core.knowledge.prewrite._load", lambda *args: (_ for _ in ()).throw(OSError("inventory unavailable")))
    with pytest.raises(WorktreeError) as caught:
        manager.promote_process_results(root, [b.process_id])
    assert caught.value.diagnostic["error"] == "LESSON_CHECK_UNAVAILABLE"
    assert (repo / "app.txt").read_text() == "base\n" and not (repo / "result.txt").exists()


def test_promotion_holds_the_same_lock_as_file_tools(case, monkeypatch):
    repo, store, manager, root, a, b = case
    from backend.core.tools.workspace_tools import write_file
    observations = []
    def competing_write():
        observations.append(write_file({"_workspace": str(repo), "_mutation_lock_root": str(store.paths.state_home / "mutation-locks"),
            "path": "app.txt", "content": "competing", "create_only": False, "expected_sha256": hashlib.sha256((repo / "app.txt").read_bytes()).hexdigest()}))
    observe_formal_apply(manager.worktree_manager, monkeypatch, competing_write)
    result = manager.promote_process_results(root, [a.process_id])
    assert result["promoted"] and observations[0]["error"] == "FILE_MUTATION_BUSY"
    assert (repo / "app.txt").read_text() == "upstream\n"


def test_owner_rebinding_during_inspection_never_reaches_formal_apply(case, monkeypatch):
    repo, store, manager, root, a, b = case
    ctx, call = pipeline(case)
    from backend.core.tools import file_mutation_batch
    original = file_mutation_batch.inspect_file_candidate
    def inspect(*args):
        result = original(*args)
        root.active_task_id = "replacement-task"
        return result
    monkeypatch.setattr(file_mutation_batch, "inspect_file_candidate", inspect)
    result = call({"process_ids": [a.process_id]})
    assert result.data["error"] == "FILE_CANDIDATE_STALE" and result.receipt["effect_state"] == "not_committed"
    assert (repo / "app.txt").read_text() == "base\n" and store.list_state_refs("agent_promotion", prefix="") == []


@pytest.mark.parametrize("required", [False, True])
def test_binary_and_deletion_are_preserved_or_unknown_check_blocks_all(tmp_path_factory, manager_factory, required):
    repo = _repo(tmp_path_factory)
    store = StorageRuntime(repo, state_home=tmp_path_factory / "state")
    manager = manager_factory(repo, store)
    root = _process(manager, task_id="root")
    root.update_context_snapshot(lambda s: {**s, "project_name": "p"})
    child = _process(manager, parent_id=root.process_id, task_id="binary")
    try:
        work = Path(manager.materialize_process_worktree(child))
        (work / "app.txt").unlink()
        payload = b"\0binary\xffcandidate"
        (work / "output.bin").write_bytes(payload)
        child.status = ProcessStatus.COMPLETED
        manager.seal_process_worktree(child)
        root.child_reviews[child.process_id] = {"verdict": "approved"}
        LessonManager.bind_storage(repo, store)
        if required:
            lesson(repo, "output.bin", required=True)
            with pytest.raises(WorktreeError) as caught:
                manager.promote_process_results(root, [child.process_id])
            assert caught.value.diagnostic["error"] == "LESSON_CHECK_UNAVAILABLE"
            assert (repo / "app.txt").exists() and not (repo / "output.bin").exists()
        else:
            result = manager.promote_process_results(root, [child.process_id])
            assert result["promoted"] and not (repo / "app.txt").exists()
            assert (repo / "output.bin").read_bytes() == payload
            deleted = next(r for r in result["file_results"] if r["operation"] == "delete_file")
            assert deleted["after_sha256"] == "" and deleted["recovery_ref"].startswith("git:")
            created = next(r for r in result["file_results"] if r["operation"] == "write_file")
            assert "recovery_ref" not in created
    finally:
        _cleanup(store, manager, child)


def test_two_service_instances_cannot_reconcile_the_same_inflight_head(case, monkeypatch):
    repo, store, manager, root, a, b = case
    other = AgentWorktreeManager(repo, store)
    observed = []
    def competing_promotion():
        try:
            other.promote(snapshot_commit=a.worktree["snapshot_commit"], ordered_commits=[a.worktree["own_commit"]],
                          promotion_id=root.active_task_id)
        except WorktreeError as exc:
            observed.append(exc.diagnostic)
    observe_formal_apply(manager.worktree_manager, monkeypatch, competing_promotion)
    result = manager.promote_process_results(root, [a.process_id])
    assert result["promoted"] and observed[0]["error"] == "FILE_MUTATION_BUSY"
    assert (repo / "app.txt").read_text() == "upstream\n"


def test_exact_user_grant_uses_existing_broker_for_a_known_formal_violation(case):
    repo, store, manager, root, a, b = case
    from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
    lesson(repo, "result.txt", required=True)
    ctx, call = pipeline(case)
    args = {"process_ids": [b.process_id]}
    create_permission_request(root, {"purpose": "Accept this known exact diagnostic", "tool_name": "promote_agent_changes",
        "arguments": args}, ctx.artifacts["tool_catalog"], str(repo))
    grant_from_decision(root, root.pending_decision, "allow_once")
    result = call(args)
    assert not result.is_error, result.formatted
    assert result.receipt["lesson_user_override"] is True
    assert result.receipt["lesson_mandatory_check_state"] == "violation"
    assert (repo / "result.txt").read_text() == "consumer\n"


def test_pending_ack_preserves_verified_effect_and_retry_does_not_apply_twice(case, monkeypatch):
    repo, store, manager, root, a, b = case
    ctx, call = pipeline(case)
    with patch.object(store, "commit_state_revision", side_effect=fail_committed_ack(store)):
        first = call({"process_ids": [a.process_id]})
    assert first.data["error"] == "PROMOTION_METADATA_PENDING"
    assert first.receipt["effect_state"] == "committed" and not a.worktree["promoted"]
    assert (repo / "app.txt").read_text() == "upstream\n"
    def no_reapply():
        pytest.fail("reconciliation replayed the formal write")
    observe_formal_apply(manager.worktree_manager, monkeypatch, no_reapply)
    final = call({"process_ids": [a.process_id]}, 1)
    assert not final.is_error and final.data["reconciled_effect"] is True, final.formatted
    assert a.worktree["promoted"] and (repo / "app.txt").read_text() == "upstream\n"


def test_incremental_promotion_also_locks_previously_accepted_outputs(case, monkeypatch):
    repo, store, manager, root, a, b = case
    from backend.core.tools.workspace_tools import write_file
    manager.promote_process_results(root, [a.process_id])
    observations = []
    def competing_write():
        observations.append(write_file({"_workspace": str(repo), "_mutation_lock_root": str(store.paths.state_home / "mutation-locks"),
            "path": "app.txt", "content": "competing", "create_only": False,
            "expected_sha256": hashlib.sha256((repo / "app.txt").read_bytes()).hexdigest()}))
    observe_formal_apply(manager.worktree_manager, monkeypatch, competing_write)
    final = manager.promote_process_results(root, [b.process_id])
    assert final["promoted"] and observations[0]["error"] == "FILE_MUTATION_BUSY"
    assert (repo / "app.txt").read_text() == "upstream\n" and (repo / "result.txt").read_text() == "consumer\n"


def test_pinned_nonoutput_input_is_locked_during_formal_application(case, monkeypatch):
    repo, store, manager, root, a, b = case
    from backend.core.tools.workspace_tools import write_file
    source = repo / "reference.txt"
    source.write_bytes(b"pinned reference\n")
    a.worktree["input_dependencies"] = [{"path": "reference.txt", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]
    observations = []
    def competing_write():
        observations.append(write_file({"_workspace": str(repo), "_mutation_lock_root": str(store.paths.state_home / "mutation-locks"),
            "path": "reference.txt", "content": "changed reference", "create_only": False,
            "expected_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}))
    observe_formal_apply(manager.worktree_manager, monkeypatch, competing_write)
    result = manager.promote_process_results(root, [a.process_id])
    assert result["promoted"] and observations[0]["error"] == "FILE_MUTATION_BUSY"
    assert source.read_bytes() == b"pinned reference\n"


@pytest.mark.parametrize("ack_lost", [False, True])
def test_crlf_delivery_freezes_the_actual_materialized_bytes(tmp_path_factory, manager_factory, ack_lost):
    from tests.test_agent_dag_worktree import _git
    repo = _repo(tmp_path_factory)
    _git(repo, "config", "core.autocrlf", "true")
    (repo / "app.txt").write_bytes(b"base\r\n")
    if sys.platform == "darwin":
        # Protected worktrees start from an explicit immutable snapshot and do
        # not run project filters. Commit the intended raw baseline in this
        # test instead of pretending dirty-root capture is implemented.
        _git(repo, "-c", "core.autocrlf=false", "add", "app.txt")
        _git(repo, "commit", "-m", "raw CRLF snapshot")
    store = StorageRuntime(repo, state_home=tmp_path_factory / "state")
    manager = manager_factory(repo, store)
    root = _process(manager, task_id="root")
    child = _process(manager, parent_id=root.process_id, task_id="crlf")
    try:
        work = Path(manager.materialize_process_worktree(child))
        (work / "app.txt").write_bytes(b"next\r\n")
        child.status = ProcessStatus.COMPLETED
        manager.seal_process_worktree(child)
        root.child_reviews[child.process_id] = {"verdict": "approved"}
        if ack_lost:
            with patch.object(store, "commit_state_revision", side_effect=fail_committed_ack(store)):
                with pytest.raises(WorktreeError, match="METADATA_PENDING"):
                    manager.promote_process_results(root, [child.process_id])
            assert (repo / "app.txt").read_bytes() == b"next\r\n"
            (repo / "app.txt").write_bytes(b"next\n")
            # Git canonicalization sees identical content; the frozen raw
            # candidate does not. Recovery must not certify the drifted bytes.
            with pytest.raises(WorktreeError, match="AMBIGUOUS"):
                manager.promote_process_results(root, [child.process_id])
            assert not child.worktree["promoted"] and (repo / "app.txt").read_bytes() == b"next\n"
        else:
            result = manager.promote_process_results(root, [child.process_id])
            assert result["promoted"] and (repo / "app.txt").read_bytes() == b"next\r\n"
            assert result["file_results"][0]["after_sha256"] == hashlib.sha256(b"next\r\n").hexdigest()
            assert result["verification_basis"] == "raw_file_versions"
    finally:
        _cleanup(store, manager, child)


@pytest.mark.parametrize("guard_mode", ["none", "advice", "required"])
def test_rename_checks_the_deleted_source_as_well_as_the_new_target(tmp_path_factory, manager_factory, guard_mode):
    repo = _repo(tmp_path_factory)
    original_bytes = (repo / "app.txt").read_bytes()
    store = StorageRuntime(repo, state_home=tmp_path_factory / "state")
    manager = manager_factory(repo, store)
    root = _process(manager, task_id="root")
    root.update_context_snapshot(lambda s: {**s, "project_name": "p"})
    child = _process(manager, parent_id=root.process_id, task_id="rename")
    try:
        work = Path(manager.materialize_process_worktree(child))
        (work / "app.txt").rename(work / "moved.txt")
        child.status = ProcessStatus.COMPLETED
        manager.seal_process_worktree(child)
        root.child_reviews[child.process_id] = {"verdict": "approved"}
        LessonManager.bind_storage(repo, store)
        if guard_mode != "none":
            guard = Lesson(id="keep-source", project_name="p", trigger="app.txt", rule="Preserve compatibility for the original path")
            if guard_mode == "required":
                guard.dangerous_tools = ["delete_file"]
                guard.check = {"authority": "registered", "mode": "violation_pattern", "paths": ["app.txt"], "pattern": "^$"}
            LessonManager.save(repo, guard)
            with pytest.raises(WorktreeError) as caught:
                manager.promote_process_results(root, [child.process_id])
            expected = "KNOWN_LESSON_VIOLATION" if guard_mode == "required" else "LESSON_FEEDBACK_REQUIRED"
            assert caught.value.diagnostic["error"] == expected
            assert (repo / "app.txt").read_bytes() == original_bytes and not (repo / "moved.txt").exists()
            assert not child.worktree["promoted"]
        else:
            result = manager.promote_process_results(root, [child.process_id])
            assert result["promoted"] and not (repo / "app.txt").exists()
            assert (repo / "moved.txt").read_bytes() == original_bytes
            assert {row["path"]: row["operation"] for row in result["file_results"]} == {"app.txt": "delete_file", "moved.txt": "write_file"}
        assert set(child.worktree["changed_files"]) == {"app.txt", "moved.txt"}
    finally:
        _cleanup(store, manager, child)
