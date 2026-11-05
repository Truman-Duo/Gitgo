"""Semantic proposals cannot bypass task, authorization or durable lifecycle boundaries."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.models import RingLevel
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.execution_contract import data_broker
from backend.core.loop.mechanism_catalog import compile_definition
from backend.core.loop.task_contract import validate_contract_proposal, publish_contract, preview_contract
from backend.core.loop.task_maintenance import state_for, source_receipts, guard, synchronize, invoke
from backend.core.loop.discussion_adoption import save_discussion, adopt_discussion
from backend.core.loop.tool_surface_runtime import ToolSurfaceRuntime
from backend.core.loop import mechanisms


@pytest.fixture
def runtime(tmp_path_factory):
    manager = AgentProcessManager()
    store = SessionStore(tmp_path_factory, state_home=tmp_path_factory / "state", migrate_legacy=False)
    manager.session_store = store
    manager.storage = store._storage
    process = manager.fork(None, "supervisor", ToolRegistry(["unit", "other"]), 30, RingLevel.RING_0,
        workspace_path=str(tmp_path_factory), task_id="maintenance-task", actor_kind="supervisor",
        capability_profile_id="supervisor.control")
    process.session.append_user("Implement the feature, update its consumers and verify it.")
    try:
        yield manager, process, store, tmp_path_factory
    finally:
        store.close()


def plan(dependencies=None):
    return {"summary": "Update behavior and its real consumers", "steps": [
        {"id": "implement", "goal": "Update the feature and consumer", "tools": ["unit"], "resources": ["feature.py"],
         "verification": "Run its contract tests", "depends_on": []}], "dependencies": dependencies or []}


def raw(process, *, revision=0, maintenance_revision=0, mode=None, dependencies=None):
    elements = [{"use": use} for use in ("task.plan", "task.discussion", "task.related_work", "task.lesson")]
    if mode:
        elements.append({"use": "task.advancement", "parameters": {"mode": mode}})
    return {"goal": "Implement the requested feature", "execution_mode": "self_execute", "expected_revision": revision,
        "deliverables": [{"kind": "response", "required": True}], "acceptance_criteria": ["Consumer and feature contracts agree"],
        "mechanism": {"intent": "Preserve the full task goal", "source_refs": list(source_receipts(process))[:1], "elements": elements},
        "maintenance": {"expected_revision": maintenance_revision, "plan": plan(dependencies),
            "related_review": {"summary": "The view consumes the feature result; both are in scope", "candidates": []}}}


def publish(runtime, proposal):
    _, process, store, root = runtime
    return publish_contract(process, validate_contract_proposal(proposal, str(root)), store=store)


def surface(runtime):
    _, process, _, root = runtime
    value = ToolSurfaceRuntime(process, None, None, str(root), None)
    value.tools = {name: AgentTool(name=name, description="Fixture", parameters={"type": "object"},
        execute=lambda args: {}, read_only=False, effect=ToolEffect.PROCESS,
        execution_contract=data_broker("test.fixture")) for name in ("unit", "other")}
    value.refresh_prompt()
    return value


def test_definition_rejects_unregistered_code_cycles_and_privilege_parameters(runtime):
    _, process, _, _ = runtime
    candidate = raw(process)["mechanism"]
    assert compile_definition(candidate)["authority_granted"] is False
    for mutation in (
        {**candidate, "code": "eval(anything)"},
        {**candidate, "elements": [{"use": "invented.executor"}]},
        {**candidate, "elements": [{"use": "task.advancement", "parameters": {"mode": "auto", "permissions": "all"}}]},
        {**candidate, "elements": [{"id": "loop", "use": "task.plan"}], "bindings": [{"from": "loop", "to": "loop"}]},
    ):
        with pytest.raises(ValueError):
            compile_definition(mutation)


def test_preview_and_publish_share_compiler_and_commit_mode_plan_scope_together(runtime):
    _, process, store, root = runtime
    proposal = validate_contract_proposal(raw(process, mode="manual"), str(root))
    before = deepcopy(process.context_snapshot)
    candidate = preview_contract(process, proposal)
    assert process.context_snapshot == before and mechanisms.snapshot(process)["mode"] == "auto"
    from backend.core.loop.mechanism_catalog import digest
    proposal["preview_digest"] = digest(candidate)
    result = publish_contract(process, proposal, store=store)
    assert mechanisms.snapshot(process)["mode"] == "manual"
    state = store.load_process_state(process.process_id)
    assert state["runtime_state"]["context_snapshot"]["task_maintenance"]["plan"]["revision"] == 1
    assert state["session"]["mechanism_instances"][mechanisms.ADVANCEMENT]["parameters"]["mode"] == "manual"
    assert result["mechanism"]["digest"] == candidate["mechanism"]["digest"]
    assert guard(process, "unit")[0] == "TASK_MANIFEST_NOT_READY"
    surface(runtime)
    assert guard(process, "unit") is None
    assert process.approval_grants == [] and process.capability_lease is None


def test_plan_progress_does_not_create_another_scope_review_of_its_own_commands(runtime):
    _, process, _, _ = runtime
    publish(runtime, raw(process))
    update = raw(process, revision=1, maintenance_revision=state_for(process)["revision"])
    update["maintenance"].pop("related_review")
    update["maintenance"]["plan"]["summary"] = "Feature done; verify the same accepted consumer contract next"
    publish(runtime, update)
    state = state_for(process)
    assert state["phase"] == "ready" and state["review_requirements"] == {"plan": False, "related": False}
    # A real goal change still requires an explicit related-work review.
    changed = raw(process, revision=2, maintenance_revision=state["revision"])
    changed["goal"] = "Also change a second consumer"
    changed["maintenance"].pop("related_review")
    publish(runtime, changed)
    assert state_for(process)["review_requirements"]["related"] is True
    assert "related review" in guard(process, "unit")[1]


def test_mode_reconciliation_does_not_manufacture_new_business_scope(runtime):
    _, process, store, root = runtime
    publish(runtime, raw(process, mode='manual'))
    mechanisms.activate(process, 'auto', mechanisms.snapshot(process)['revision'], store)
    update = raw(process, revision=1, maintenance_revision=state_for(process)['revision'], mode='manual')
    update['maintenance'].pop('related_review')
    authored = deepcopy(update)
    compiled = preview_contract(process, validate_contract_proposal(update, str(root)))
    assert compiled['mechanism_parameter_resolution']['proposed'] == {'mode': 'manual'}
    assert compiled['mechanism_parameter_resolution']['effective'] == {'mode': 'auto'}
    assert update == authored and mechanisms.snapshot(process)['mode'] == 'auto'
    accepted = publish(runtime, update)
    assert accepted['mechanism'] == compiled['mechanism']
    assert state_for(process)['phase'] == 'ready'
    assert not any(state_for(process)['review_requirements'].values())
    assert mechanisms.snapshot(process)['mode'] == 'auto' and process.approval_grants == []


@pytest.mark.parametrize('diagnostics,receipt', [
    ({'nature': 'governance', 'code': 'SELF_EXECUTION_LEASE_REQUIRED', 'execution_state': 'not_started'}, {}),
    ({'nature': 'governance', 'code': 'TOOL_PERMISSION_DENIED'}, {}),
    ({'nature': 'system', 'code': 'RUNTIME_UNAVAILABLE'}, {}),
    ({'nature': 'business'}, {'diagnostic_scope': 'gitgo_runtime'}),
])
def test_internal_admission_failures_do_not_create_project_replanning(runtime, diagnostics, receipt):
    from backend.core.loop.task_maintenance import observe_result
    from backend.core.loop.tool_pipeline import ToolResult
    _, process, _, _ = runtime
    publish(runtime, raw(process))
    before = state_for(process)
    result = ToolResult(tool_name='unit', is_error=True, diagnostics=diagnostics,
                        receipt={'receipt_id': 'runtime-failure', **receipt})
    observe_result(process, result)
    assert state_for(process) == before
    assert result.is_error and result.receipt['receipt_id'] == 'runtime-failure'


def test_business_test_failure_still_requires_plan_reconciliation(runtime):
    from backend.core.loop.task_maintenance import observe_result
    from backend.core.loop.tool_pipeline import ToolResult
    _, process, _, _ = runtime
    publish(runtime, raw(process))
    observe_result(process, ToolResult(tool_name='run_test', is_error=True,
        diagnostics={'nature': 'business', 'code': 'TEST_FAILED'}, receipt={'receipt_id': 'test-failure'}))
    state = state_for(process)
    assert state['phase'] == 'needs_review'
    assert state['maintenance_items']['failure-test-failure']['status'] == 'pending'


def test_explicit_plan_retain_reconciles_real_failure_without_rewriting_steps(runtime):
    from backend.core.loop.task_maintenance import observe_result
    from backend.core.loop.tool_pipeline import ToolResult
    from backend.core.loop.executor import _build_internal_tools
    _, process, store, root = runtime
    publish(runtime, raw(process))
    observe_result(process, ToolResult(tool_name='run_test', is_error=True,
        diagnostics={'nature': 'business', 'code': 'TEST_FAILED'}, receipt={'receipt_id': 'baseline'}))
    before = state_for(process)
    tool = _build_internal_tools(process, {}, str(root), object(), SimpleNamespace(_executors={}))['declare_task_contract']
    args = {'operation': 'revise', 'plan_review': {'action': 'retain', 'reason': 'Expected baseline failure is covered by the repair and contract-test step.'},
        'maintenance': {'resolve_maintenance': ['failure-baseline']}}
    from jsonschema import Draft202012Validator
    Draft202012Validator(tool.to_openai_function()['function']['parameters']).validate(args)
    reviewed = tool.alignment_contract(args)
    assert reviewed['maintenance_intent']['plan_review'] == args['plan_review']
    assert state_for(process) == before  # review preparation does not clear the failure
    result = tool.execute(args)
    assert 'error' not in result, result
    after = state_for(process)
    assert after['plan']['steps'] == before['plan']['steps'] and after['plan']['dependencies'] == before['plan']['dependencies']
    assert after['maintenance_items']['failure-baseline']['status'] == 'resolved'
    assert after['plan_review']['reason'] == args['plan_review']['reason']
    assert not process.approval_grants
    saved = store.load_process_state(process.process_id)['runtime_state']['context_snapshot']['task_maintenance']
    assert saved['plan_review'] == after['plan_review']


@pytest.mark.parametrize('failure', ['scope', 'trigger', 'stale', 'no_plan', 'unavailable'])
def test_plan_retain_cannot_erase_scope_dependencies_or_concurrency(runtime, failure):
    from backend.core.loop.task_maintenance import observe_result
    from backend.core.loop.tool_pipeline import ToolResult
    _, process, _, root = runtime
    deps = []
    if failure == 'unavailable':
        (root / 'oversized.txt').write_bytes(b'x' * 1000001)
        deps = [{'kind': 'file', 'id': 'oversized.txt', 'affected_tools': ['unit']}]
    publish(runtime, raw(process, dependencies=deps))
    observe_result(process, ToolResult(tool_name='run_test', is_error=True,
        diagnostics={'nature': 'business', 'code': 'TEST_FAILED'}, receipt={'receipt_id': 'baseline'}))
    if failure == 'unavailable':
        synchronize(process, surface(runtime).tools)
    if failure == 'no_plan':
        process.update_context_snapshot(lambda value: {**value, 'task_maintenance': {**value['task_maintenance'], 'plan': {}}})
    before = deepcopy(process.context_snapshot)
    state = state_for(process)
    proposal = raw(process, revision=1, maintenance_revision=state['revision'])
    proposal['maintenance'].pop('plan')
    proposal['maintenance']['plan_review'] = {'action': 'retain', 'reason': 'Current plan covers the observed failure.'}
    proposal['maintenance']['resolve_maintenance'] = list(state['maintenance_items'])
    if failure == 'scope':
        proposal['goal'] = 'Also change the public interface'
    elif failure == 'trigger':
        proposal['maintenance']['resolve_maintenance'] = ['invented']
    elif failure == 'stale':
        proposal['maintenance']['expected_revision'] -= 1
    with pytest.raises(ValueError):
        publish(runtime, proposal)
    assert process.context_snapshot == before


def test_declaration_reports_unfinished_reconciliation_and_its_resolution(runtime):
    from backend.core.loop.executor import _build_internal_tools
    _, process, _, root = runtime
    publish(runtime, raw(process))
    tool = _build_internal_tools(process, {}, str(root), object(), SimpleNamespace(_executors={}))['declare_task_contract']
    update = raw(process, revision=1, maintenance_revision=state_for(process)['revision'])
    update['goal'] = 'Also update another consumer'
    update['maintenance'].pop('related_review')
    receipt = tool.execute(update)
    assert receipt['accepted'] and not receipt['authority_granted']
    assert receipt['maintenance_status']['review_requirements']['related']
    assert receipt['maintenance_status']['next_action'] == 'reconcile_with_declare_task_contract'
    update = raw(process, revision=2, maintenance_revision=state_for(process)['revision'])
    update['goal'] = 'Also update another consumer'
    receipt = tool.execute(update)
    assert receipt['accepted'] and receipt['maintenance_status']['phase'] == 'ready'
    assert not receipt['maintenance_status']['next_action']


def test_known_maintenance_gate_defers_paid_effect_review_but_not_other_boundaries(runtime):
    from backend.core.loop.tool_boundary_preview import preview_tool_boundaries
    _, process, _, _ = runtime
    publish(runtime, raw(process))
    value = surface(runtime)
    update = raw(process, revision=1, maintenance_revision=state_for(process)['revision'])
    update['goal'] = 'Another consumer must also be updated'
    update['maintenance'].pop('related_review')
    publish(runtime, update)
    calls = [{'name': 'unit', 'args': {}}]
    assert not preview_tool_boundaries(calls, value.tools, process).effectful
    assert guard(process, 'unit')[0] == 'TASK_MAINTENANCE_REQUIRED'
    # A control-plane proposal in the same batch still needs its own review.
    value.tools['declare_task_contract'] = AgentTool(name='declare_task_contract', description='control',
        parameters={}, execute=lambda args: {}, read_only=True, effect=ToolEffect.READ,
        alignment_contract=lambda args: {'goal': 'Reconciled proposal'})
    mixed = preview_tool_boundaries(calls + [{'name': 'declare_task_contract', 'args': {}}], value.tools, process)
    assert mixed.contracts == [{'goal': 'Reconciled proposal'}]
    update = raw(process, revision=2, maintenance_revision=state_for(process)['revision'])
    update['goal'] = 'Another consumer must also be updated'
    publish(runtime, update)
    value.refresh_prompt()
    assert guard(process, 'unit') is None
    assert preview_tool_boundaries(calls, value.tools, process).effectful


def test_mode_rebase_preview_is_invalidated_by_later_actual_user_switch(runtime):
    _, process, store, root = runtime
    publish(runtime, raw(process, mode='manual'))
    mechanisms.activate(process, 'auto', mechanisms.snapshot(process)['revision'], store)
    update = raw(process, revision=1, maintenance_revision=state_for(process)['revision'], mode='manual')
    from backend.core.loop.mechanism_catalog import digest
    preview = preview_contract(process, validate_contract_proposal(update, str(root)))
    update['preview_digest'] = digest(preview)
    mechanisms.activate(process, 'manual', mechanisms.snapshot(process)['revision'], store)
    before = deepcopy(process.context_snapshot)
    with pytest.raises(ValueError, match='PREVIEW_CHANGED'):
        publish(runtime, update)
    assert process.context_snapshot == before and mechanisms.snapshot(process)['mode'] == 'manual'


def test_failed_combined_activation_rolls_back_all_mode_plan_and_contract_state(runtime, monkeypatch):
    _, process, store, _ = runtime
    before = deepcopy((process.context_snapshot, process.session.mechanism_instances, process.session.host_ledger))
    monkeypatch.setattr(store, "save_process_checkpoint", lambda p: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        publish(runtime, raw(process, mode="manual"))
    assert (process.context_snapshot, process.session.mechanism_instances, process.session.host_ledger) == before


def discussion(runtime, question="Add a zero-total boundary check"):
    manager, process, store, _ = runtime
    return save_discussion(manager.storage, "sidecar", question,
        {"answer": "The display must format zero as 0.00 and test it.", "status": "completed"},
        [{"process_id": process.process_id, "task_id": process.active_task_id,
          "contract_revision": (process.context_snapshot.get("task_contract") or {}).get("revision", 0)}])


def adopt(runtime, ref, revision=1):
    manager, process, store, _ = runtime
    return adopt_discussion(process, manager.storage, sidecar_id="sidecar", discussion_ref=ref["discussion_ref"],
        expected_task_id=process.active_task_id, expected_contract_revision=revision, store=store)


def test_readonly_discussion_has_no_effect_until_explicit_adoption_then_reconciles_atomically(runtime):
    _, process, store, _ = runtime
    publish(runtime, raw(process))
    value = surface(runtime)
    ref = discussion(runtime)
    assert not state_for(process)["changes"] and guard(process, "unit") is None
    adopted = adopt(runtime, ref)
    current = state_for(process)
    assert adopted["status"] == "pending" and guard(process, "unit")[0] == "TASK_MAINTENANCE_REQUIRED"
    assert adopt(runtime, ref) == adopted and state_for(process)["revision"] == current["revision"]
    # Repeating the conclusion as goal text cannot close the pending adoption.
    partial = raw(process, revision=1, maintenance_revision=current["revision"])
    publish(runtime, partial)
    assert state_for(process)["changes"][adopted["change"]["id"]]["status"] == "pending"
    current = state_for(process)
    proposal = raw(process, revision=2, maintenance_revision=current["revision"])
    proposal["acceptance_criteria"].append("Zero is rendered as 0.00")
    proposal["maintenance"]["resolve_changes"] = [{"id": adopted["change"]["id"], "reason": "Include zero in the consumer contract",
        "contract_fields": ["acceptance_criteria"], "question_ids": []}]
    publish(runtime, proposal)
    value.refresh_prompt()
    saved = store.load_process_state(process.process_id)["runtime_state"]["context_snapshot"]["task_maintenance"]
    assert saved["changes"][adopted["change"]["id"]]["status"] == "adopted"
    assert saved["changes"][adopted["change"]["id"]]["contract_revision"] == 3 and guard(process, "unit") is None


def test_wrong_task_stale_revision_unregistered_source_and_failed_discussion_cannot_adopt(runtime):
    manager, process, store, _ = runtime
    publish(runtime, raw(process))
    ref = discussion(runtime)
    with pytest.raises(ValueError, match="TARGET_CHANGED"):
        adopt(runtime, ref, 0)
    process.active_task_id = "other-task"
    with pytest.raises(ValueError, match="TARGET_CHANGED"):
        adopt(runtime, ref)
    process.active_task_id = "maintenance-task"
    forged = dict(json.loads(manager.storage.read_blob(ref["discussion_ref"])), revision=9)
    forged_ref = manager.storage.put_blob(json.dumps(forged).encode())
    with pytest.raises(ValueError, match="UNREGISTERED"):
        adopt(runtime, {"discussion_ref": forged_ref})
    failed = save_discussion(manager.storage, "sidecar", "failed", {"answer": "No result", "status": "failed"}, [])
    with pytest.raises(ValueError, match="NOT_ADOPTABLE"):
        adopt(runtime, failed)


def test_hotplug_dependency_baseline_and_retirement_do_not_invent_changes(runtime):
    _, process, _, root = runtime
    (root / "rules.txt").write_text("one", encoding="utf-8")
    minimal = raw(process)
    minimal["mechanism"]["elements"] = []
    minimal.pop("maintenance")
    publish(runtime, minimal)
    value = surface(runtime)
    synchronize(process, value.tools)
    assert state_for(process)["dependency_versions"] == {}
    deps = [{"kind": "file", "id": "rules.txt", "affected_tools": ["unit"]}]
    publish(runtime, raw(process, revision=1, maintenance_revision=state_for(process)["revision"], dependencies=deps))
    value.synchronize()
    assert state_for(process)["phase"] == "ready"
    assert state_for(process)["maintenance_items"] == {}
    assert "file:rules.txt" in state_for(process)["dependency_versions"]
    publish(runtime, raw(process, revision=2, maintenance_revision=state_for(process)["revision"]))
    value.synchronize()
    assert state_for(process)["phase"] == "ready"
    assert state_for(process)["maintenance_items"] == {}


def test_declared_dependency_change_invalidates_only_affected_actions_and_is_deduplicated(runtime):
    _, process, _, root = runtime
    (root / "rules.txt").write_text("one", encoding="utf-8")
    deps = [{"kind": "file", "id": "rules.txt", "affected_tools": ["unit"]}]
    publish(runtime, raw(process, dependencies=deps))
    value = surface(runtime)
    synchronize(process, value.tools)
    (root / "unrelated.txt").write_text("unrelated", encoding="utf-8")
    assert not synchronize(process, value.tools)
    (root / "rules.txt").write_text("two", encoding="utf-8")
    assert synchronize(process, value.tools)
    current = state_for(process)
    assert guard(process, "unit")[0] == "TASK_MAINTENANCE_REQUIRED" and guard(process, "other") is None
    assert guard(process) and not synchronize(process, value.tools)
    assert state_for(process)["revision"] == current["revision"]
    revised = raw(process, revision=1, maintenance_revision=current["revision"], dependencies=deps)
    revised["maintenance"]["resolve_maintenance"] = list(current["maintenance_items"])
    publish(runtime, revised)
    value.synchronize()
    assert state_for(process)["phase"] == "ready" and guard(process, "unit") is None


def test_active_plan_survives_suspend_resume_and_pending_requirements_cannot_detach(runtime):
    _, process, _, _ = runtime
    publish(runtime, raw(process))
    value = surface(runtime)
    initial_plan = deepcopy(state_for(process)["plan"])
    invoke(process, {"operation": "suspend", "expected_revision": state_for(process)["revision"]})
    assert guard(process, "unit")[0] == "TASK_MECHANISM_SUSPENDED"
    invoke(process, {"operation": "resume", "expected_revision": state_for(process)["revision"]})
    value.synchronize()
    assert state_for(process)["plan"] == initial_plan and guard(process, "unit") is None
    adopt(runtime, discussion(runtime))
    with pytest.raises(ValueError, match="obligations"):
        invoke(process, {"operation": "detach", "expected_revision": state_for(process)["revision"]})


def test_pending_parent_scope_blocks_child_effects_without_cancelling_llm(runtime):
    manager, parent, _, root = runtime
    publish(runtime, raw(parent))
    surface(runtime)
    child = manager.fork(parent.process_id, "worker", ToolRegistry(["unit"]), 5, RingLevel.RING_3,
        workspace_path=str(root), task_id="child")
    assert guard(child, "unit") is None
    adopt(runtime, discussion(runtime))
    assert guard(child, "unit")[0] == "TASK_MAINTENANCE_REQUIRED" and not child.cancel_requested


def test_parent_local_projection_does_not_deadlock_waiting_child(runtime):
    manager, parent, _, root = runtime
    publish(runtime, raw(parent))
    surface(runtime)
    child = manager.fork(parent.process_id, 'worker', ToolRegistry(['unit']), 5, RingLevel.RING_3,
        workspace_path=str(root), task_id='child')
    invoke(parent, {'operation':'suspend', 'expected_revision':state_for(parent)['revision']}, user_control=True)
    assert guard(child, 'unit')[0] == 'TASK_MECHANISM_SUSPENDED'
    invoke(parent, {'operation':'resume', 'expected_revision':state_for(parent)['revision']}, user_control=True)
    assert guard(parent, 'unit')[0] == 'TASK_MANIFEST_NOT_READY'
    assert guard(child, 'unit') is None
    adopt(runtime, discussion(runtime))
    assert guard(child, 'unit')[0] == 'TASK_MAINTENANCE_REQUIRED'


def test_child_user_adoption_mounts_only_bounded_scope_control_and_preserves_assigned_obligations(runtime):
    manager, parent, store, root = runtime
    publish(runtime, raw(parent))
    child = manager.fork(parent.process_id, "worker", ToolRegistry(["unit"]), 8, RingLevel.RING_3,
        workspace_path=str(root), task_id="child-scope", actor_kind="worker", capability_profile_id="development.workspace",
        context_snapshot={"task_contract": {"goal": "Original assignment", "acceptance_criteria": ["Keep original requirement"],
            "target_files": ["feature.py"], "allowed_tools": ["unit"]}})
    ref = save_discussion(manager.storage, "child-discussion", "Add consumer validation",
        {"answer": "Keep the assigned goal and add its missing consumer test", "status": "completed"},
        [{"process_id": child.process_id, "task_id": child.active_task_id, "contract_revision": 0}])
    adopted = adopt_discussion(child, manager.storage, sidecar_id="child-discussion", discussion_ref=ref["discussion_ref"],
        expected_task_id=child.active_task_id, expected_contract_revision=0, store=store)
    assert set(child.tool_registry.list_all()) == {"unit", "declare_task_contract"}
    from backend.core.loop.executor import _build_internal_tools
    tool = _build_internal_tools(child, {}, str(root), object(), SimpleNamespace(_executors={}))["declare_task_contract"]
    proposal = {"goal": "Keep the assignment and add consumer validation", "execution_mode": "self_execute",
        "acceptance_criteria": ["Keep original requirement", "Consumer rejects invalid input"], "deliverables": [],
        "maintenance": {"expected_revision": state_for(child)["revision"], "plan": plan(),
            "resolve_changes": [{"id": adopted["change"]["id"], "reason": "Add consumer coverage within assigned scope",
                                  "contract_fields": ["acceptance_criteria"], "question_ids": []}]}}
    invalid = deepcopy(proposal)
    invalid["acceptance_criteria"] = ["Easier replacement"]
    assert not tool.execute(invalid)["accepted"]
    result = tool.execute(proposal)
    assert result["accepted"] and result["contract"]["target_files"] == ["feature.py"]
    assert result["contract"]["allowed_tools"] == ["unit"] and child.approval_grants == [] and child.capability_lease is None


def test_known_child_scope_change_reaches_owner_even_if_notification_was_lost(runtime):
    manager, parent, store, root = runtime
    publish(runtime, raw(parent))
    value = surface(runtime)
    value.synchronize()
    child = manager.fork(parent.process_id, "worker", ToolRegistry(["unit"]), 6, RingLevel.RING_3,
        workspace_path=str(root), task_id="child-scope")
    from backend.core.loop.task_maintenance import add_change
    add_change(child, "Actual explicitly adopted child requirement", identifier="child-change", source_ref="fixture-ref",
        expected_task_id=child.active_task_id, expected_contract_revision=0, store=store)
    assert synchronize(parent, value.tools)
    pending = list(state_for(parent)["maintenance_items"].values())
    assert any(item["source"] == "child_scope:" + child.process_id for item in pending)
    assert guard(parent, "unit")[0] == "TASK_MAINTENANCE_REQUIRED"
    from backend.core.loop.task_maintenance import inspect
    changes = inspect(parent)["owned_scope_updates"]
    assert changes[0]["process_id"] == child.process_id
    assert changes[0]["changes"]["child-change"]["content"] == "Actual explicitly adopted child requirement"


def test_compaction_and_restart_retain_source_plan_and_pending_change(runtime):
    manager, process, store, root = runtime
    publish(runtime, raw(process))
    adopted = adopt(runtime, discussion(runtime))
    before = deepcopy(state_for(process))
    process.session.messages = []  # Dropped public history is not authoritative control state.
    assert set(before["source_receipts"]).issubset(source_receipts(process))
    store.save_process_checkpoint(process)
    recovered = AgentProcessManager()
    from backend.core.loop.recovery import restore_incomplete_processes
    restore_incomplete_processes(store, recovered, str(root), include_process_ids=[process.process_id])
    restored = recovered.get(process.process_id)
    assert state_for(restored) == before
    assert state_for(restored)["changes"][adopted["change"]["id"]]["status"] == "pending"
    assert guard(restored, "unit")[0] == "TASK_MAINTENANCE_REQUIRED"


def test_newer_user_mode_selection_survives_old_natural_language_mechanism(runtime):
    _, process, store, _ = runtime
    publish(runtime, raw(process))
    mechanisms.activate(process, "manual", 0, store)
    before = deepcopy(process.context_snapshot)
    stale = raw(process, revision=1, maintenance_revision=1, mode="auto")
    preview = preview_contract(process, validate_contract_proposal(stale, process.worktree_path))
    assert mechanisms.snapshot(process)["mode"] == "manual" and process.context_snapshot == before
    assert preview["mechanism_parameter_resolution"]["effective"] == {"mode": "manual"}
    accepted = publish(runtime, stale)
    assert accepted['mechanism'] == preview['mechanism']
    assert mechanisms.snapshot(process)["mode"] == "manual"
    from backend.core.loop.mechanism_lifecycle import next_authority_sequence
    from backend.core.loop.mechanism_catalog import digest
    content = "Please switch back to automatic advancement."
    ref = "user:" + digest(content)
    process.session.host_ledger.append({"event": "task_user_instruction_received", "task_id": process.active_task_id,
        "source_ref": ref, "content": content, "authority_sequence": next_authority_sequence(process.session)})
    stale["mechanism"]["source_refs"] = [ref]
    stale['expected_revision'] = 2
    stale['maintenance']['expected_revision'] = state_for(process)['revision']
    publish(runtime, stale)
    assert mechanisms.snapshot(process)["mode"] == "auto" and process.approval_grants == []


def test_authority_order_survives_trimmed_ledger_and_durable_restore(runtime):
    _, process, store, _ = runtime
    from backend.core.loop.mechanism_lifecycle import next_authority_sequence
    mechanisms.activate(process, "manual", 0, store)
    last = next_authority_sequence(process.session)
    process.session.host_ledger = []
    store.save_process_checkpoint(process)
    from backend.core.loop.session import AgentSession
    restored = AgentSession.from_durable_state(store.load_process_state(process.process_id)["session"])
    assert next_authority_sequence(restored) > last


@pytest.mark.parametrize("name", ["../outside", ".git/config", ".gitgo/state.json", "C:/private.txt", ".git /config"])
def test_declared_dependencies_cannot_select_nonbusiness_or_escaped_paths(runtime, name):
    _, process, _, _ = runtime
    with pytest.raises((ValueError, OSError)):
        publish(runtime, raw(process, dependencies=[{"kind": "file", "id": name}]))
    assert not state_for(process)


def test_unavailable_input_cannot_be_silently_closed_or_repeatedly_replanned(runtime):
    _, process, _, root = runtime
    (root / "oversized.txt").write_bytes(b"x" * 1000001)
    deps = [{"kind": "file", "id": "oversized.txt", "affected_tools": ["unit"]}]
    publish(runtime, raw(process, dependencies=deps))
    value = surface(runtime)
    assert synchronize(process, value.tools)
    current = state_for(process)
    assert current["phase"] == "needs_review" and len(current["maintenance_items"]) == 1
    assert not synchronize(process, value.tools) and state_for(process) == current
    proposal = raw(process, revision=1, maintenance_revision=current["revision"], dependencies=deps)
    proposal["maintenance"]["resolve_maintenance"] = list(current["maintenance_items"])
    with pytest.raises(ValueError, match="unavailable"):
        publish(runtime, proposal)


def test_tool_schema_change_invalidates_the_plan_and_old_projection(runtime):
    _, process, _, _ = runtime
    publish(runtime, raw(process, dependencies=[{"kind": "tool", "id": "unit", "affected_tools": ["unit"]}]))
    value = surface(runtime)
    value.synchronize()
    before = deepcopy(process._mechanism_projection)
    value.tools["unit"].parameters = {"type": "object", "required": ["new_parameter"]}
    value.synchronize()
    current = state_for(process)
    assert current["phase"] == "needs_review"
    assert any(item["source"] == "tool:unit" for item in current["maintenance_items"].values())
    assert process._mechanism_projection["tool_digest"] != before["tool_digest"]
    assert process._mechanism_projection["epoch"] == current["epoch"]
    assert guard(process, "unit")[0] == "TASK_MAINTENANCE_REQUIRED" and guard(process, "other") is None


def test_lesson_statistics_do_not_invalidate_but_content_changes_do(runtime):
    manager, process, _, _ = runtime
    from backend.core.knowledge.models import Lesson
    from backend.core.loop.mechanism_catalog import digest
    lesson = Lesson(id="input-rule", project_name="fixture", trigger="feature.py", rule="Reject invalid input")
    def save():
        raw_lesson = lesson.to_dict()
        manager.storage.upsert_lesson_record(raw_lesson, scope="instance", content_hash=digest(raw_lesson))
    save()
    deps = [{"kind": "lesson", "id": lesson.id, "affected_tools": ["unit"]}]
    publish(runtime, raw(process, dependencies=deps))
    value = surface(runtime)
    value.synchronize()
    lesson.trigger_count += 1
    save()
    assert not synchronize(process, value.tools)
    lesson.rule = "Reject invalid input and preserve its source"
    save()
    assert synchronize(process, value.tools)
    assert guard(process, "unit")[0] == "TASK_MAINTENANCE_REQUIRED"
    assert any(v["source"] == "lesson:" + lesson.id for v in state_for(process)["maintenance_items"].values())


def test_informational_feedback_interpretation_cannot_dismiss_adopted_btw(runtime):
    _, process, store, _ = runtime
    publish(runtime, raw(process))
    from backend.core.loop.task_maintenance import add_change
    ordinary = add_change(process, "What is the current progress?", identifier="feedback", source_ref="user:fixture",
        expected_task_id=process.active_task_id, expected_contract_revision=1, store=store, user_adopted=False)
    invoke(process, {"operation": "interpret_update", "change_id": ordinary["id"], "expected_revision": state_for(process)["revision"],
        "disposition": "question", "reason": "The user asks for status without changing scope"})
    assert state_for(process)["changes"][ordinary["id"]]["status"] == "acknowledged"
    adopted = adopt(runtime, discussion(runtime))
    with pytest.raises(ValueError, match="informational"):
        invoke(process, {"operation": "interpret_update", "change_id": adopted["change"]["id"],
            "expected_revision": state_for(process)["revision"], "disposition": "information", "reason": "Skip it"})
    assert state_for(process)["changes"][adopted["change"]["id"]]["status"] == "pending"


def test_related_review_refresh_does_not_erase_adopted_candidate(runtime):
    _, process, _, _ = runtime
    candidate = {"source": "feature.py", "affected": "consumer.py", "reason": "Consumes the changed value",
        "consequence": "Would display the old units", "work": "Update display units", "verification": "Consumer assertion",
        "owner": "main task", "evidence_kind": "observed"}
    proposal = raw(process)
    proposal["maintenance"]["related_review"]["candidates"] = [candidate]
    publish(runtime, proposal)
    identifier = next(iter(state_for(process)["related"]["candidates"]))
    proposal = raw(process, revision=1, maintenance_revision=1)
    proposal["maintenance"]["adopt_related"] = [identifier]
    publish(runtime, proposal)
    proposal = raw(process, revision=2, maintenance_revision=2)
    proposal["maintenance"]["related_review"]["candidates"] = [candidate]
    publish(runtime, proposal)
    assert state_for(process)["related"]["candidates"][identifier]["status"] == "adopted"
    assert state_for(process)["related"]["candidates"][identifier]["contract_revision"] == 2
    original_adoption = deepcopy(state_for(process)['related']['candidates'][identifier])
    repeated = raw(process, revision=3, maintenance_revision=state_for(process)['revision'])
    repeated['maintenance'] = {'expected_revision': state_for(process)['revision'], 'adopt_related': [identifier]}
    publish(runtime, repeated)
    assert state_for(process)['related']['candidates'][identifier] == original_adoption
    assert state_for(process)['phase'] == 'ready'


def test_uncertain_change_commit_blocks_all_new_effects_without_claiming_rollback(runtime, monkeypatch):
    _, process, store, _ = runtime
    publish(runtime, raw(process))
    ref = discussion(runtime)
    from backend.core.storage import StorageCommitUnknown
    monkeypatch.setattr(store, "save_process_checkpoint", lambda p: (_ for _ in ()).throw(StorageCommitUnknown("unknown")))
    with pytest.raises(StorageCommitUnknown):
        adopt(runtime, ref)
    assert process.session.mechanism_activation_blocked
    assert state_for(process)["phase"] == "needs_review"
    assert process.approval_grants == []


def test_withdrawal_requires_newer_real_user_evidence_and_retains_history(runtime):
    _, process, _, _ = runtime
    publish(runtime, raw(process))
    adopted = adopt(runtime, discussion(runtime))
    proposal = raw(process, revision=1, maintenance_revision=state_for(process)["revision"])
    resolution = {"id": adopted["change"]["id"], "disposition": "withdraw", "reason": "The user retracted it",
        "contract_fields": ["acceptance_criteria"], "source_ref": "user:invented", "quote": "Withdraw the zero boundary requirement."}
    proposal["maintenance"]["resolve_changes"] = [resolution]
    with pytest.raises(ValueError, match="newer actual"):
        publish(runtime, proposal)
    from backend.core.loop.mechanism_lifecycle import next_authority_sequence
    from backend.core.loop.mechanism_catalog import digest
    content = resolution["quote"]
    ref = "user:" + digest(content)
    process.session.host_ledger.append({"event": "task_user_instruction_received", "task_id": process.active_task_id,
        "source_ref": ref, "content": content, "authority_sequence": next_authority_sequence(process.session)})
    resolution["source_ref"] = ref
    publish(runtime, proposal)
    stored = state_for(process)["changes"][adopted["change"]["id"]]
    assert stored["status"] == "withdrawn" and stored["content"] == adopted["change"]["content"]


def test_related_work_adoption_is_not_execution_or_task_completion(runtime):
    _, process, _, _ = runtime
    proposal = raw(process)
    proposal["maintenance"]["related_review"]["candidates"] = [{"source": "feature.py", "affected": "consumer.py",
        "reason": "Uses the changed units", "consequence": "Wrong display", "work": "Update consumer",
        "verification": "Consumer assertion", "owner": "main", "evidence_kind": "observed"}]
    publish(runtime, proposal)
    identifier = next(iter(state_for(process)["related"]["candidates"]))
    proposal = raw(process, revision=1, maintenance_revision=1)
    proposal["maintenance"]["adopt_related"] = [identifier]
    publish(runtime, proposal)
    from backend.core.loop.task_maintenance import inspect
    candidate = lambda: inspect(process)["state"]["related"]["candidates"][identifier]
    assert candidate()["execution"]["state"] == "awaiting_execution"
    process.tool_receipts.append({"receipt_id": "actual-output", "task_id": process.active_task_id,
        "tool_name": "unit", "effect": "workspace_write", "committed": True, "succeeded": True, "task_contract_revision": 2})
    assert candidate()["execution"]["state"] == "task_in_progress"
    process.result = {"outcome": {"status": "degraded"}}
    process.lifecycle_finalized = True
    assert candidate()["execution"]["state"] != "task_completed"
    process.result = {"outcome": {"status": "completed"}}
    assert candidate()["execution"]["state"] == "task_completed"
    assert candidate()["execution"]["scope"] == "containing_task"


def test_application_control_uses_same_task_state_and_rejects_stale_target(runtime):
    manager, process, store, root = runtime
    publish(runtime, raw(process))
    from backend.core.daemon.mechanism_control import command
    emitted = []
    ctx = {"apm": manager, "session_store": store}
    args = {"task_operation": "suspend", "process_id": process.process_id, "task_id": "other", "expected_revision": 1}
    command(args, SimpleNamespace(workspace_path=root), SimpleNamespace(name="fixture"), ctx, emitted.append)
    assert emitted[-1]["error"] == "TASK_MECHANISM_TARGET_CHANGED" and not state_for(process)["suspended"]
    args["task_id"] = process.active_task_id
    command(args, SimpleNamespace(workspace_path=root), SimpleNamespace(name="fixture"), ctx, emitted.append)
    assert emitted[-1]["result"]["state"]["suspended"] is True
    assert state_for(process)["suspended"] and guard(process, "unit")[0] == "TASK_MECHANISM_SUSPENDED"
    # Repeating an old UI action is a version conflict, never a fresh mutation.
    command(args, SimpleNamespace(workspace_path=root), SimpleNamespace(name="fixture"), ctx, emitted.append)
    assert "REVISION_CONFLICT" in emitted[-1]["error"]


def test_failed_user_feedback_checkpoint_retracts_mailbox_and_source_together(runtime, monkeypatch):
    manager, process, store, root = runtime
    publish(runtime, raw(process))
    before = deepcopy(process.context_snapshot), deepcopy(process.session.host_ledger)
    monkeypatch.setattr(store, "save_process_checkpoint", lambda p: (_ for _ in ()).throw(OSError("full")))
    from backend.core.daemon.dispatch import _cmd_task
    emitted = []
    _cmd_task({"action": "instruct", "process_id": process.process_id, "instruction": "Add a consumer check"},
        SimpleNamespace(workspace_path=root), SimpleNamespace(name="fixture"),
        {"apm": manager, "session_store": store}, emitted.append)
    assert emitted[-1]["error"] == "full"
    assert process.mailbox.snapshot()["pending"] == 0
    assert process.context_snapshot == before[0] and process.session.host_ledger == before[1]
