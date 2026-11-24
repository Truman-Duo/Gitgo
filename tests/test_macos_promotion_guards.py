"""Native publication guards with real Git objects, SQLite and filesystem locks.

Broker admission is substituted by the existing ordinary-user fixture. These
tests exercise the configured ToolPipeline, not Dashboard or privileged service
installation. Crash ambiguity/replay coverage lives in test_macos_promotion.
"""
from pathlib import Path

import pytest

from backend.core.loop.worktree import AgentWorktreeManager, WorktreeError
from tests.test_macos_promotion import publication, record, run
from tests.test_macos_worktree_result import (
    ready, staged, case, create, repository, commit_fixture,
)
from tests.test_macos_worktree_upstream import wire_queries


def test_new_unavailable_check_after_exact_approval_still_blocks_publication(
    ready, monkeypatch, tmp_path_factory,
):
    from backend.core.knowledge.manager import LessonManager
    from backend.core.knowledge.models import Lesson
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.manager import AgentProcessManager
    from backend.core.loop.models import ProcessStatus, RingLevel
    from backend.core.loop.permission_broker import (
        automatic_permission_request, grant_from_decision,
    )
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.tools import ToolRegistry

    worktrees, baseline, *_ = ready
    project, store = worktrees.repo_root, worktrees.storage
    LessonManager.bind_storage(project, store)
    wire_queries(worktrees, monkeypatch, tmp_path_factory / "approved-queries")
    manager = AgentProcessManager(
        worktree_manager=worktrees, project_snapshot_commit=baseline.snapshot_commit,
    )
    supervisor = manager.fork(
        None, "supervisor", ToolRegistry(["review_child_outcome", "promote_agent_changes"]),
        8, RingLevel.RING_0, workspace_path=str(project), task_id="checked-publication",
        actor_kind="supervisor", task_kind="supervisor",
        capability_profile_id="supervisor.control",
    )
    supervisor.update_context_snapshot(lambda value: {**value, "project_name": "p"})
    child = manager.fork(
        supervisor.process_id, "executor", ToolRegistry([]), 4, RingLevel.RING_3,
        workspace_path=str(project), task_id="checked-child", task_kind="action",
        capability_profile_id="development.workspace",
    )
    lease = worktrees.create(
        process_id=child.process_id, task_id=child.active_task_id,
        snapshot_commit=baseline.snapshot_commit,
    )
    (Path(lease.path) / "a.bin").write_bytes(b"reviewed binary result\0\xff")
    worktrees.stage_index(lease, ["a.bin"])
    worktrees.publish_index(lease)
    worktrees.seal(lease)
    child.worktree = lease.to_dict()
    child.status = ProcessStatus.COMPLETED
    child.tool_receipts.append({
        "receipt_id": "sealed-fixture", "effect": "workspace_write",
        "succeeded": True, "committed": True,
    })
    catalog = build_internal_tools(supervisor, {}, str(project), None, None, None)
    ctx = ExecutionContext(
        supervisor, supervisor.session, str(project), storage=store,
        cancellation=supervisor.cancellation_event,
    )
    ctx.artifacts["tool_catalog"] = catalog
    pipeline = ToolPipeline()

    def call(name, args, key):
        return pipeline.execute({"name": name, "args": args}, catalog[name], ctx, key)

    reviewed = call("review_child_outcome", {
        "process_id": child.process_id, "verdict": "approved", "summary": "Reviewed binary",
    }, "review")
    assert reviewed.data["accepted"], reviewed.formatted
    args = {"process_ids": [child.process_id]}
    automatic_permission_request(supervisor, catalog["promote_agent_changes"], args, str(project))
    grant = grant_from_decision(supervisor, supervisor.pending_decision, "allow_once")
    assert grant is not None and grant["remaining_uses"] == 1
    supervisor.pending_decision = None

    # Known violations may be explicitly overridden. A newly required check
    # that cannot inspect these binary bytes must instead remain fail-closed.
    LessonManager.save(project, Lesson(
        id="new-binary-check", project_name="p", trigger="a.bin",
        rule="Inspect the exact published bytes", dangerous_tools=["write_file"],
        check={"authority": "registered", "mode": "violation_pattern",
               "paths": ["a.bin"], "pattern": "forbidden"},
    ))
    before = (project / "a.bin").read_bytes()
    blocked = call("promote_agent_changes", args, "approved-then-rule-changed")
    assert blocked.data["error"] == "LESSON_CHECK_UNAVAILABLE", blocked.formatted
    assert blocked.receipt["effect_state"] == "not_committed"
    assert grant["remaining_uses"] == 0
    assert (project / "a.bin").read_bytes() == before
    assert not child.worktree["promoted"]
    assert not store.list_state_refs("agent_promotion", prefix="")
    assert not list(project.rglob(".gitgo-promotion-*.tmp"))
    again = call("promote_agent_changes", args, "approval-was-spent")
    assert "SENSITIVE_TOOL_APPROVAL_REQUIRED" in again.formatted
    assert (project / "a.bin").read_bytes() == before


def test_native_publication_obeys_existing_file_writer_lock(publication):
    from backend.core.tools.file_mutation_batch import file_writer_locks

    manager, lease, _ = publication
    root = manager.repo_root
    before = (root / "a.bin").read_bytes()
    args = {"_mutation_lock_root": str(manager.storage.paths.state_home / "mutation-locks")}
    with file_writer_locks(root, [root / "a.bin"], args):
        with pytest.raises(WorktreeError) as caught:
            run(publication)
        assert caught.value.diagnostic["error"] == "FILE_MUTATION_BUSY"
        assert caught.value.diagnostic["effect_state"] == "not_committed"
        assert record(manager) is None and (root / "a.bin").read_bytes() == before
        assert not list(root.rglob(".gitgo-promotion-*.tmp"))
    result = run(publication)
    assert result["promoted"] and record(manager)["state"] == "committed"
    assert (root / "a.bin").read_bytes() == (Path(lease.path) / "a.bin").read_bytes()


def test_second_manager_cannot_enter_same_prepared_publication(publication, monkeypatch):
    from backend.core import macos_promotion_write as writer

    manager, lease, _ = publication
    other = AgentWorktreeManager(manager.repo_root, manager.storage)
    publish = writer.publish_file
    attempts = []

    def competing_publish(*args, **kwargs):
        before = record(manager)
        assert before["state"] == "prepared"
        with pytest.raises(WorktreeError) as caught:
            other.promote(
                snapshot_commit=lease.snapshot_commit, ordered_commits=[lease.result_commit],
                promotion_id="formal",
            )
        attempts.append(caught.value.diagnostic)
        assert attempts[-1]["error"] == "FILE_MUTATION_BUSY"
        assert attempts[-1]["effect_state"] == "not_committed"
        assert record(manager) == before
        return publish(*args, **kwargs)

    monkeypatch.setattr(writer, "publish_file", competing_publish)
    result = run(publication)
    assert result["promoted"] and len(attempts) == 1
    assert record(manager)["state"] == "committed"
    assert (manager.repo_root / "a.bin").read_bytes() == (Path(lease.path) / "a.bin").read_bytes()
    repeated = run(publication)
    assert repeated["already_promoted"] == [lease.result_commit]
    assert repeated["applied_commits"] == [] and len(attempts) == 1
    assert not list(manager.repo_root.rglob(".gitgo-promotion-*.tmp"))
