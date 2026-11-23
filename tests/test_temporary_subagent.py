"""Temporary helpers share execution machinery without inheriting responsibility."""
import json
from pathlib import Path
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest

from backend.core.loop import executor
from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.completion_protocol import HostCompletionEvaluator
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tool_surface import _select_authorized_tools
from backend.core.loop.tools import ToolRegistry
from backend.core.tools.catalog import build_workspace_tools
from tests.test_agent_dag_worktree import _repo, worktree_case


def _parent(manager, workspace, *, patch=False):
    contract = {"revision": 1, "execution_mode": "delegate", "delegation_required": True,
                "minimum_delegated_outcomes": 1, "deliverables": [
        {"kind": "workspace_file", "path": "app.txt"},
    ]} if patch else {}
    return manager.fork(
        parent_id=None, role="supervisor", actor_kind="supervisor", task_id="parent-task",
        capability_profile_id="supervisor.control", task_kind="supervisor" if patch else "answer",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("supervisor.control")),
        ring_level=RingLevel.RING_0, max_steps=20, workspace_path=str(workspace),
        context_snapshot={"task_contract": contract, "brief": "Parent-only private context."},
    )


def _tools(parent, workspace, provider, on_stream_event=None):
    catalog = build_workspace_tools(workspace)
    return executor._build_internal_tools(parent, catalog, str(workspace), provider,
                                          SimpleNamespace(_executors=catalog), on_stream_event)


@pytest.mark.parametrize("batch", [False, True])
def test_investigation_uses_real_child_loop_and_returns_only_summary(tmp_path_factory, batch):
    tmp_path = tmp_path_factory
    manager = AgentProcessManager()
    parent = _parent(manager, tmp_path)
    prompts = []

    class Provider:
        def stream_chat(self, messages, *args, **kwargs):
            prompts.append(json.dumps(messages, ensure_ascii=False))
            yield {"choices": [{"delta": {"content": "Independent conclusion"}}]}

    tools = _tools(parent, tmp_path, Provider())
    available, _ = _select_authorized_tools(parent, tools)
    assert "run_subagent" in available
    tool = available["run_subagent"]
    context = ExecutionContext(parent, parent.session, str(tmp_path))
    context.artifacts["tool_catalog"] = available
    request = {"task": "Check the reasoning independently.", "inputs": ["app.txt"]}
    arguments = {"tasks": [request]} if batch else request
    pipeline = ToolPipeline()
    spoofed = pipeline.execute({"name": tool.name, "args": {**arguments, "_allowed_roots": ["outside"]}},
                               tool, context, "forged-subagent-scope")
    assert spoofed.receipt["error_code"] == "SANDBOX_POLICY_INVALID"
    run = pipeline.execute({"name": tool.name, "args": arguments}, tool, context, "subagent")
    assert not run.is_error, run.formatted
    result = run.data["results"][0] if batch else run.data
    child = manager.get(result["process_id"])
    assert result["status"] == "completed", json.dumps(child.result, ensure_ascii=False, default=str)
    assert result["summary"] == "Independent conclusion"
    assert child.session is not parent.session
    assert child.parent_id == parent.process_id
    assert child.task_budget is parent.task_budget
    assert child.lifecycle_finalized and child.budget_lease["state"] == "released"
    assert child.role == "subagent"
    assert all("Parent-only private context." not in prompt for prompt in prompts)
    assert any("app.txt" in prompt for prompt in prompts)
    assert not child.tool_registry.has("run_subagent")
    assert not child.tool_registry.has("write_file")


