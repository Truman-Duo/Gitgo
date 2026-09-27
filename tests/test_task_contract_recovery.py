from __future__ import annotations

from types import SimpleNamespace

from backend.core.errors import ERROR_CATALOG, error_payload
from backend.core.daemon.dispatch import _btw_parent_snapshot, _btw_scope_snapshot
from backend.core.loop import executor as executor_module
from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.completion_protocol import HostCompletionEvaluator
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import RingLevel
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.task_contract import (
    compile_routing_proposal,
    routing_advice,
    validate_contract_proposal,
)
from backend.core.loop.coordination import (
    observe_interface_changes, publish_coordination_event,
    resolve_coordination_event,
)
from backend.core.loop.interface_contract import (
    capture_declared_contract, verify_declared_contract,
)


def _supervisor(tmp_path):
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None,
        role="supervisor",
        tool_registry=ToolRegistry(
            CapabilityProfiles.resolve_tools("supervisor.control")
        ),
        max_steps=8,
        ring_level=RingLevel.RING_0,
        workspace_path=str(tmp_path),
        task_id="root-task",
        actor_kind="supervisor",
        capability_profile_id="supervisor.control",
        task_kind="supervisor",
        context_snapshot={"task_contract": {"task_description": "create page"}},
    )
    tools = executor_module._build_internal_tools(
        process, {}, str(tmp_path), object(), SimpleNamespace(_executors={}),
    )
    return process, tools


def test_error_catalog_has_unique_stable_ids_and_dual_identity():
    ids = [definition.catalog_id for definition in ERROR_CATALOG.values()]
    assert len(ids) == len(set(ids))
    payload = error_payload("CAPABILITY_PROFILE_UNKNOWN")
    assert payload["error"] == "CAPABILITY_PROFILE_UNKNOWN"
    assert payload["error_info"]["catalog_id"] == "GITGO-E3101"
    assert payload["error_info"]["occurrence_id"].startswith("err_")


def test_adaptive_root_cannot_skip_semantic_contract_and_delegate_directly(tmp_path_factory):
    process, tools = _supervisor(tmp_path_factory)
    process.task_kind = "answer"
    result = tools["delegate_task"].execute({
        "task_description": "write one bounded file",
        "target_files": ["solution.py"],
        "acceptance_criteria": ["file exists"],
        "capability_profile_id": "development.workspace",
        "task_kind": "action",
    })
    assert result["delegated"] is False
    assert result["error"] == "DELEGATION_ADMISSION_FAILED"
    assert result["error_info"]["details"]["reason"] == "task_contract_not_declared"
    assert result["error_info"]["next_actions"][0]["action"] == "declare_task_contract"


def test_btw_snapshot_reads_parent_prose_without_reasoning_or_tools(tmp_path_factory):
    manager = AgentProcessManager()
    process, _tools = _supervisor(tmp_path_factory)
    # The helper is intentionally manager-agnostic; register this test process
    # in the same way recovered live processes are indexed.
    manager._processes[process.process_id] = process
    process.session.messages.extend([
        {"role": "system", "content": "secret governance prompt"},
        {"role": "user", "content": "Create the page with a blue heading"},
        {"role": "assistant", "content": "I will delegate it", "reasoning_content": "private"},
        {"role": "tool", "content": "large receipt"},
    ])
    snapshot = _btw_parent_snapshot(manager, process.process_id)
    assert snapshot["attached"] is True
    assert snapshot["conversation"] == [
        {"role": "user", "content": "Create the page with a blue heading"},
        {"role": "assistant", "content": "I will delegate it"},
    ]
    encoded = str(snapshot)
    assert "secret governance prompt" not in encoded
    assert "large receipt" not in encoded
    assert "private" not in encoded


def test_btw_scope_can_read_selected_a_and_multiple_b_without_peer_chat(tmp_path_factory):
    manager = AgentProcessManager()
    root = manager.fork(
        parent_id=None, role="supervisor",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("supervisor.control")),
        max_steps=8, ring_level=RingLevel.RING_0,
        workspace_path=str(tmp_path_factory), task_id="root",
        actor_kind="supervisor", capability_profile_id="supervisor.control",
        task_kind="supervisor",
    )
    children = []
    for index in range(2):
        child = manager.fork(
            parent_id=root.process_id, role="executor",
            tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("development.workspace")),
            max_steps=8, ring_level=RingLevel.RING_3,
            workspace_path=str(tmp_path_factory), task_id=f"child-{index}",
            actor_kind="worker", capability_profile_id="development.workspace",
            task_kind="action",
        )
        child.session.append_assistant(f"worker {index} fact")
        children.append(child)
    snapshot = _btw_scope_snapshot(
        manager, [root.process_id, *(item.process_id for item in children)],
    )
    assert snapshot["source_process_ids"] == [
        root.process_id, children[0].process_id, children[1].process_id,
    ]
    assert len(snapshot["relationship_graph"]) == 3
    assert all(
        not dict(item.get("relationship_policy") or {}).get(
            "communication", {},
        ).get("private_peer_chat", False)
        for item in snapshot["relationship_graph"] if item["owner_process_id"]
    )


