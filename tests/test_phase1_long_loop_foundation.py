"""Regression tests for the first real-long-loop infrastructure phase."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.core.history import HistoryManager
from backend.core.loop.budget import TaskBudgetExceeded
from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.completion_protocol import HostCompletionEvaluator
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.tools import ToolRegistry


def _manager_process(manager: AgentProcessManager, *, parent_id=None,
                     actor="worker", task_kind="answer", task_id="task",
                     workspace="."):
    profile = "supervisor.control" if actor == "supervisor" else "text.only"
    return manager.fork(
        parent_id=parent_id,
        role="supervisor" if actor == "supervisor" else "executor",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools(profile)),
        max_steps=5,
        ring_level=RingLevel.RING_0 if actor == "supervisor" else RingLevel.RING_3,
        workspace_path=str(workspace),
        task_id=task_id,
        actor_kind=actor,
        capability_profile_id=profile,
        task_kind=task_kind,
    )


def test_supervisor_action_requires_completed_child_and_structured_a_review(tmp_path_factory):
    tmp_path = tmp_path_factory
    manager = AgentProcessManager()
    root = _manager_process(
        manager, actor="supervisor", task_kind="supervisor",
        task_id="root", workspace=tmp_path,
    )
    child = _manager_process(
        manager, parent_id=root.process_id, task_kind="action",
        task_id="child", workspace=tmp_path,
    )
    root.child_ids.append(child.process_id)
    root.delegated_contracts[child.process_id] = {
        "task_kind": "action",
        "required_for_parent_completion": True,
    }

    child.status = ProcessStatus.COMPLETED
    child.result = {"status": "completed", "process_id": child.process_id}
    child.tool_receipts = [{
        "receipt_id": "receipt-1",
        "succeeded": True,
        "committed": True,
        "effect": "write",
    }]
    blocked = HostCompletionEvaluator.evaluate(root, "TASK_COMPLETE\ndone")
    assert not blocked.allowed
    assert any("structured approval" in reason for reason in blocked.reasons)

    root.child_reviews[child.process_id] = {
        "verdict": "approved", "summary": "receipts and tests inspected",
        "receipt_ids": ["receipt-1"],
    }
    assert HostCompletionEvaluator.evaluate(root, "TASK_COMPLETE\ndone").allowed

    child.tool_receipts[0]["effect"] = "read"
    blocked = HostCompletionEvaluator.evaluate(root, "TASK_COMPLETE\ndone")
    assert not blocked.allowed
    assert any("committed child action" in reason for reason in blocked.reasons)


def test_supervisor_answer_child_also_requires_structured_a_review(tmp_path_factory):
    manager = AgentProcessManager()
    root = _manager_process(
        manager, actor="supervisor", task_kind="supervisor",
        task_id="root-answer", workspace=tmp_path_factory,
    )
    child = _manager_process(
        manager, parent_id=root.process_id, task_kind="answer",
        task_id="child-answer", workspace=tmp_path_factory,
    )
    root.child_ids.append(child.process_id)
    root.delegated_contracts[child.process_id] = {
        "task_kind": "answer", "required_for_parent_completion": True,
    }
    child.status = ProcessStatus.COMPLETED
    child.result = {"status": "completed", "response": "evidence"}

    blocked = HostCompletionEvaluator.evaluate(root, "TASK_COMPLETE\ndone")
    assert not blocked.allowed
    assert any("structured approval" in reason for reason in blocked.reasons)
    root.child_reviews[child.process_id] = {
        "verdict": "approved", "summary": "evidence inspected",
    }
    assert HostCompletionEvaluator.evaluate(root, "TASK_COMPLETE\ndone").allowed


def test_review_child_outcome_host_links_action_receipts(tmp_path_factory):
    from backend.core.loop import executor as executor_module

    tmp_path = tmp_path_factory
    manager = AgentProcessManager()
    root = _manager_process(
        manager, actor="supervisor", task_kind="supervisor",
        task_id="root-auto-receipt", workspace=tmp_path,
    )
    child = _manager_process(
        manager, parent_id=root.process_id, task_kind="action",
        task_id="child-auto-receipt", workspace=tmp_path,
    )
    root.child_ids.append(child.process_id)
    root.delegated_contracts[child.process_id] = {
        "task_kind": "action", "required_for_parent_completion": True,
    }
    child.status = ProcessStatus.COMPLETED
    child.result = {"status": "completed", "process_id": child.process_id}
    child.tool_receipts = [{
        "receipt_id": "committed-write",
        "succeeded": True,
        "committed": True,
        "effect": "write",
    }, {
        "receipt_id": "read-only",
        "succeeded": True,
        "committed": True,
        "effect": "read",
    }]

    tools = executor_module._build_internal_tools(
        root, {}, str(tmp_path), object(), SimpleNamespace(_executors={}),
    )
    review_tool = tools["review_child_outcome"]
    assert "receipt_ids" not in review_tool.parameters["properties"]
    assert "receipt_ids" not in review_tool.parameters["required"]

    result = review_tool.execute({
        "process_id": child.process_id,
        "verdict": "approve",
        "summary": "semantic result and tests inspected",
    })
    assert result["accepted"] is True
    assert result["review"]["verdict"] == "approved"
    assert review_tool.parameters["properties"]["verdict"]["enum"] == [
        "approved", "changes_required",
    ]
    assert result["review"]["receipt_ids"] == ["committed-write"]
    assert result["review"]["receipt_selection"] == "host_auto"
    assert HostCompletionEvaluator.evaluate(root, "TASK_COMPLETE\ndone").allowed


def test_delegate_contract_propagates_required_tests(tmp_path_factory, monkeypatch):
    tmp_path = tmp_path_factory
    from backend.core.loop import executor as executor_module

    manager = AgentProcessManager()
    root = _manager_process(
        manager, actor="supervisor", task_kind="supervisor",
        task_id="root", workspace=tmp_path,
    )

    def fake_agent_step(*, process, **_kwargs):
        process.status = ProcessStatus.COMPLETED
        return {"status": "completed", "process_id": process.process_id}

    monkeypatch.setattr(executor_module, "agent_step", fake_agent_step)
    tools = executor_module._build_internal_tools(
        root, {}, str(tmp_path), object(), SimpleNamespace(_executors={}),
    )
    assert "review_child_outcome" in tools
    delegated = tools["delegate_task"].execute({
        "task_description": "perform bounded work",
        "capability_profile_id": "development.workspace",
        "task_kind": "action",
        "target_files": [],
        "acceptance_criteria": ["return evidence"],
        "required_test_ids": ["unit:core", "seed:7"],
        "tool_scope_mode": "test_only",
        "required_for_parent_completion": True,
    })
    child = manager.get(delegated["process_id"])
    assert child is not None
    assert child.required_test_ids == ["unit:core", "seed:7"]
    assert child.tool_registry.list_all() == ["run_test"]
    assert child.read_context_snapshot()[0]["task_contract"]["required_test_ids"] == [
        "unit:core", "seed:7",
    ]

    mixed = tools["delegate_task"].execute({
        "task_description": "inspect runtime behavior and then run its required test",
        "capability_profile_id": "development.workspace",
        "task_kind": "action",
        "target_files": ["backend/core/loop/tool_pipeline.py"],
        "acceptance_criteria": ["return inspection and test evidence"],
        "required_test_ids": ["unit:mixed"],
        "required_tool_calls": [
            {"tool_name": "define_tool", "min_calls": 1, "max_calls": 1},
            {"tool_name": "inspect_runtime_contract", "min_calls": 1},
            {"tool_name": "run_test", "min_calls": 1, "max_calls": 1},
        ],
        "required_for_parent_completion": False,
    })
    mixed_child = manager.get(mixed["process_id"])
    assert mixed_child is not None
    assert {"define_tool", "read_file", "run_test"} <= set(
        mixed_child.tool_registry.list_all()
    )
    assert (
        mixed_child.read_context_snapshot()[0]["task_contract"]["tool_scope_source"]
        == "capability_profile"
    )
    assert mixed_child.read_context_snapshot()[0]["task_contract"]["required_tool_calls"] == [
        {"tool_name": "define_tool", "min_calls": 1, "max_calls": 1,
         "include_composite_steps": False},
        {"tool_name": "inspect_runtime_contract", "min_calls": 1,
         "include_composite_steps": False},
        {"tool_name": "run_test", "min_calls": 1, "max_calls": 1,
         "include_composite_steps": False},
    ]

    observed = tools["delegate_task"].execute({
        "task_description": "inspect without side effects",
        "capability_profile_id": "governance.observe",
        "target_files": [],
        "acceptance_criteria": ["return findings"],
    })
    observed_child = manager.get(observed["process_id"])
    assert observed_child is not None
    assert observed_child.task_kind == "answer"

    development = tools["delegate_task"].execute({
        "task_description": "perform a bounded workspace change",
        "capability_profile_id": "development.workspace",
        "target_files": [],
        "acceptance_criteria": ["return evidence"],
    })
    development_child = manager.get(development["process_id"])
    assert development_child is not None
    assert development_child.task_kind == "action"

    impossible = tools["delegate_task"].execute({
        "task_description": "impossible read-only action",
        "capability_profile_id": "governance.observe",
        "task_kind": "action",
        "target_files": [],
        "acceptance_criteria": [],
    })
    assert impossible["delegated"] is False
    assert impossible["error"] == "DELEGATION_ADMISSION_FAILED"
    assert "cannot satisfy an action task" in impossible["message"]

    wrong_actor_kind = tools["delegate_task"].execute({
        "task_description": "misrouted review",
        "capability_profile_id": "governance.observe",
        "task_kind": "review",
        "target_files": [],
        "acceptance_criteria": [],
    })
    assert wrong_actor_kind["delegated"] is False
    assert wrong_actor_kind["error"] == "DELEGATION_ADMISSION_FAILED"
    assert "only accepts answer, plan, or action" in wrong_actor_kind["message"]


def test_governance_context_is_versioned_and_project_lessons_are_loaded(
    tmp_path_factory, monkeypatch,
):
    tmp_path = tmp_path_factory
    from backend.core.knowledge.manager import LessonManager
    from backend.core.loop import context_builder

    calls = []
    monkeypatch.setattr(LessonManager, "load_abstract", lambda _ws: [])
    monkeypatch.setattr(
        LessonManager, "load_instance",
        lambda _ws, project: calls.append(("instance", project)) or [],
    )
    monkeypatch.setattr(
        LessonManager, "load_pending",
        lambda _ws, project: calls.append(("pending", project)) or [],
    )
    monkeypatch.setattr(context_builder, "_get_latest_policy_results", lambda _p: None)
    monkeypatch.setattr(context_builder, "_get_recent_rejections", lambda _p: [])
    monkeypatch.setattr(context_builder, "_get_recent_facts", lambda _p: [])

    context = context_builder.build_governance_context("project-x", tmp_path)
    assert ("instance", "project-x") in calls
    assert ("pending", "project-x") in calls
    assert context["project_name"] == "project-x"

    manager = AgentProcessManager()
    process = _manager_process(manager, workspace=tmp_path)
    old_version = process.read_context_snapshot()[1]
    new_version = process.replace_context_snapshot(context)
    assert new_version > old_version
    assert process.read_context_snapshot()[0]["project_name"] == "project-x"


def test_recursive_cancel_stops_every_live_descendant(tmp_path_factory):
    tmp_path = tmp_path_factory
    manager = AgentProcessManager()
    root = _manager_process(
        manager, actor="supervisor", task_kind="supervisor",
        task_id="root", workspace=tmp_path,
    )
    child = _manager_process(
        manager, parent_id=root.process_id, task_id="child", workspace=tmp_path,
    )
    child.status = ProcessStatus.RUNNING
    result = manager.kill(root.process_id, reason="test_cancel")
    assert set(result["process_ids"]) == {root.process_id, child.process_id}
    assert root.cancellation_event.is_set()
    assert child.cancellation_event.is_set()
    assert root.cancellation_reason == child.cancellation_reason == "test_cancel"
    duplicate = manager.kill(root.process_id, reason="test_cancel")
    assert not duplicate["requested"]
    assert set(duplicate["already_cancelling"]) == {root.process_id, child.process_id}


def test_task_tree_budget_is_shared_and_hard_limited(tmp_path_factory):
    tmp_path = tmp_path_factory
    manager = AgentProcessManager()
    root = manager.fork(
        parent_id=None, role="supervisor", tool_registry=ToolRegistry([]),
        max_steps=5, ring_level=RingLevel.RING_0,
        workspace_path=str(tmp_path), task_id="root",
        task_budget_limits={
            "max_agents": 2, "max_provider_calls": 1,
            "max_output_tokens": 4, "max_seconds": 30,
        },
    )
    child = manager.fork(
        parent_id=root.process_id, role="worker", tool_registry=ToolRegistry([]),
        max_steps=5, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path), task_id="child",
    )
    assert child.task_budget is root.task_budget
    with pytest.raises(TaskBudgetExceeded, match="agent limit"):
        manager.fork(
            parent_id=root.process_id, role="worker", tool_registry=ToolRegistry([]),
            max_steps=5, ring_level=RingLevel.RING_3,
            workspace_path=str(tmp_path), task_id="too-many",
        )
    # The sole call is escrowed to the admitted child; A cannot steal it.
    child.task_budget.begin_provider_call(child.process_id)
    with pytest.raises(TaskBudgetExceeded, match="provider-call"):
        root.task_budget.begin_provider_call(root.process_id)
    # Consume the child's escrow, then the remaining work pool. The protected
    # verification reserve cannot be consumed by either A or an ordinary B.
    child.task_budget.consume_output("a" * 176, child.process_id)
    root.task_budget.consume_output("a" * 692, root.process_id)
    with pytest.raises(TaskBudgetExceeded, match="output allocation"):
        root.task_budget.consume_output("1234", root.process_id)


def test_harvest_signal_state_machine_is_idempotent_and_mixed_source(tmp_path_factory):
    tmp_path = tmp_path_factory
    from backend.core.knowledge import harvest

    HistoryManager.set_workspace(str(tmp_path))
    first = harvest.capture_signal(
        "lesson_trigger", {"trigger": "a.py", "rule": "if a then must b"},
        "project-x", source_event_id="event-1",
    )
    duplicate = harvest.capture_signal(
        "lesson_trigger", {"trigger": "a.py", "rule": "if a then must b"},
        "project-x", source_event_id="event-1",
    )
    second = harvest.capture_signal(
        "contract_drift", {"trigger": "b.py", "rule": "if b then must c"},
        "project-x", source_event_id="event-2",
    )
    for index in range(3, 6):
        harvest.capture_signal(
            "lesson_trigger" if index % 2 else "contract_drift",
            {"trigger": f"{index}.py", "rule": "if changed then must verify"},
            "project-x", source_event_id=f"event-{index}",
        )
    assert first == duplicate
    assert len(harvest.get_unprocessed_signals("project-x")) == 5
    assert harvest.source_diversity(
        harvest.get_unprocessed_signals("project-x")
    ) == 2
    assert harvest.should_trigger_harvest("lesson_trigger", "project-x")

    leased = harvest.lease_harvest_signals("project-x")
    leased_ids = [item["signal_id"] for item in leased]
    assert {first, second}.issubset(set(leased_ids))
    assert harvest.get_unprocessed_signals("project-x") == []

    harvest.fail_harvest("project-x", leased_ids, "temporary")
    retried = harvest.get_unprocessed_signals("project-x")
    assert {item["harvest_retry_count"] for item in retried} == {1}

    leased = harvest.lease_harvest_signals("project-x")
    harvest.complete_harvest(
        "project-x", [item["signal_id"] for item in leased], ["lesson-1"],
    )
    assert harvest.get_unprocessed_signals("project-x") == []
