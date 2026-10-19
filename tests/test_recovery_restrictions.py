"""Committed Host restrictions survive database reopen and stale decision cards."""
import queue
from contextlib import closing

import pytest

from backend.core.daemon.dispatch import _cmd_task
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.recovery import restore_incomplete_processes
from backend.core.loop.tools import ToolRegistry


def _checkpoint(workspace, state_home, recovery, *, pending=False):
    manager = AgentProcessManager()
    process = manager.fork(parent_id=None, role="worker", tool_registry=ToolRegistry([]),
        max_steps=4, ring_level=RingLevel.RING_3, workspace_path=str(workspace),
        task_id="restriction-fixture", capability_profile_id="text.only")
    process.recovery = recovery
    if pending:
        process.status = ProcessStatus.AWAITING_USER
        process.pending_decision = {"decision_id": "old-card", "question": "Fixture choice",
                                    "options": [{"label": "Continue"}]}
        process.pending_tool_batch = {"fixture": "old-batch"}
        process.approval_grants = [{"grant_id": "old-grant"}]
    with closing(SessionStore(str(workspace), state_home=state_home)) as store:
        store.save_process_checkpoint(process)
    return process.process_id


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("restriction", [
    {"resume_forbidden": True, "reasons": [{"code": "HOST_REVIEW_REQUIRED"}]},
    {"checkpoint_commit": "unknown"},
    {"resume_forbidden": True, "checkpoint_commit": "unknown",
     "requires_manual_verification": True},
])
def test_committed_restriction_blocks_resume_and_old_card_after_repeated_reopen(
    tmp_path_factory, restriction, pending,
):
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    state_home = tmp_path_factory / "state"
    process_id = _checkpoint(workspace, state_home, restriction, pending=pending)
    previous_reasons = None
    for _ in range(2):
        with closing(SessionStore(str(workspace), state_home=state_home)) as store:
            manager = AgentProcessManager()
            candidates = restore_incomplete_processes(store, manager, workspace)
            process = manager.get(process_id)
            assert process.status is ProcessStatus.RECOVERY_REVIEW_REQUIRED
            assert candidates[0]["resume_forbidden"] is True
            assert process.recovery["requires_manual_verification"] is True
            if "checkpoint_commit" in restriction:
                assert process.recovery["checkpoint_commit"] == "unknown"
                assert {"code": "STORAGE_COMMIT_UNKNOWN"} in process.recovery["reasons"]
            for reason in restriction.get("reasons", []):
                assert reason in process.recovery["reasons"]
            if previous_reasons is not None:
                assert process.recovery["reasons"] == previous_reasons
            previous_reasons = process.recovery["reasons"]
            assert process.pending_decision is None
            assert process.pending_tool_batch is None and process.approval_grants == []
            revision = store.process_revision(process_id)
            context = {"apm": manager, "session_store": store, "evq": queue.Queue()}
            for verified in (False, True):
                emitted = []
                _cmd_task({"action": "resume", "process_id": process_id,
                           "manually_verified": verified, "verification_note": "Fixture review"},
                          None, None, context, emitted.append)
                assert emitted[-1]["error"].startswith("RECOVERY_RESUME_FORBIDDEN:")
                assert not manager._threads
                assert store.process_revision(process_id) == revision
            emitted = []
            _cmd_task({"action": "decision", "process_id": process_id,
                       "decision_id": "old-card", "instruction": "Choose option 1: Continue"},
                      None, None, context, emitted.append)
            assert emitted[-1]["error"].startswith("PROCESS_NOT_AWAITING_USER:")
            assert not manager._threads
            assert store.process_revision(process_id) == revision
            store.save_process_checkpoint(process)


