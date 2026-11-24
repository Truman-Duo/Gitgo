"""A fresh coordinator can find the same asset owner without a process ID."""
import io
import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.loop import executor
from backend.core.loop.asset_maintenance import AssetMaintenance, AssetMaintenanceError
from backend.core.loop.conversation_continuation import inspect_conversation_process
from backend.core.loop.manager import SessionStore
from backend.core.loop.model_feedback import contract_feedback, promotion_feedback
from backend.core.loop.models import ProcessStatus
from backend.core.loop.session import AgentSession
from backend.core.loop.tool_outcome_runtime import ToolOutcomeRuntime
from backend.core.loop.tool_result_support import _apply_host_task_transitions
from backend.core.native_host import NativeHost
from backend.core.storage.checkpoint_work import CheckpointPublication
from tests.test_agent_dag_worktree import _repo, worktree_case, _await_worktree_workers
from tests.test_native_host import FakeDaemon
from tests.test_temporary_subagent import _parent, _tools


def test_maintenance_hint_uses_current_revision_and_only_deduplicates_displayed_scopes():
    class Registry:
        ref = None
        reads = 0

        def get_state_ref(self, *_):
            return self.ref

        def read_blob(self, _):
            self.reads += 1
            return json.dumps(records).encode()

    records = {}
    storage = Registry()
    session = AgentSession()
    process = SimpleNamespace(actor_kind="supervisor", parent_id=None, _nudge_counters={},
        context_snapshot={}, steps_used=1, runtime_preferences={}, active_task_id="current",
        _manager=SimpleNamespace(session_store=SimpleNamespace(_storage=storage)))
    runtime = ToolOutcomeRuntime(process, session, None, lambda _: None, None, None)

    def observe(*paths):
        runtime._notify_maintained_files([SimpleNamespace(
            tool_name="read_file", data={"path": path}, is_error=False) for path in paths])

    paths = [f"consumer-{index}.py" for index in range(1, 10)]
    observe(paths[-1])
    assert not session.messages
    for index, path in enumerate(paths):
        asset = f"asset-{index}"
        records[asset] = {"asset_id": asset, "revision": 1, "name": asset, "paths": [path],
                          "owner": "worker-session", "owner_name": "Worker"}
    storage.ref = "first"
    observe(*paths)
    assert "1 additional" in session.messages[-1]["content"] and paths[-1] not in session.messages[-1]["content"]
    observe(paths[-1])
    assert paths[-1] in session.messages[-1]["content"]
    observe(*paths)
    assert len(session.messages) == 2 and storage.reads == 1
    records["asset-8"].update(revision=2, owner_name="Successor")
    storage.ref = "second"
    observe(paths[-1])
    assert len(session.messages) == 3 and "Successor" in session.messages[-1]["content"] and storage.reads == 2