@pytest.mark.parametrize("out_of_scope", [False, True])
def test_patch_freezes_only_approved_files_and_parent_applies_exact_result(
    tmp_path_factory, monkeypatch, worktree_case, out_of_scope,
):
    tmp_path = tmp_path_factory
    repo = _repo(tmp_path)
    storage, manager = worktree_case(repo, tmp_path / "state")
    manager.session_store = SessionStore(repo, storage=storage)
    parent = _parent(manager, repo, patch=True)

    def make_patch(process, workspace_path, **_):
        assert Path(workspace_path) != repo
        Path(workspace_path, "app.txt").write_text("candidate\n", encoding="utf-8")
        if out_of_scope:
            Path(workspace_path, "extra.txt").write_text("outside scope\n", encoding="utf-8")
        process.tool_receipts.append({"receipt_id": "substantiated-write", "tool_name": "edit_file",
            "succeeded": True, "committed": True, "effect": "workspace_write"})
        process.status = ProcessStatus.COMPLETED
        process.result = {"status": "completed", "response": "Prepared the change."}
        return process.result

    # The worker's claim and receipt are simulated; the actual Git snapshots,
    # scope check, cleanup, approval and promotion are exercised below.
    monkeypatch.setattr(executor, "agent_step", make_patch)
    tools = _tools(parent, repo, object())
    rejected = tools["run_subagent"].execute({"task": "Patch.", "write_files": ["extra.txt"]})
    assert rejected["error_info"]["name"] == "SUBAGENT_WRITE_SCOPE_REQUIRED"
    result = tools["run_subagent"].execute({"task": "Patch app.txt.", "write_files": ["app.txt"]})
    child = manager.get(result["process_id"])
    assert child.worktree["state"] == "disposed"
    assert not Path(child.worktree["path"]).exists()
    assert child.process_id not in storage.read_project_b_processes()
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\n"
    if out_of_scope:
        assert result["child_error_code"] == "ASSIGNMENT_HANDOFF_DELIVERY_OUTSIDE_SCOPE"
        assert "result_ref" not in result
        assert not (repo / "extra.txt").exists()
        return
    assert result["changed_files"] == ["app.txt"], result
    assert storage.load_worktree_state(child.process_id)["retained_result"]
    ref = result["result_ref"]
    artifact = tools["read_child_artifact"].execute({"result_ref": ref, "path": "app.txt"})
    assert artifact["content"] == "candidate\n"
    context, _ = parent.read_context_snapshot()
    arguments = {"result_ref": ref, "review": {"summary": "Checked the pinned app.txt change."}}
    context["task_contract"].update(revision=2, acceptance_criteria=["A materially different result"])
    parent.replace_context_snapshot(context)
    stale = tools["promote_agent_changes"].execute(arguments)
    assert stale["error_info"]["name"] == "SUBAGENT_RESULT_STALE"
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\n"
    context["task_contract"].pop("acceptance_criteria")
    context["task_contract"].update(revision=3, plan=["Review the sealed result"])
    parent.replace_context_snapshot(context)
    applied = tools["promote_agent_changes"].execute(arguments)
    assert applied["promoted"], applied
    assert (repo / "app.txt").read_text(encoding="utf-8") == "candidate\n"
    duplicate = tools["promote_agent_changes"].execute(arguments)
    assert duplicate["error_info"]["name"] == "SUBAGENT_RESULT_ALREADY_APPLIED"
    context, _ = parent.read_context_snapshot()
    context["task_contract"]["runtime_projection"] = {"delivery": "committed"}
    parent.replace_context_snapshot(context)
    outcome = HostCompletionEvaluator.evaluate(parent, "Delivered the confirmed change.")
    assert not any("REQUIRED_DELEGATION_UNSATISFIED" in reason for reason in outcome.reasons)
    context["task_contract"].update(revision=4, acceptance_criteria=["A materially different result"])
    parent.replace_context_snapshot(context)
    revised = HostCompletionEvaluator.evaluate(parent, "Delivered the confirmed change.")
    assert any("REQUIRED_DELEGATION_UNSATISFIED" in reason for reason in revised.reasons)


def test_parent_cancellation_releases_the_helper_without_consuming_late_result(tmp_path_factory, monkeypatch):
    tmp_path = tmp_path_factory
    manager = AgentProcessManager()
    parent = _parent(manager, tmp_path)
    entered = Event()

    def cancellable_loop(process, **_):
        entered.set()
        process.cancellation_event.wait(5)
        process.status = ProcessStatus.CANCELLED
        process.result = {"status": "cancelled", "response": "Late response"}
        return process.result

    monkeypatch.setattr(executor, "agent_step", cancellable_loop)
    tools = _tools(parent, tmp_path, object())
    results = []
    caller = Thread(target=lambda: results.append(tools["run_subagent"].execute({"task": "Check something."})))
    caller.start()
    try:
        assert entered.wait(5)
        overlapping = tools["run_subagent"].execute({"tasks": [{"task": "Overlapping batch"}]})
        assert overlapping["error_info"]["name"] == "SUBAGENT_BATCH_IN_PROGRESS"
        assert len(manager.children_of(parent.process_id)) == 1
        manager.kill(parent.process_id)
    finally:
        caller.join(10)
    assert not caller.is_alive()
    result, = results
    assert result["error_info"]["name"] == "SUBAGENT_CANCELLED", result
    assert "summary" not in result
    child = manager.get(result["process_id"])
    assert child.lifecycle_finalized and child.budget_lease["state"] == "released"