@pytest.mark.parametrize("restriction", [
    {"requires_manual_verification": True},
    {"reasons": [{"code": "UNRESOLVED_EFFECT", "effect_state": "ambiguous"}]},
])
def test_pending_card_cannot_bypass_saved_manual_review(tmp_path_factory, restriction):
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    state_home = tmp_path_factory / "state"
    process_id = _checkpoint(workspace, state_home, restriction, pending=True)
    with closing(SessionStore(str(workspace), state_home=state_home)) as store:
        manager = AgentProcessManager()
        restore_incomplete_processes(store, manager, workspace)
        process = manager.get(process_id)
        assert process.status is ProcessStatus.RECOVERY_REVIEW_REQUIRED
        assert not process.recovery["resume_forbidden"]
        assert process.recovery["requires_manual_verification"]
        emitted = []
        context = {"apm": manager, "session_store": store, "evq": queue.Queue()}
        _cmd_task({"action": "resume", "process_id": process_id},
                  None, None, context, emitted.append)
        assert emitted[-1]["error"] == "RECOVERY_MANUAL_VERIFICATION_REQUIRED"
        _cmd_task({"action": "decision", "process_id": process_id,
                   "decision_id": "old-card", "instruction": "Choose option 1: Continue"},
                  None, None, context, emitted.append)
        assert emitted[-1]["error"].startswith("PROCESS_NOT_AWAITING_USER:")
        assert not manager._threads


def test_saved_and_new_recovery_findings_are_merged_across_reopen(tmp_path_factory):
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    state_home = tmp_path_factory / "state"
    reason = {"code": "HOST_REVIEW_REQUIRED"}
    process_id = _checkpoint(workspace, state_home,
        {"resume_forbidden": True, "reasons": [reason]}, pending=True)
    # A workspace-side legacy journal is untrusted, independently of saved facts.
    (workspace / ".gitgo" / "tool_invocations").mkdir(parents=True)
    with closing(SessionStore(str(workspace), state_home=state_home)) as store:
        manager = AgentProcessManager()
        restore_incomplete_processes(store, manager, workspace)
        process = manager.get(process_id)
        assert reason in process.recovery["reasons"]
        assert any(item["code"] == "LEGACY_INVOCATION_JOURNAL_UNTRUSTED"
                   for item in process.recovery["reasons"])
        assert process.recovery["resume_forbidden"]


def test_clean_saved_recovery_does_not_invent_a_restriction(tmp_path_factory):
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    state_home = tmp_path_factory / "state"
    process_id = _checkpoint(workspace, state_home,
        {"state": "resolved", "reasons": [], "resume_forbidden": False,
         "requires_manual_verification": False}, pending=True)
    with closing(SessionStore(str(workspace), state_home=state_home)) as store:
        manager = AgentProcessManager()
        restore_incomplete_processes(store, manager, workspace)
        process = manager.get(process_id)
        assert process.status is ProcessStatus.AWAITING_USER
        assert process.pending_decision["decision_id"] == "old-card"
        assert not process.recovery["resume_forbidden"]
        assert not process.recovery["requires_manual_verification"]
        assert process.recovery["reasons"] == []


def test_new_missing_host_evidence_blocks_an_old_card_without_saved_restrictions(tmp_path_factory):
    from backend.core.storage.invocation_journal import invocation_journal_root
    workspace = tmp_path_factory / "workspace"
    workspace.mkdir()
    state_home = tmp_path_factory / "state"
    process_id = _checkpoint(workspace, state_home, {}, pending=True)
    journal = invocation_journal_root(workspace, state_home=state_home)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("not a journal directory", encoding="utf8")
    with closing(SessionStore(str(workspace), state_home=state_home)) as store:
        manager = AgentProcessManager()
        restore_incomplete_processes(store, manager, workspace)
        process = manager.get(process_id)
        assert process.status is ProcessStatus.RECOVERY_REVIEW_REQUIRED
        assert process.recovery["resume_forbidden"]
        assert any(item["code"] == "INVOCATION_EVIDENCE_UNAVAILABLE"
                   for item in process.recovery["reasons"])
        assert process.pending_decision is None
        assert process.pending_tool_batch is None and not process.approval_grants