def test_scope_revision_preserves_owner_and_delivery_coverage_and_retirement_releases_path(
        tmp_path_factory, worktree_case):
    repo = _repo(tmp_path_factory)
    storage, manager = worktree_case(repo, tmp_path_factory / "state")
    manager.session_store = SessionStore(repo, storage=storage)
    root = _parent(manager, repo, patch=True)
    tools = _tools(root, repo, object())
    service = AssetMaintenance(storage)
    asset = service.register(name="Application", commitment="Keep app compatible.", paths=["app.txt"],
                             decision="Original contract", owner="owner-session", owner_name="Owner",
                             source={"commit": "seed", "process_id": "prior"})
    asset_id = asset["asset_id"]
    other = service.register(name="Independent", commitment="Keep separate.", paths=["other.txt"],
                              decision="Separate contract", owner="project", owner_name="", source={})
    mutate = tools["asset_maintenance"].execute
    revise = {"operation": "revise", "asset_id": asset_id, "expected_revision": 1,
              "add_paths": ["consumer.txt"], "commitment": "Check app and consumer.", "decision": "Verified caller."}
    assert mutate({**revise, "add_paths": ["other.txt"]})["error"] == "ASSET_SCOPE_EXISTS"
    assert mutate(revise)["asset"]["revision"] == 2
    assert mutate(revise)["error"] == "ASSET_STATE_CHANGED"
    current = service.lookup("project", asset_id=asset_id)[0]
    assert current["owner"] == "owner-session" and current["paths"] == ["app.txt", "consumer.txt"]
    assert current["accepted_version"]["commit"] == "seed"
    assert current["accepted_version"]["scope_paths"] == ["app.txt"]
    assert current["scope_history"][0]["decision"] == "Original contract"
    assert not current["subscribed"] and next(iter(current["notices"].values()))["status"] == "queued"
    changed = mutate({**revise, "expected_revision": 2, "add_paths": [], "remove_paths": ["app.txt"],
                      "decision": "Old entry retired."})
    assert changed["asset"]["paths"] == ["consumer.txt"]
    restarted = AssetMaintenance(storage)
    historical = restarted.lookup("owner-session", asset_id=asset_id)[0]
    assert historical["revision"] == 3
    retire = {"operation": "retire", "asset_id": asset_id, "expected_revision": 3, "decision": "Consumer removed."}
    head, records = restarted.load()
    historical = records[asset_id]
    historical["work"] = {"status": "accepted"}
    restarted.save(head, records)
    assert mutate(retire)["error"] == "ASSET_SCOPE_BUSY"
    head, records = restarted.load()
    records[asset_id]["work"]["status"] = "delivered"
    restarted.save(head, records)
    assert mutate(retire)["asset"]["retired"] is True
    assert mutate({"operation": "wake", "asset_id": asset_id, "instruction": "Restart"})["error"] == "ASSET_RETIRED"
    assert restarted.lookup("project", query="consumer.txt") == []
    assert restarted.lookup("project", asset_id=asset_id)[0]["retired"]
    assert mutate({"operation": "register", "name": "Application", "paths": ["consumer.txt"],
                   "commitment": "New obligation.", "decision": "Replaced consumer."})["asset"]["asset_id"] != asset_id
    assert restarted.lookup("project", asset_id=other["asset_id"])

def test_asset_wake_activates_supervisor_completion_without_self_execution():
    supervisor = SimpleNamespace(actor_kind="supervisor", task_kind="answer", runtime_preferences={},
                                 context_snapshot={})
    result = SimpleNamespace(tool_name="asset_maintenance", is_error=False, data={"notice": {"status": "queued"}},
                             receipt={"effect": "process", "committed": True})
    assert not _apply_host_task_transitions(supervisor, [result])
    result.data = {"delegated": True, "process_id": "maintainer-execution"}
    assert _apply_host_task_transitions(supervisor, [result])
    assert supervisor.task_kind == "supervisor"


