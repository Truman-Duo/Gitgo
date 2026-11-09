from copy import deepcopy

import pytest

from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.mechanism_lifecycle import next_authority_sequence
from backend.core.loop.recovery import restore_incomplete_processes
from backend.core.loop.task_contract import get_task_contract, preview_contract, publish_contract
from backend.core.loop.task_maintenance import add_change, inspect, state_for
from tests.test_task_contract_activation import proposal, runtime


def _steps(*, split=False, remove=False):
    result = [
        {"id": "inspect", "goal": "Inspect the source", "verification": "Retain inspection evidence"},
        {"id": "independent", "goal": "Inspect the separate concern", "verification": "Retain its evidence"},
    ]
    if not remove:
        result.append({"id": "deliver", "goal": "Verify the updated behavior", "depends_on": ["inspect"],
                       "verification": "Verify the requested behavior"})
    if split:
        result.append({"id": "variant", "goal": "Verify the new variant", "depends_on": ["deliver"],
                       "verification": "Verify the additional behavior"})
    return result


def _workflow(*, linked=True, split=False, remove=False):
    nodes = [{"id": "retro", "kind": "report", "format": "retrospective"}]
    pairs = [("source_read", "inspect"), ("source_check", "inspect"), ("independent", "independent")]
    if not remove:
        pairs.append(("delivery_check", "deliver"))
    if split:
        pairs.append(("variant_check", "variant"))
    nodes.extend({"id": name, "kind": "observation", "tools": [name],
                  **({"work_item_id": item} if linked else {})} for name, item in pairs)
    return {"profiles": ["retrospective"], "nodes": nodes}


def _maintenance(process, steps, **fields):
    return {"expected_revision": state_for(process).get("revision", 0),
            "plan": {"summary": "Verify the requested change", "steps": steps},
            "related_review": {"summary": "No additional obligations identified in the fixture", "candidates": []},
            **fields}


def _receipt(process, name, revision):
    receipt_id = f"{name}-{len(process.tool_receipts)}"
    process.tool_receipts.append({"task_id": process.active_task_id, "task_contract_revision": revision,
        "tool_name": name, "receipt_id": receipt_id, "effect": "read", "succeeded": True, "committed": True})
    return receipt_id


def _nodes(process):
    return {n["id"]: n for n in EngineeringWorkflow(process).status()["nodes"]}


def _source(process, content):
    ref = f"user:fixture-{len(process.session.host_ledger)}"
    process.session.host_ledger.append({"event": "task_user_instruction_received",
        "task_id": process.active_task_id, "source_ref": ref, "content": content,
        "authority_sequence": next_authority_sequence(process.session)})
    return ref


def test_stable_items_expand_dependencies_and_only_reconcile_affected_receipts(runtime, monkeypatch):
    _, process, store, root = runtime
    first = publish_contract(process, proposal(root, engineering_workflow=_workflow(),
        maintenance=_maintenance(process, _steps())), store=store)
    nodes = _nodes(process)
    assert nodes["delivery_check"]["depends_on"] == ["source_check", "source_read"]
    assert inspect(process)["work_item_links"]["by_work_item"]["inspect"] == ["source_read", "source_check"]
    assert nodes["delivery_check"]["status"] == "blocked"
    for name in ("source_read", "source_check", "independent", "delivery_check"):
        _receipt(process, name, first["revision"])
    before = _nodes(process)
    assert before["delivery_check"]["status"] == "complete"
    reordered = publish_contract(process, proposal(root, expected_revision=1,
        maintenance=_maintenance(process, list(reversed(_steps())))), store=store)
    assert reordered["engineering_workflow"]["digest"] == first["engineering_workflow"]["digest"]
    assert _nodes(process)["delivery_check"]["evidence"] == before["delivery_check"]["evidence"]

    request = "Expand the requested behavior to include the extra variant."
    source = _source(process, request)
    add_change(process, request, identifier="variant-request", source_ref=source,
               expected_task_id=process.active_task_id, expected_contract_revision=2, store=store)
    reconcile = proposal(root, expected_revision=2, goal="Verify the behavior and the new variant",
        engineering_workflow=_workflow(split=True), maintenance=_maintenance(process, _steps(split=True),
            resolve_changes=[{"id": "variant-request", "reason": "Split the additional verification",
                "contract_fields": ["goal"], "work_item_ids": ["deliver", "variant"]}]))
    original = deepcopy((process.context_snapshot, process.context_version, process.session.host_ledger))
    with monkeypatch.context() as failure:
        def fail(_):
            raise OSError("fixture checkpoint failure")
        failure.setattr(store, "save_process_checkpoint", fail)
        with pytest.raises(OSError, match="fixture checkpoint failure"):
            publish_contract(process, reconcile, store=store)
    assert (process.context_snapshot, process.context_version, process.session.host_ledger) == original
    committed = publish_contract(process, reconcile, store=store)
    current = _nodes(process)
    assert current["independent"]["evidence"] == before["independent"]["evidence"]
    assert current["delivery_check"]["status"] == "ready"
    assert current["variant_check"]["status"] == "blocked"
    assert current["variant_check"]["depends_on"] == ["delivery_check", "source_check", "source_read"]
    archived = next(event for event in reversed(process.session.host_ledger)
                    if event.get("transaction_id") == committed["activation"]["transaction_id"])
    assert archived["retired_work_item_evidence"][0]["evidence"] == before["delivery_check"]["evidence"]
    _receipt(process, "delivery_check", 2)  # An old in-flight receipt can arrive after adoption.
    assert _nodes(process)["delivery_check"]["status"] == "ready"
    _receipt(process, "delivery_check", 3)
    _receipt(process, "variant_check", 3)
    assert _nodes(process)["variant_check"]["status"] == "complete"
    store.save_process_checkpoint(process)
    from backend.core.loop.manager import AgentProcessManager
    manager = AgentProcessManager()
    manager.session_store, manager.storage = store, store._storage
    restore_incomplete_processes(store, manager, root, include_process_ids=[process.process_id])
    restored = manager.get(process.process_id)
    assert _nodes(restored)["variant_check"]["evidence"] == _nodes(process)["variant_check"]["evidence"]
    assert state_for(restored)["changes"]["variant-request"]["status"] == "adopted"