def test_host_routes_worker_coordination_to_a_and_affected_b(tmp_path_factory):
    manager = AgentProcessManager()
    root = manager.fork(
        parent_id=None, role="supervisor",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("supervisor.control")),
        max_steps=8, ring_level=RingLevel.RING_0,
        workspace_path=str(tmp_path_factory), task_id="root",
        actor_kind="supervisor", capability_profile_id="supervisor.control",
        task_kind="supervisor",
    )
    first = manager.fork(
        parent_id=root.process_id, role="executor",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("development.workspace")),
        max_steps=8, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id="upstream",
        actor_kind="worker", capability_profile_id="development.workspace",
        task_kind="action",
    )
    second = manager.fork(
        parent_id=root.process_id, role="executor",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("development.workspace")),
        max_steps=8, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id="downstream",
        actor_kind="worker", capability_profile_id="development.workspace",
        task_kind="action", depends_on=[first.process_id],
    )
    event = publish_coordination_event(
        manager, first, kind="dependency_change", summary="schema changed",
        affected_process_ids=[second.process_id], affected_interfaces=["api.py:read"],
        requires_supervisor_action=True, block_affected=True,
    )
    root_context, _ = root.read_context_snapshot()
    downstream_context, _ = second.read_context_snapshot()
    assert root_context["coordination_events"][-1]["coordination_event_id"] == event["coordination_event_id"]
    assert downstream_context["coordination_blocks"] == [event["coordination_event_id"]]
    resolved = resolve_coordination_event(
        manager, root, event["coordination_event_id"],
        disposition="acknowledge", note="No contract revision required",
    )
    assert resolved["status"] == "resolved"
    assert second.read_context_snapshot()[0]["coordination_blocks"] == []


def test_interface_revision_updates_document_and_blocks_stale_downstream(tmp_path_factory):
    api = tmp_path_factory / "api.py"
    api.write_text("def read(value: str) -> str:\n    return value\n", encoding="utf-8")
    contract = capture_declared_contract(tmp_path_factory, [{
        "node_id": "upstream", "output_interfaces": ["api.py:read"],
        "input_interfaces": [],
    }, {
        "node_id": "downstream", "output_interfaces": [],
        "input_interfaces": ["api.py:read"],
    }])
    manager = AgentProcessManager()
    root = manager.fork(
        parent_id=None, role="supervisor", tool_registry=ToolRegistry([]),
        max_steps=8, ring_level=RingLevel.RING_0,
        workspace_path=str(tmp_path_factory), task_id="root",
        actor_kind="supervisor", capability_profile_id="supervisor.control",
        task_kind="supervisor",
    )
    first = manager.fork(
        parent_id=root.process_id, role="executor", tool_registry=ToolRegistry([]),
        max_steps=8, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id="upstream",
        actor_kind="worker", capability_profile_id="development.workspace",
        task_kind="action", context_snapshot={"task_contract": {
            "output_interfaces": ["api.py:read"], "interface_contract": contract,
        }},
    )
    second = manager.fork(
        parent_id=root.process_id, role="executor", tool_registry=ToolRegistry([]),
        max_steps=8, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id="downstream",
        actor_kind="worker", capability_profile_id="development.workspace",
        task_kind="action", depends_on=[first.process_id],
        context_snapshot={"task_contract": {
            "input_interfaces": ["api.py:read"], "interface_contract": contract,
        }},
    )
    api.write_text(
        "def read(value: str, strict: bool = False) -> str:\n    return value\n",
        encoding="utf-8",
    )
    observed = observe_interface_changes(
        manager, first, str(tmp_path_factory), compatibility="breaking",
    )
    assert len(observed["events"]) == 1
    event = observed["events"][0]
    assert second.read_context_snapshot()[0]["coordination_blocks"] == [
        event["coordination_event_id"],
    ]
    resolve_coordination_event(
        manager, root, event["coordination_event_id"],
        disposition="accept_revision", note="Use the revised signature",
    )
    revised = second.read_context_snapshot()[0]["task_contract"]["interface_contract"]
    assert verify_declared_contract(revised, tmp_path_factory, ["api.py:read"]) == []