@pytest.mark.parametrize("delivery", ["complete", "changed", "rollback"])
def test_direct_delivery_and_asset_versions_share_the_terminal_checkpoint(
        tmp_path_factory, monkeypatch, worktree_case, delivery):
    repo = _repo(tmp_path_factory)
    storage, manager = worktree_case(repo, tmp_path_factory / "state")
    store = SessionStore(repo, storage=storage)
    manager.session_store = store
    root = _parent(manager, repo, patch=True)
    root.active_task_id = "direct"
    store.save_process_checkpoint(root)
    previous_revision = root.durable_revision
    service = AssetMaintenance(storage)
    files = {}
    for name in ("app.txt", "submit.txt"):
        asset = service.register(name=name, commitment="Preserve the booking contract.", paths=[name],
                                 decision="Initial rule", owner=f"{name}-session", owner_name=name,
                                 source={"process_id": name, "task_id": "old", "commit": "old-commit"})
        text = b"current\n"
        (repo / name).write_bytes(text)
        digest = sha256(text).hexdigest()
        files[asset["asset_id"]] = name, digest
        root.tool_receipts.append({
            "receipt_id": f"edit-{name}", "task_id": root.active_task_id, "process_id": root.process_id,
            "tool_name": "edit_file", "succeeded": True, "committed": True, "effect": "workspace_write",
            "effect_state": "committed", "file_service_result": True, "files": [name], "after_sha256": digest,
        })
        if name == "submit.txt":
            root.tool_receipts[-1].update(after_sha256="b" * 64, file_mutations=[
                {"path": name, "effect_state": "committed", "after_sha256": digest},
            ])
    root.tool_receipts.append({**root.tool_receipts[0], "receipt_id": "another-execution", "process_id": "other",
                               "after_sha256": "c" * 64})
    old_head, old_assets = service.load()
    tool = _tools(root, repo, object())["asset_status"]
    selected = next(iter(files))
    result = tool.execute({"asset_id": selected})
    visible = tool.model_result({"asset_id": selected}, deepcopy(result))
    assert result["assets"][0]["decision"] == "Initial rule"
    assert visible["assets"][0]["registration_decision"] == "Initial rule"
    assert visible["assets"][0]["current_task_write_paths"] == ["app.txt"]
    assert "precedes the terminal checkpoint" in visible["delivery_timing"]
    root.status = ProcessStatus.COMPLETED
    root.result = {"status": "completed", "metadata": {}}
    if delivery == "changed":
        (repo / "submit.txt").write_bytes(b"edited outside the task\n")
        with pytest.raises(AssetMaintenanceError, match="Cannot match submit.txt"):
            store.save_process_checkpoint(root, lifecycle_finalized=True, record_asset_versions=True)
    elif delivery == "rollback":
        original_apply = CheckpointPublication.apply

        def fail_after_writes(plan, connection):
            original_apply(plan, connection)
            raise RuntimeError("checkpoint interrupted")

        monkeypatch.setattr(CheckpointPublication, "apply", fail_after_writes)
        with pytest.raises(RuntimeError, match="checkpoint interrupted"):
            store.save_process_checkpoint(root, lifecycle_finalized=True, record_asset_versions=True)
    else:
        store.save_process_checkpoint(root, lifecycle_finalized=True, record_asset_versions=True)
        assert root.durable_revision == previous_revision + 1
        assert len(root.result["metadata"]["asset_deliveries"]) == 2
        for asset_id, (path, digest) in files.items():
            accepted = service.lookup("project", asset_id=asset_id)[0]
            assert accepted["owner"] == f"{path}-session"
            assert accepted["revision"] == 1 and accepted["notices"] == {}
            assert accepted["decision"] == "Initial rule"
            assert accepted["accepted_version"]["basis"] == "workspace_delivery"
            assert accepted["accepted_version"]["base_commit"] == "old-commit"
            assert accepted["accepted_version"]["files"][path]["sha256"] == digest
            assert accepted["accepted_version"]["task_id"] == root.active_task_id
        return
    assert root.durable_revision == previous_revision
    assert storage.process_revision(root.process_id) == previous_revision
    assert service.load() == (old_head, old_assets)
    assert root.result["metadata"] == {}