def test_legacy_nodes_stay_unmapped_until_explicit_link_and_bad_links_do_not_commit(runtime):
    _, process, store, root = runtime
    publish_contract(process, proposal(root, engineering_workflow=_workflow(linked=False),
        maintenance=_maintenance(process, _steps())), store=store)
    links = inspect(process)["work_item_links"]
    assert "independent" in links["unmapped_nodes"] and not links["by_work_item"]["independent"]
    _receipt(process, "independent", 1)
    assert _nodes(process)["independent"]["status"] == "complete"
    before = deepcopy(process.context_snapshot)
    bad = _workflow()
    bad["nodes"][1]["work_item_id"] = "missing"
    with pytest.raises(ValueError, match="unknown work item"):
        preview_contract(process, proposal(root, expected_revision=1, engineering_workflow=bad))
    assert process.context_snapshot == before
    good = proposal(root, expected_revision=1, engineering_workflow=_workflow())
    publish_contract(process, good, store=store)
    assert _nodes(process)["independent"]["status"] == "ready"
    assert inspect(process)["work_item_links"]["unmapped_nodes"] == ["retro"]


def test_work_item_withdrawal_cannot_silently_drop_active_evidence(runtime):
    _, process, store, root = runtime
    publish_contract(process, proposal(root, engineering_workflow=_workflow(),
        maintenance=_maintenance(process, _steps())), store=store)
    with pytest.raises(ValueError, match="unknown work item"):
        publish_contract(process, proposal(root, expected_revision=1,
            maintenance=_maintenance(process, _steps(remove=True))), store=store)
    workflow = EngineeringWorkflow(process)
    change = workflow.propose_amendment(_workflow(remove=True), "Withdraw the delivery verification requirement.")
    assert "delivery_check" in _nodes(process)
    process.session.host_ledger.append({"event": "user_decision_received", "task_id": process.active_task_id,
        "decision_id": change["decision_id"], "selected_action": "accept_engineering_amendment", "answer": "accept"})
    process.pending_decision = None
    assert "delivery_check" not in _nodes(process)
    publish_contract(process, proposal(root, expected_revision=1,
        maintenance=_maintenance(process, _steps(remove=True))), store=store)
    assert "deliver" not in inspect(process)["work_item_links"]["by_work_item"]
    assert get_task_contract(process)["revision"] == 2


def test_generated_edges_satisfy_tdd_order_without_repeating_dependencies(runtime):
    _, process, store, root = runtime
    steps = [{"id": name, "goal": name, "verification": "Use the declared evidence"}
             for name in ("reproduce", "repair", "verify")]
    steps[1]["depends_on"] = ["reproduce"]
    steps[2]["depends_on"] = ["repair"]
    workflow = {"profiles": ["tdd"], "nodes": [
        {"id": "red", "kind": "check", "test_id": "regression", "passed": False, "work_item_id": "reproduce"},
        {"id": "edit", "kind": "change", "files": ["app.py"], "work_item_id": "repair"},
        {"id": "green", "kind": "check", "test_id": "regression", "passed": True, "work_item_id": "verify"}]}
    initial = deepcopy(process.context_snapshot)
    cycle = deepcopy(workflow)
    cycle["nodes"][0]["depends_on"] = ["green"]
    with pytest.raises(ValueError, match="workflow dependency cycle"):
        preview_contract(process, proposal(root, engineering_workflow=cycle,
            maintenance=_maintenance(process, steps)))
    assert process.context_snapshot == initial
    publish_contract(process, proposal(root, engineering_workflow=workflow,
        maintenance=_maintenance(process, steps)), store=store)
    nodes = _nodes(process)
    assert nodes["red"]["before_mutation"] is True
    assert nodes["edit"]["depends_on"] == ["red"]
    assert nodes["green"]["depends_on"] == ["edit", "red"]
    assert not EngineeringWorkflow(process).guard("edit_file", "workspace_write")["allowed"]


def test_retained_plan_renews_evidence_after_declared_source_changes(runtime):
    from backend.core.loop.task_maintenance import synchronize
    _, process, store, root = runtime
    source = root / "expected.txt"
    source.write_text("first", encoding="utf-8")
    maintenance = _maintenance(process, _steps())
    maintenance["plan"]["dependencies"] = [{"kind": "file", "id": "expected.txt",
                                             "affected_tools": ["delivery_check"]}]
    publish_contract(process, proposal(root, engineering_workflow=_workflow(), maintenance=maintenance), store=store)
    synchronize(process, {})
    for name in ("source_read", "source_check", "independent", "delivery_check"):
        _receipt(process, name, 1)
    before = _nodes(process)
    source.write_text("second", encoding="utf-8")
    synchronize(process, {})
    state = state_for(process)
    identifier = next(key for key, item in state["maintenance_items"].items() if item["status"] == "pending")
    publish_contract(process, proposal(root, expected_revision=1, maintenance={
        "expected_revision": state["revision"], "resolve_maintenance": [identifier],
        "plan_review": {"action": "retain", "reason": "The same check covers the newly observed source."}}), store=store)
    assert state_for(process)["plan"]["steps"] == state["plan"]["steps"]
    after = _nodes(process)
    assert after["delivery_check"]["status"] == "ready"
    assert after["independent"]["evidence"] == before["independent"]["evidence"]