def test_worker_coordination_tools_do_not_leak_into_supervisor_profile():
    supervisor = set(CapabilityProfiles.resolve_tools("supervisor.control"))
    worker = set(CapabilityProfiles.resolve_tools("development.workspace"))
    assert {"publish_interface_update", "escalate_to_supervisor"} <= worker
    assert "publish_interface_update" not in supervisor
    assert "escalate_to_supervisor" not in supervisor
    assert {"list_coordination_events", "resolve_coordination_event"} <= supervisor


def test_delegate_schema_exposes_legal_profiles_and_task_kinds(tmp_path_factory):
    _process, tools = _supervisor(tmp_path_factory)
    properties = tools["delegate_task"].parameters["properties"]
    assert properties["capability_profile_id"]["default"] == "development.workspace"
    assert set(properties["capability_profile_id"]["enum"]) == set(
        CapabilityProfiles.ids(actor_kind="worker")
    )
    assert properties["task_kind"]["enum"] == ["answer", "plan", "action"]


def test_structured_routing_keeps_bounded_single_owner_work_with_a(tmp_path_factory):
    contract = validate_contract_proposal({
        "goal": "Write one small answer file",
        "execution_mode": "either",
        "estimated_complexity": "bounded",
        "independent_workstreams": 1,
    }, str(tmp_path_factory))
    assert routing_advice(contract)["recommended"] == "self_execute"


def test_route_compiler_normalizes_bounded_delegation_without_granting_authority(
    tmp_path_factory,
):
    proposal = validate_contract_proposal({
        "goal": "Write one small answer file",
        "execution_mode": "delegate",
        "delegation_required": True,
        "minimum_delegated_outcomes": 1,
        "estimated_complexity": "bounded",
        "independent_workstreams": 1,
        "routing_transition": "delegate_initial",
        "deliverables": [{"kind": "workspace_file", "path": "answer.txt", "required": True}],
    }, str(tmp_path_factory))
    normalized, compilation = compile_routing_proposal(
        {}, proposal, workflow_started=False,
    )

    assert normalized["execution_mode"] == "self_execute"
    assert normalized["delegation_required"] is False
    assert normalized["minimum_delegated_outcomes"] == 0
    assert normalized["routing_transition"] == "keep_supervisor"
    assert compilation == {
        "requested": "delegate",
        "recommended": "self_execute",
        "reason": "bounded_single_owner",
        "adjusted": True,
        "authority_granted": False,
        "next_action": "request_self_execute",
    }


def test_root_contract_normalizes_required_tool_receipt_counts(tmp_path_factory):
    contract = validate_contract_proposal({
        "goal": "Exercise an authored tool exactly once",
        "execution_mode": "self_execute",
        "required_tool_calls": [{
            "tool_name": "authored_probe",
            "min_calls": 1,
            "max_calls": 1,
        }],
    }, str(tmp_path_factory))
    assert contract["required_tool_calls"] == [{
        "tool_name": "authored_probe",
        "min_calls": 1,
        "max_calls": 1,
        "include_composite_steps": False,
    }]


def test_root_contract_drops_circular_completion_protocol_requirements(tmp_path_factory):
    contract = validate_contract_proposal({
        "goal": "Write and test one solution",
        "execution_mode": "self_execute",
        "required_tool_calls": [
            {"tool_name": "write_file", "min_calls": 1},
            {"tool_name": "complete_supervision", "min_calls": 1},
            {"tool_name": "complete_task", "min_calls": 1},
        ],
    }, str(tmp_path_factory))

    assert [item["tool_name"] for item in contract["required_tool_calls"]] == [
        "write_file",
    ]
    assert contract["ignored_protocol_requirements"] == [
        "complete_supervision", "complete_task",
    ]


def test_root_contract_rejects_inverted_required_tool_bounds(tmp_path_factory):
    import pytest

    with pytest.raises(ValueError, match="max_calls"):
        validate_contract_proposal({
            "goal": "Invalid receipt contract",
            "required_tool_calls": [{
                "tool_name": "authored_probe", "min_calls": 2, "max_calls": 1,
            }],
        }, str(tmp_path_factory))