@pytest.mark.parametrize(("transfer_owner", "route"), [
    (False, "asset"), (True, "asset"), (False, "continuation"), (False, "owner_name"),
    (False, "retry"), (False, "scope_blocked"),
])
def test_cold_lookup_notice_adoption_and_versioned_delivery(
        tmp_path_factory, monkeypatch, worktree_case, transfer_owner, route):
    repo = _repo(tmp_path_factory)
    storage, first = worktree_case(repo, tmp_path_factory / "state")
    first.session_store = SessionStore(repo, storage=storage)
    root = _parent(first, repo, patch=True)
    seen = []
    failed_execution = []

    def run_agent(process, workspace_path, **_):
        binding = process.runtime_preferences.get("asset_execution")
        if binding:
            if route == "retry" and not failed_execution:
                (Path(workspace_path) / "app.txt").write_text("retained before failure\n", encoding="utf-8")
                failed_execution.append(process.process_id)
                process.status = ProcessStatus.FAILED
                process.result = {"status": "failed", "code": "CHILD_LIFECYCLE_FAILED"}
                return process.result
            if route in {"retry", "scope_blocked"} and failed_execution:
                assert (Path(workspace_path) / "app.txt").read_text(encoding="utf-8") == "retained before failure\n"
                assert not (Path(workspace_path) / "outside.txt").exists()
            service = AssetMaintenance(storage)
            with pytest.raises(AssetMaintenanceError, match="Accept the asset work"):
                service.require_accepted(process)
            child_tools = _tools(process, workspace_path, object())
            asset = child_tools["asset_status"].execute({"asset_id": binding["asset_id"]})["assets"][0]
            notice = next(iter(asset["notices"].values()))
            if route == "scope_blocked" and failed_execution:
                assert notice["status"] == "acknowledged"
                assert process.mailbox.get(notice["notice_id"]) is None
            else:
                assert notice["status"] == "delivered"
                assert process.mailbox.get(notice["notice_id"]).kind == "coordination_update"
                update = child_tools["asset_maintenance"].execute({
                    "operation": "acknowledge_notice", "notice_id": notice["notice_id"]})
                assert update["asset"]["work_state"] == "offered"
            seen.append(asset["decision"])
            assert asset["work"]["status"] == "offered"
            accepted = child_tools["asset_maintenance"].execute({"operation": "accept_work"})
            assert accepted["asset"]["work_state"] == "accepted"
            service.require_accepted(process)
            if route == "scope_blocked" and not failed_execution:
                from backend.core.loop.execution_context import ExecutionContext
                from backend.core.loop.tool_pipeline import ToolPipeline
                from backend.core.tools.catalog import build_workspace_tools

                workspace = Path(workspace_path)
                context = ExecutionContext(process, process.session, str(workspace))
                tool = build_workspace_tools(workspace)["write_file"]
                blocked = ToolPipeline().execute({"name": "write_file", "args": {
                    "path": "outside.txt", "content": "outside the assignment",
                }}, tool, context, "asset-known-outside-path")
                assert blocked.receipt["error_code"] == "ASSET_WRITE_OUTSIDE_SCOPE", blocked.formatted
                assert blocked.receipt["effect_state"] == "not_committed"
                assert not (workspace / "outside.txt").exists()
                (workspace / "app.txt").write_text("retained before failure\n", encoding="utf-8")
                # Emulate an opaque tool mutation. The worktree seal is the
                # final authority even when a tool provides no named path.
                (workspace / "outside.txt").write_text("must stay private\n", encoding="utf-8")
                failed_execution.append(process.process_id)
                process.status = ProcessStatus.COMPLETED
                process.result = {"status": "completed", "response": "Attempted a broader patch."}
                return process.result
        if process.task_kind == "action":
            Path(workspace_path, "app.txt").write_text("revision two\n" if binding else "revision one\n", encoding="utf-8")
            process.tool_receipts.append({"receipt_id": "edit-" + process.process_id, "tool_name": "edit_file",
                                          "succeeded": True, "committed": True, "effect": "workspace_write"})
        process.status = ProcessStatus.COMPLETED
        process.result = {"status": "completed", "response": "Prepared."}
        return process.result

    monkeypatch.setattr(executor, "agent_step", run_agent)
    tools = _tools(root, repo, object())
    original = tools["delegate_task"].execute({"task_description": "Initial implementation", "target_files": ["app.txt"],
                                              "display_name": "Calendar Maintainer",
                                              "acceptance_criteria": ["A verified initial version"]})
    assert original["delegated"], original
    original_id = original["process_id"]
    _await_worktree_workers(first, [original_id])
    assert tools["review_child_outcome"].execute({"process_id": original_id, "verdict": "approved", "summary": "Reviewed."})["accepted"]
    initial = tools["promote_agent_changes"].execute({"process_ids": [original_id]})
    assert initial["promoted"], initial
    registered = tools["asset_maintenance"].execute({"operation": "register", "name": "Booking compatibility",
        "commitment": "Keep the public booking result compatible.", "paths": ["app.txt"],
        "process_id": original_id, "decision": "Use the actual end time."})
    asset_id = registered["asset"]["asset_id"]
    persisted = tools["asset_status"].execute({"asset_id": asset_id})["assets"][0]
    assert persisted["accepted_version"]["commit"] == initial["result_commit"]
    assert persisted["accepted_version"]["basis"] == "integrated"
    root.status = ProcessStatus.COMPLETED
    first.session_store.save_process_checkpoint(root, lifecycle_finalized=True)
    first.session_store.save_process_checkpoint(first.get(original_id), lifecycle_finalized=True)
    tools["asset_maintenance"].execute({"operation": "subscribe", "asset_id": asset_id})
    notify = {"operation": "notify", "asset_id": asset_id, "version": "booking-v2", "message": "New booking rule"}
    pending = tools["asset_maintenance"].execute(notify)
    assert pending["notice"]["status"] == "queued"
    assert tools["asset_maintenance"].execute(notify) == pending
    after_notice = tools["asset_status"].execute({"asset_id": asset_id})["assets"][0]
    assert after_notice["accepted_version"] == persisted["accepted_version"]
    assert tuple(root.coordination_snapshot()[0]) == (original_id,)

    storage, cold = worktree_case(repo, tmp_path_factory / "state")
    cold.session_store = SessionStore(repo, storage=storage)
    if route == "asset" and not transfer_owner:
        observation = _parent(cold, repo)
        runtime = ToolOutcomeRuntime(observation, observation.session, None, lambda _: None, None, None)
        unrelated = SimpleNamespace(tool_name="read_file", data={"path": "another.txt"}, is_error=False)
        match = SimpleNamespace(tool_name="read_file", data={"path": "app.txt"}, is_error=False)
        runtime.notify_evidence_ready([unrelated])
        runtime.notify_evidence_ready([match])
        runtime.notify_evidence_ready([match])
        hints = [m for m in observation.session.messages if m.get("steering_type") == "maintained_files"]
        assert len(hints) == 1 and asset_id in hints[0]["content"]
        assert not observation.coordination_snapshot()[0]
    if route == "continuation":
        former = inspect_conversation_process(cold, cold.session_store, repo, root.process_id)
        coordinator = cold.fork(parent_id=None, role="supervisor", actor_kind="supervisor",
            capability_profile_id=root.capability_profile_id, task_kind=root.task_kind, task_id="restart-turn",
            tool_registry=root.tool_registry, ring_level=root.ring_level, max_steps=20, workspace_path=str(repo),
            context_snapshot=root.read_context_snapshot()[0], session=former.session)
        cold.inherit_terminal_coordination(coordinator, former)
    else:
        coordinator = _parent(cold, repo, patch=True)
    coordinator.active_task_id = "restart-turn"
    tools = _tools(coordinator, repo, object())
    declaration = tools["declare_task_contract"].execute({"operation": "revise",
        "goal": "Adjust remaining time", "acceptance_criteria": ["Check the remaining-time boundary"]})
    assert declaration["accepted"], declaration
    feedback = contract_feedback({}, declaration)
    projected = feedback["maintained_assets"]
    assert projected == [{"asset_id": asset_id, "name": "Booking compatibility", "owner": "Calendar Maintainer", "revision": 1,
                          "paths": ["app.txt"], "covers_required_files": True}]
    assert "if you keep its current route" in feedback["asset_routing_next"]
    assert declaration["routing_compilation"]["effective"] == declaration["contract"]["execution_mode"]
    assert declaration["authority_granted"] is False
    found = tools["asset_status"].execute({"query": "booking"})["assets"][0]
    assert found["asset_id"] == asset_id and found["owner"] == original["session_id"]
    assert found["pending_notices"] == 1
    if route != "continuation":
        assert not cold.get(original_id)
    host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
    host.services = SimpleNamespace(project_list=lambda: [{"name": "booking", "workspace": str(repo)}],
                                    provider_status=lambda: {"providers": [], "active_provider": ""})
    host._storage_projections["booking"] = storage
    try:
        status = host._runtime_status("booking")
        assert status["asset_maintenance"] == [{**found, "execution_active": False}]
        assert status["processes"][original_id]["maintained_assets"] == status["asset_maintenance"]
    finally:
        host.close()
    request = {"operation": "wake", "asset_id": asset_id, "instruction": "Adjust the remaining-time behavior."}
    if route in {"asset", "retry", "scope_blocked"}:
        written = {"task_id": coordinator.active_task_id, "process_id": coordinator.process_id,
                   "receipt_id": "write-matched-scope", "tool_name": "edit_file", "succeeded": True, "committed": True,
                   "file_service_result": True, "effect": "workspace_write", "effect_state": "committed",
                   "files": ["app.txt"], "after_sha256": "a" * 64}
        coordinator.tool_receipts.append(written)
        late_wake = tools["asset_maintenance"].execute(request)
        assert late_wake["error"] == "ASSET_FOLLOW_UP_REQUIRED" and late_wake["effect_state"] == "not_committed"
        coordinator.tool_receipts.remove(written)
        wake = tools["asset_maintenance"].execute(request)
    else:
        selection = ({"continue_process_id": original_id, "continuation_mode": "follow_up"} if route == "continuation"
                     else {"display_name": "Calendar Maintainer"})
        if route == "continuation":
            # A sibling may already have pinned this new task's checkout.
            coordinator.worktree["snapshot_commit"] = initial["result_commit"]
        wake = tools["delegate_task"].execute({"task_description": request["instruction"], "target_files": ["app.txt"],
            "acceptance_criteria": ["Verify remaining time"], **selection})
    assert wake["delegated"], wake
    old = wake["process_id"]
    if route in {"asset", "retry", "scope_blocked"}:
        assert coordinator.delegated_contracts[old]["acceptance_criteria"] == ["Check the remaining-time boundary"]
    assert wake["session_id"] == original["session_id"] and old != original_id
    if route in {"retry", "scope_blocked"}:
        _await_worktree_workers(cold, [old])
        assert cold.get(old).status == ProcessStatus.FAILED and failed_execution == [old], cold.get(old).result
        retry = request
        if route == "scope_blocked":
            failed = cold.get(old)
            assert failed.worktree["state"] == "scope_blocked" and not failed.worktree.get("result_commit")
            assert (repo / "app.txt").read_text(encoding="utf-8") == "revision one\n"
            assert not (repo / "outside.txt").exists()
            listed = tools["list_agents"].execute({})
            call, = [item["asset_recovery_call"] for item in tools["list_agents"].model_result({}, listed)["agents"]
                     if item.get("asset_recovery_call")]
            waited = tools["wait_agents"].execute({"process_ids": [old], "timeout": 1})
            assert tools["wait_agents"].model_result({}, waited)["agents"][old]["asset_recovery_call"] == call
            retry = call["arguments"]
            completion = tools["complete_supervision"].execute({"result": "Ready to deliver."})
            assert not completion["accepted"] and completion["error"] == "ASSET_RECOVERY_REQUIRED"
            assert completion["error_info"]["next_actions"][0]["arguments"] == retry
            detached = tools["delegate_task"].execute({"task_description": "Retry outside asset routing",
                "target_files": ["app.txt"], "acceptance_criteria": ["Finish the original work"],
                "continue_process_id": old, "continuation_mode": "remaining"})
            assert not detached["delegated"] and detached["error"] == "ASSET_DELIVERY_REQUIRED"
        replacement = tools["asset_maintenance"].execute(retry)
        assert replacement["delegated"], replacement
        assert replacement["session_id"] == wake["session_id"]
        assert coordinator.delegated_contracts[old]["superseded_by"] == replacement["process_id"]
        assert coordinator.delegated_contracts[replacement["process_id"]]["supersedes_process_id"] == old
        old = replacement["process_id"]
    assert tools["asset_maintenance"].execute(request)["process_id"] == old
    _await_worktree_workers(cold, [old])
    assert cold.get(old).status == ProcessStatus.COMPLETED, cold.get(old).result
    assert seen == ["Use the actual end time."] * (2 if route == "scope_blocked" else 1)
    assert tools["asset_status"].execute({"query": "app.txt"})["assets"][0]["pending_notices"] == 0
    wait_tool = tools["wait_agents"]
    waited = wait_tool.execute({"process_ids": [old], "timeout": 1})
    delivery = waited["agents"][old]["delivery_call"]
    ready = wait_tool.model_result({}, waited)
    assert ready["delivery_review_tool"] == delivery["tool"]
    ready_result, = ready["ready_deliveries"]
    assert ready_result["result_ref"] == delivery["arguments"]["result_ref"]
    assert ready_result["changed_files"] == ["app.txt"]
    missing_instruction = tools["asset_maintenance"].execute({**request, "instruction": ""})
    assert missing_instruction["error"] == "ASSET_WORK_INSTRUCTION_REQUIRED"
    assert "in instruction" in missing_instruction["detail"]
    list_tool = tools["list_agents"]
    listed = list_tool.execute({})
    assert any(item["result_ref"] == ready_result["result_ref"]
               for item in list_tool.model_result({}, listed)["ready_deliveries"])
    reviewed_result = {**delivery["arguments"], "review": {"summary": "Reviewed the sealed result."}}

    if not transfer_owner:
        # Project changed after the worker's snapshot. The canonical promotion
        # must leave it untouched and allow an exact retry once reconciled.
        (repo / "app.txt").write_text("independent edit\n", encoding="utf-8")
        blocked = tools["promote_agent_changes"].execute(reviewed_result)
        assert not blocked.get("promoted"), blocked
        failed = tools["asset_status"].execute({"asset_id": asset_id})["assets"][0]
        assert "delivery_pending" not in failed and failed["work"]["status"] == "accepted"
        assert (repo / "app.txt").read_text(encoding="utf-8") == "independent edit\n"
        (repo / "app.txt").write_text("revision one\n", encoding="utf-8")
        delivered = tools["promote_agent_changes"].execute(reviewed_result)
        assert delivered["promoted"], delivered
        current = tools["asset_status"].execute({"asset_id": asset_id})["assets"][0]
        assert current["work"]["status"] == "delivered"
        assert current["accepted_version"]["commit"] == delivered["result_commit"]
        if route == "scope_blocked":
            assert current["owner"] == original["session_id"]
            assert cold.get(old).worktree["resumed_from_process_id"] == failed_execution[0]
            assert not (repo / "outside.txt").exists()
            complete = tools["complete_supervision"].execute({"result": "Delivered from the retained assignment."})
            assert complete["accepted"] and complete.get("terminal_status") != "failed"
        assert (repo / "app.txt").read_text(encoding="utf-8") == "revision two\n"
        receipt = promotion_feedback({}, delivered)
        assert receipt["asset_delivery"]["commit"] == delivered["result_commit"]
        assert receipt["after_delivery"]["next_action"] == "complete_supervision"
        applied = wait_tool.execute({"process_ids": [old], "timeout": 1})
        assert "ready_deliveries" not in wait_tool.model_result({}, applied)
        child_ids = coordinator.coordination_snapshot()[0]
        extra = tools["asset_maintenance"].execute({**request, "instruction": "Confirm the new version again"})
        assert extra["error"] == "ASSET_FOLLOW_UP_REQUIRED"
        assert extra["effect_state"] == "not_committed"
        assert coordinator.coordination_snapshot()[0] == child_ids
        host = NativeHost(stdout=io.StringIO(), daemon_factory=FakeDaemon)
        host.services = SimpleNamespace(project_list=lambda: [{"name": "booking", "workspace": str(repo)}],
                                        provider_status=lambda: {"providers": [], "active_provider": ""})
        host._storage_projections["booking"] = storage
        monkeypatch.setattr(type(storage), "read_project_b_processes", lambda _: {
            old: {"process_id": old, "status": "completed", "closeout_state": "pending", "execution_active": True},
        })
        try:
            status = host._runtime_status("booking")
            assert status["asset_maintenance"][0]["work_state"] == "delivered"
            assert status["asset_maintenance"][0]["execution_active"] is False
        finally:
            host.close()
        return

    successor = tools["delegate_task"].execute({"task_description": "Review future responsibility", "target_files": [],
        "acceptance_criteria": [], "task_kind": "answer", "capability_profile_id": "governance.observe",
        "required_for_parent_completion": False})
    assert successor["delegated"], successor
    _await_worktree_workers(cold, [successor["process_id"]])
    transfer = tools["asset_maintenance"].execute({"operation": "offer_transfer", "asset_id": asset_id,
        "process_id": successor["process_id"]})
    assert transfer["asset"]["owner"] == original["session_id"] and transfer["asset"]["transfer_pending"]
    assert tools["asset_maintenance"].execute({"operation": "accept_transfer", "asset_id": asset_id})["error"] == "ASSET_ROLE_REQUIRED"
    accepted = _tools(cold.get(successor["process_id"]), repo, object())["asset_maintenance"].execute(
        {"operation": "accept_transfer", "asset_id": asset_id})
    assert accepted["asset"]["revision"] == 2 and accepted["asset"]["owner"] == successor["session_id"]
    review = tools["review_child_outcome"].execute({**delivery["arguments"], "verdict": "approved", "summary": "Old candidate."})
    assert review["accepted"], review
    stale = tools["promote_agent_changes"].execute({"process_ids": [old]})
    assert stale["error"] == "ASSET_EXECUTION_STALE", stale
    assert (repo / "app.txt").read_text(encoding="utf-8") == "revision one\n"
    tools["asset_maintenance"].execute({"operation": "unsubscribe", "asset_id": asset_id})
    assert tools["asset_maintenance"].execute({**notify, "version": "booking-v3"})["notice"]["status"] == "unsubscribed"
