"""Task scope activation uses the existing fence, checkpoint and authority path."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest

from backend.core.loop.agent_tool import AgentTool, ApprovalMode, ToolEffect
from backend.core.loop.event_bus import EventBus
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.execution_contract import data_broker
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import RingLevel
from backend.core.loop.task_contract import (
    publish_contract, preview_contract, get_task_contract, semantic_contract,
    validate_contract_proposal, TaskContractRevisionConflict,
)
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tools import ToolRegistry
from backend.core.storage import StorageCommitUnknown


@pytest.fixture
def runtime(tmp_path_factory):
    manager = AgentProcessManager()
    store = SessionStore(tmp_path_factory, state_home=tmp_path_factory / "state", migrate_legacy=False)
    manager.session_store = store
    process = manager.fork(None, "supervisor", ToolRegistry(["unit", "declare_task_contract"]), 12,
        RingLevel.RING_0, workspace_path=str(tmp_path_factory), task_id="scope-task",
        actor_kind="supervisor", capability_profile_id="supervisor.control")
    try:
        yield manager, process, store, tmp_path_factory
    finally:
        store.close()


def proposal(root, **fields):
    return validate_contract_proposal({"goal": "Update the requested feature", "execution_mode": "self_execute",
        "deliverables": [{"kind": "response", "required": True}], "acceptance_criteria": ["Explain verified behavior"],
        **fields}, str(root))


def plan():
    return {"profiles": ["retrospective"], "nodes": [{"id": "retro", "kind": "report", "format": "retrospective"}]}


def execute(process, root, candidate, bus=None):
    context = ExecutionContext(process=process, session=process.session, workspace_path=str(root),
        event_bus=bus or EventBus(), cancellation=process.cancellation_event)
    return ToolPipeline().execute({"name": "unit", "args": {"value": "one"}}, candidate, context, "scope-batch")


def unit(calls, handler=None, approval=ApprovalMode.ALLOW):
    return AgentTool(name="unit", description="Fixture mutation", parameters={"type": "object"},
        execute=handler or (lambda args: calls.append(args["value"]) or {"ok": True}),
        effect=ToolEffect.PROCESS, read_only=False, execution_contract=data_broker("test.fixture"),
        approval=approval, approval_per_invocation=approval == ApprovalMode.ASK)


def test_preview_and_activation_commit_contract_workflow_and_child_together(runtime):
    manager, process, store, root = runtime
    child = manager.fork(process.process_id, "worker", ToolRegistry([]), 4, RingLevel.RING_3,
        workspace_path=str(root), task_id="previous-child")
    process.delegated_contracts[child.process_id] = {"required_for_parent_completion": False}
    candidate = proposal(root, engineering_workflow=plan(), adopt_process_ids=[child.process_id])
    before = deepcopy(process.context_snapshot)
    preview = preview_contract(process, candidate)
    assert process.context_snapshot == before and store.load_process_state(process.process_id) is None
    result = publish_contract(process, candidate, store=store)
    assert semantic_contract(result) == semantic_contract(preview)
    state = store.load_process_state(process.process_id)
    context = state["runtime_state"]["context_snapshot"]
    assert context["task_contract"] == result
    assert context["engineering_workflow"]["plan"]["digest"] == result["activation"]["workflow_digest"]
    assert state["runtime_state"]["delegated_contracts"][child.process_id]["required_for_parent_completion"]
    assert any(e.get("transaction_id") == result["activation"]["transaction_id"] for e in state["session"]["host_ledger"])
    assert process.approval_grants == [] and process.capability_lease is None


def test_omitted_workflow_preserves_active_requirements_and_preview_rejects_replacement(runtime):
    _, process, store, root = runtime
    publish_contract(process, proposal(root, engineering_workflow=plan()), store=store)
    result = publish_contract(process, proposal(root, expected_revision=1, acceptance_criteria=["New verification"]), store=store)
    assert result["engineering_workflow"]["digest"] == process.context_snapshot["engineering_workflow"]["plan"]["digest"]
    before = deepcopy(process.context_snapshot)
    replacement = plan()
    replacement["nodes"][0]["id"] = "easier"
    with pytest.raises(ValueError, match="extended"):
        preview_contract(process, proposal(root, expected_revision=2, engineering_workflow=replacement))
    assert process.context_snapshot == before


def test_checkpoint_failure_restores_contract_workflow_children_and_ledger(runtime, monkeypatch):
    _, process, store, root = runtime
    publish_contract(process, proposal(root), store=store)
    process.delegated_contracts["old"] = {"required_for_parent_completion": False}
    before = deepcopy((process.context_snapshot, process.context_version, process.delegated_contracts, process.session.host_ledger))
    monkeypatch.setattr(store, "save_process_checkpoint", lambda p: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        publish_contract(process, proposal(root, expected_revision=1, engineering_workflow=plan(), adopt_process_ids=["old"]), store=store)
    assert (process.context_snapshot, process.context_version, process.delegated_contracts, process.session.host_ledger) == before
    assert store.load_process_state(process.process_id)["runtime_state"]["context_snapshot"]["task_contract"]["revision"] == 1


def test_unknown_commit_blocks_new_effects_and_mode_changes(runtime, monkeypatch):
    _, process, store, root = runtime
    monkeypatch.setattr(store, "save_process_checkpoint", lambda p: (_ for _ in ()).throw(StorageCommitUnknown("scope commit")))
    with pytest.raises(StorageCommitUnknown):
        publish_contract(process, proposal(root), store=store)
    calls = []
    result = execute(process, root, unit(calls))
    assert result.is_error and calls == [] and process.session.mechanism_activation_blocked
    from backend.core.loop import mechanisms
    with pytest.raises(ValueError, match="ACTIVATION_BLOCKED"):
        mechanisms.activate(process, "manual", 0, store)


def test_two_revisions_from_same_base_have_one_winner_and_restore_exact_revision(runtime):
    _, process, store, root = runtime
    publish_contract(process, proposal(root), store=store)
    barrier = threading.Barrier(2)
    def revise(goal):
        candidate = proposal(root, expected_revision=1, goal=goal)
        barrier.wait(timeout=5)
        try:
            return publish_contract(process, candidate, store=store)
        except TaskContractRevisionConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(revise, ["First revision", "Second revision"]))
    assert sum(r is not None for r in results) == 1
    from backend.core.loop.recovery import restore_incomplete_processes
    from backend.core.loop.models import ProcessStatus
    recovered = AgentProcessManager()
    restore_incomplete_processes(store, recovered, str(root), include_process_ids=[process.process_id])
    restored = recovered.get(process.process_id)
    assert restored.status == ProcessStatus.RECOVERY_REVIEW_REQUIRED
    assert get_task_contract(restored) == next(r for r in results if r)


def test_protocol_revision_is_explicit_and_durable(runtime):
    _, process, store, root = runtime
    from backend.core.loop.executor import _build_internal_tools
    tool = _build_internal_tools(process, {}, str(root), object(), SimpleNamespace(_executors={}))["declare_task_contract"]
    first = tool.execute(proposal(root))
    assert first["accepted"] and first["activation"]["persistence"] == "checkpoint"
    missing = tool.execute(proposal(root, goal="New requirement"))
    assert missing["error"] == "TASK_CONTRACT_REVISION_CONFLICT" and missing["contract"]["revision"] == 1
    stale = tool.execute(proposal(root, expected_revision=0, goal="New requirement"))
    assert not stale["accepted"] and get_task_contract(process)["revision"] == 1
    changed = tool.execute(proposal(root, expected_revision=1, goal="New requirement", acceptance_criteria=["Verify the added behavior"]))
    assert changed["accepted"] and changed["activation"]["base_revision"] == 1 and not changed["authority_granted"]
    assert store.load_process_state(process.process_id)["runtime_state"]["context_snapshot"]["task_contract"]["goal"] == "New requirement"


def test_revision_between_preflight_and_admission_spends_no_permission(runtime):
    _, process, store, root = runtime
    from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
    publish_contract(process, proposal(root), store=store)
    calls = []
    candidate = unit(calls, approval=ApprovalMode.ASK)
    create_permission_request(process, {"tool_name": "unit", "arguments": {"value": "one"}, "purpose": "Fixture"}, {"unit": candidate}, str(root))
    grant = grant_from_decision(process, process.pending_decision, "allow_once")
    process.pending_decision = None
    bus = EventBus()
    bus.subscribe("ToolBeforeHooksStarted", lambda event: publish_contract(process, proposal(root, expected_revision=1, goal="Changed scope"), store=store))
    result = execute(process, root, candidate, bus)
    assert not bus.delivery_failures
    assert result.is_error and result.diagnostics["code"] == "TASK_CONTRACT_REVISION_CONFLICT"
    assert calls == [] and grant["remaining_uses"] == 1


def test_changed_user_evidence_requires_new_grill_review_at_final_boundary(runtime):
    _, process, store, root = runtime
    from backend.core.loop.alignment_evidence import alignment_input_digest
    from backend.core.loop.alignment_routing import REVIEW_POLICY
    publish_contract(process, proposal(root), store=store)
    process.update_context_snapshot(lambda c: {**c, "alignment_runtime": {"review_policy": REVIEW_POLICY,
        "task_id": process.active_task_id, "process_id": process.process_id, "request": "Old request"}})
    digest = alignment_input_digest(process)
    process.update_context_snapshot(lambda c: {**c, "alignment_runtime": {**c["alignment_runtime"],
        "admission_review": {"status": "ready", "input_digest": digest}}})
    bus = EventBus()
    bus.subscribe("ToolBeforeHooksStarted", lambda event: process.update_context_snapshot(lambda c: {**c,
        "alignment_runtime": {**c["alignment_runtime"], "request": "New user requirement"}}))
    calls = []
    result = execute(process, root, unit(calls), bus)
    assert result.is_error and result.diagnostics["code"] == "ALIGNMENT_REVIEW_REQUIRED" and calls == []


def test_started_effect_finishes_and_next_effect_uses_new_revision(runtime):
    _, process, store, root = runtime
    publish_contract(process, proposal(root), store=store)
    started, release = threading.Event(), threading.Event()
    calls = []
    def work(args):
        started.set()
        assert release.wait(timeout=5)
        calls.append(args["value"])
        return {"ok": True}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute, process, root, unit(calls, work))
        assert started.wait(timeout=5)
        publish_contract(process, proposal(root, expected_revision=1, goal="Extended scope"), store=store)
        release.set()
        first = future.result(timeout=5)
    second = execute(process, root, unit(calls))
    assert not first.is_error and not second.is_error and calls == ["one", "one"]
    assert first.receipt["task_contract_revision"] == 1 and second.receipt["task_contract_revision"] == 2


@pytest.fixture
def isolated_pipeline_runtime(tmp_path_factory):
    manager = AgentProcessManager()
    workspace = tmp_path_factory / 'workspace'
    workspace.mkdir()
    store = SessionStore(workspace, state_home=tmp_path_factory / 'state', migrate_legacy=False)
    manager.storage, manager.session_store = store._storage, store
    process = manager.fork(None, 'supervisor', ToolRegistry(['declare_task_contract']), 12,
        RingLevel.RING_0, workspace_path=str(workspace), task_id='scope-task',
        actor_kind='supervisor', capability_profile_id='supervisor.control')
    try:
        yield manager, process, store, workspace
    finally:
        store.close()


@pytest.mark.parametrize('adopt_self', [True, False])
def test_rejected_owner_adoption_leaves_mechanism_ready_for_corrected_contract(isolated_pipeline_runtime, adopt_self):
    from backend.core.loop.executor import _build_internal_tools
    from backend.core.mechanism_runtime.application import Application
    from backend.core.loop.task_maintenance import source_receipts
    from tests.test_mechanism_interactions import enable
    manager, process, store, root = isolated_pipeline_runtime
    manager.storage = store._storage
    process.session.append_user('Maintain this task and keep its existing completion observation.')
    publish_contract(process, proposal(root), store=store)
    app = Application(process)
    process._harness_application = app
    raw = {'schema_version': 2, 'id': 'owner-observer', 'intent': 'Observe completion',
        'source_refs': list(source_receipts(process)),
        'elements': [{'id': 'event', 'use': 'runtime.event', 'parameters': {'boundaries': ['completion']}},
            {'id': 'note', 'use': 'context.project', 'parameters': {'content': 'Completion evidence'}}],
        'bindings': [{'kind': 'trigger', 'from': {'node': 'event', 'port': 'go'}, 'to': {'node': 'note', 'port': 'go'}}]}
    enabled = enable(app, process, raw)
    foreign = manager.fork(None, 'worker', ToolRegistry([]), 4, RingLevel.RING_3,
        workspace_path=str(root), task_id='foreign')
    tool = _build_internal_tools(process, {}, str(root), object(), SimpleNamespace(_executors={}))['declare_task_contract']
    before = deepcopy(get_task_contract(process))
    ctx = ExecutionContext(process, process.session, str(root), storage=store._storage)
    args = proposal(root, expected_revision=before['revision'], routing_transition='continue_owner',
        adopt_process_ids=[process.process_id if adopt_self else foreign.process_id])
    rejected = tool.execute(args)
    assert rejected['error'] == 'INVALID_TASK_CONTRACT' and rejected['effect_state'] == 'not_committed'
    result = ToolPipeline().execute({'name': tool.name, 'args': args}, tool, ctx, 'invalid-owner')
    assert result.is_error and result.receipt.get('error_code') == 'INVALID_TOOL_ARGUMENTS', result.formatted
    assert result.receipt['effect_state'] == 'not_committed', result.formatted
    assert get_task_contract(process) == before
    current = app.repo.instance(app.task, enabled['id'])[0]
    assert current['phase'] == 'ready' and not current.get('active_calls')
    corrected = proposal(root, expected_revision=before['revision'], routing_transition='keep_supervisor')
    result = ToolPipeline().execute({'name': tool.name, 'args': corrected}, tool, ctx, 'correct-owner')
    assert not result.is_error and get_task_contract(process)['revision'] == before['revision'] + 1, result.formatted
    assert app.repo.instance(app.task, enabled['id'])[0]['phase'] == 'ready'


def test_scope_revision_invalidates_previous_delivery_alignment(runtime):
    _, process, store, root = runtime
    from backend.core.loop.alignment_evidence import alignment_input_digest, answer_digest
    from backend.core.loop.alignment_routing import REVIEW_POLICY
    from backend.core.loop.completion_protocol import HostCompletionEvaluator
    publish_contract(process, proposal(root), store=store)
    answer = "Verified feature behavior."
    from backend.core.loop.alignment_runtime import AlignmentRuntime
    AlignmentRuntime(process).payload("Update the requested feature")
    process.update_context_snapshot(lambda c: {**c, "alignment_runtime": {**c["alignment_runtime"], "review_policy": REVIEW_POLICY}})
    digest = alignment_input_digest(process)
    process.update_context_snapshot(lambda c: {**c, "alignment_runtime": {**c["alignment_runtime"],
        "delivery": {"input_digest": digest, "candidate_digest": answer_digest(answer), "source": "agent"}}})
    assert HostCompletionEvaluator.evaluate(process, answer).allowed
    publish_contract(process, proposal(root, expected_revision=1, acceptance_criteria=["Verify the newly adopted requirement"]), store=store)
    evaluation = HostCompletionEvaluator.evaluate(process, answer)
    assert not evaluation.allowed
    assert any(g["code"] == "ALIGNMENT_REVIEW_REQUIRED" for g in evaluation.gates)