def test_structured_routing_allows_parallel_or_justified_complex_delegation(tmp_path_factory):
    parallel = validate_contract_proposal({
        "goal": "Audit independent components",
        "execution_mode": "either",
        "independent_workstreams": 3,
    }, str(tmp_path_factory))
    assert routing_advice(parallel)["reason"] == "independent_workstreams"

    complex_one = validate_contract_proposal({
        "goal": "Perform one deep migration",
        "execution_mode": "either",
        "estimated_complexity": "high",
        "delegation_rationale": "Requires an isolated implementation and independent A review",
    }, str(tmp_path_factory))
    assert routing_advice(complex_one)["reason"] == "complexity_justified"


def test_routing_contract_can_evolve_from_a_to_handoff_and_continue_owner(tmp_path_factory):
    handoff = validate_contract_proposal({
        "goal": "The bounded task grew into an implementation workstream",
        "execution_mode": "either",
        "routing_transition": "handoff_to_worker",
        "handoff_summary": "A established the design; B owns implementation.",
    }, str(tmp_path_factory))
    assert routing_advice(handoff) == {
        "recommended": "delegate", "binding": False,
        "reason": "handoff_to_worker",
    }
    continuation = validate_contract_proposal({
        "goal": "Revise the same component",
        "execution_mode": "either",
        "routing_transition": "continue_owner",
    }, str(tmp_path_factory))
    assert routing_advice(continuation)["recommended"] == "continue_worker"


def test_unknown_profile_returns_recoverable_error_and_preserves_attempt(tmp_path_factory):
    process, tools = _supervisor(tmp_path_factory)
    result = tools["delegate_task"].execute({
        "task_description": "Create index.html",
        "capability_profile_id": "code",
        "task_kind": "action",
        "target_files": ["index.html"],
        "acceptance_criteria": ["Write the file"],
    })
    assert result["delegated"] is False
    assert result["error"] == "CAPABILITY_PROFILE_UNKNOWN"
    info = result["error_info"]
    assert info["catalog_id"] == "GITGO-E3101"
    assert info["retryable"] is True
    assert info["details"]["recommended_profile"] == "development.workspace"
    contract = process.read_context_snapshot()[0]["task_contract"]
    assert contract["minimum_delegated_outcomes"] == 1
    assert contract["delegation_attempts"][-1]["state"] == "admission_failed"


def test_llm_contract_blocks_text_substitution_for_missing_b_and_file(tmp_path_factory):
    process, tools = _supervisor(tmp_path_factory)
    declared = tools["declare_task_contract"].execute({
        "goal": "Create an HTML introduction",
        "execution_mode": "delegate",
        "delegation_required": True,
        "minimum_delegated_outcomes": 1,
        "deliverables": [{
            "kind": "workspace_file",
            "path": "index.html",
            "required": True,
            "allow_inline_substitution": False,
        }],
        "acceptance_criteria": ["A B Agent writes index.html"],
        "uncertainties": [],
        "requires_user_decision": False,
        "estimated_complexity": "moderate",
        "independent_workstreams": 1,
        "delegation_rationale": "The test contract explicitly requires an independently owned B delivery.",
        "routing_transition": "delegate_initial",
    })
    assert declared["accepted"] is True
    evaluation = HostCompletionEvaluator.evaluate(
        process, "Here is HTML you can copy from chat.",
    )
    assert not evaluation.allowed
    joined = "; ".join(evaluation.reasons)
    assert "GITGO-E6101" in joined
    assert "GITGO-E6102" in joined


def test_recoverable_admission_failure_requires_retry_before_terminal_failure(tmp_path_factory):
    process, tools = _supervisor(tmp_path_factory)
    args = {
        "task_description": "Create index.html",
        "capability_profile_id": "code",
        "task_kind": "action",
        "target_files": ["index.html"],
        "acceptance_criteria": ["Write the file"],
    }
    tools["delegate_task"].execute(args)
    first = HostCompletionEvaluator.evaluate_supervisor_failure(
        process, "Delegation admission failed.",
    )
    assert not first.allowed
    assert any("untried Host-prescribed" in reason for reason in first.reasons)
    tools["delegate_task"].execute(args)
    second = HostCompletionEvaluator.evaluate_supervisor_failure(
        process, "Delegation admission failed after retry.",
    )
    assert second.allowed


def test_empty_bundle_is_never_reported_as_delegated(tmp_path_factory):
    _process, tools = _supervisor(tmp_path_factory)
    result = tools["delegate_task_bundle"].execute({
        "goal": "Create a new file",
        "target_files": ["index.html"],
        "acceptance_criteria": ["Write index.html"],
    })
    assert result["delegated"] is False
    assert result["error"] == "DELEGATION_EMPTY_RESULT"
    assert result["error_info"]["catalog_id"] == "GITGO-E3104"
    assert result["error_info"]["next_actions"][0]["action"] == "use_delegate_task"