@pytest.mark.parametrize(("subprocess", "batch_size", "round_limit"), [(False, 5, 3), (True, 3, 2)])
def test_full_batches_overlap_and_turn_limit_counts_batches(tmp_path_factory, monkeypatch,
                                                           subprocess, batch_size, round_limit):
    manager = AgentProcessManager()
    root = _parent(manager, tmp_path_factory)
    owner = manager.fork(
        parent_id=root.process_id, role="executor", actor_kind="worker", task_id="worker-task",
        ring_level=RingLevel.RING_3, max_steps=20, workspace_path=str(tmp_path_factory),
        task_kind="answer", capability_profile_id="governance.observe",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("governance.observe")),
    ) if subprocess else root

    barrier = Barrier(batch_size, timeout=10)

    def concurrent_helper(process, **_):
        process.task_budget.begin_provider_call(process.process_id)
        barrier.wait()
        process.status = ProcessStatus.COMPLETED
        process.result = {"status": "completed", "response": process.task_description.splitlines()[0]}
        return process.result

    monkeypatch.setattr(executor, "agent_step", concurrent_helper)
    mounted = _tools(owner, tmp_path_factory, object())
    available, _ = _select_authorized_tools(owner, mounted)
    tool = available["run_subagent"]
    asks = [{"task": f"Question {index}"} for index in range(batch_size)]
    invalid = tool.execute({"tasks": [*asks, {"task": "Excess"}]})
    assert invalid["error_info"]["name"] == "SUBAGENT_BATCH_SIZE_LIMIT"
    assert not manager.children_of(owner.process_id)

    for index in range(round_limit):
        result = tool.execute({"tasks": asks})
        assert result["status"] == "completed", result
        assert result["batches_remaining"] == round_limit - 1 - index
        assert [item["summary"] for item in result["results"]] == [item["task"] for item in asks]
    assert len(manager.children_of(owner.process_id)) == batch_size * round_limit
    root_budget = root.task_budget.snapshot()
    assert root_budget["used"]["agents"] == (2 if subprocess else 1)
    if subprocess:
        lease = root_budget["process_leases"][owner.process_id]
        initial_lease = owner.budget_lease
        assert lease["reserved_provider_calls"] == initial_lease["reserved_provider_calls"] - batch_size * round_limit
        assert lease["reserved_output_tokens"] == initial_lease["reserved_output_tokens"]
    # Rebuilding the model/tool surface mid-task does not reset the quota.
    next_tool = _tools(owner, tmp_path_factory, object())["run_subagent"]
    blocked = next_tool.execute({"task": "Another batch"})
    assert blocked["error_info"]["name"] == "SUBAGENT_TURN_BATCH_LIMIT"
    assert len(manager.children_of(owner.process_id)) == batch_size * round_limit
    owner.active_task_id += "-next"
    result = next_tool.execute({"tasks": asks})
    assert result["status"] == "completed", result
    assert result["batches_remaining"] == round_limit - 1


def test_nested_patch_reads_owner_checkout_and_keeps_nested_event_identity(
    tmp_path_factory, monkeypatch, worktree_case,
):
    repo = _repo(tmp_path_factory)
    storage, manager = worktree_case(repo, tmp_path_factory / "state")
    manager.session_store = SessionStore(repo, storage=storage)
    parent = _parent(manager, repo, patch=True)
    streamed = []
    observations = {}

    def execute(process, workspace_path, on_stream_event, **_):
        target = Path(workspace_path, "app.txt")
        if process.role == "subagent":
            assert target.read_text(encoding="utf-8") == "subprocess progress\n"
            target.write_text("helper candidate\n", encoding="utf-8")
        else:
            target.write_text("subprocess progress\n", encoding="utf-8")
            tools = _tools(process, workspace_path, object(), on_stream_event)
            blocked = tools["run_subagent"].execute({"task": "Outside scope", "write_files": ["extra.txt"]})
            assert blocked["error_info"]["name"] == "SUBAGENT_WRITE_SCOPE_REQUIRED"
            result = tools["run_subagent"].execute({"task": "Prepare patch", "write_files": ["app.txt"]})
            assert result["status"] == "completed", result
            assert target.read_text(encoding="utf-8") == "subprocess progress\n"
            artifact = tools["read_child_artifact"].execute({"result_ref": result["result_ref"], "path": "app.txt"})
            observations["candidate"] = artifact["content"]
            observations["helper"] = result["process_id"]
        process.status = ProcessStatus.COMPLETED
        process.result = {"status": "completed", "response": "Done"}
        return process.result

    monkeypatch.setattr(executor, "agent_step", execute)
    root_tools = _tools(parent, repo, object(), streamed.append)
    launched = root_tools["delegate_task"].execute({
        "task_description": "Prepare a change to app.txt",
        "target_files": ["app.txt"],
        "acceptance_criteria": ["Complete the specific change"],
    })
    assert launched["delegated"], launched
    outer = launched["process_id"]
    settled = manager.wait_many([outer], timeout=30)["processes"][outer]
    assert settled["status"] == "completed", settled
    assert observations["candidate"] == "helper candidate\n"
    helper = observations["helper"]
    events = [event for event in streamed if event["process_id"] == helper and event["event"] in {"agent_started", "agent_terminal"}]
    assert [event["event"] for event in events] == ["agent_started", "agent_terminal"]
    assert all(event["role"] == "subagent" and event["parent_process_id"] == outer
               and event["root_process_id"] == parent.process_id for event in events)
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert helper not in storage.read_project_b_processes()
