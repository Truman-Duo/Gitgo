"""Consumer regressions for bound task plans and persistent workbench evidence."""
from copy import deepcopy
import pytest
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.task_contract import publish_contract
from tests.test_task_contract_activation import proposal, runtime
from tests.test_workbench_materials import work


def linked_workflow():
    return {"profiles": ["retrospective"], "nodes": [
        {"id": "retro", "kind": "report", "format": "retrospective", "work_item_id": "verify"}
    ]}


def activate(runtime):
    _, process, store, root = runtime
    publish_contract(process, proposal(root, engineering_workflow=linked_workflow(), maintenance={
        "expected_revision": 0,
        "plan": {"summary": "Verify the feature", "steps": [
            {"id": "verify", "goal": "Verify the feature", "verification": "Retain evidence"}
        ]}
    }), store=store)
    return process


def test_linked_workflow_can_still_ask_a_new_user_question(runtime):
    process = activate(runtime)
    result = EngineeringWorkflow(process).ask_semantic({
        "kind": "clarification", "state_topic": "output_format",
        "question": "Which output format should this task use?",
        "why_user_must_decide": "The output format is the user's preference.",
        "allow_free_form": True, "options": []
    })
    assert result["decision_id"]


def test_linked_workflow_can_use_existing_configure_entrypoint(runtime):
    process = activate(runtime)
    EngineeringWorkflow(process).configure(linked_workflow())


def test_main_reply_adds_a_fact_without_losing_bound_work_items(runtime):
    from backend.core.loop.main_dialogue import apply_answer_updates
    from backend.core.loop.decision_support import confirmed_user_state
    process = activate(runtime)
    workflow = EngineeringWorkflow(process)
    card = workflow.ask_semantic({"kind": "clarification", "state_topic": "audience",
        "question": "Which audience?", "why_user_must_decide": "A user preference", "options": []})
    answer = "Teams will use it. Only preview; no writes."
    process.session.host_ledger.append({"event": "user_decision_received", "task_id": process.active_task_id,
        "process_id": process.process_id, "decision_id": card["decision_id"], "answer": answer})
    process.pending_decision = None
    old_binding = deepcopy(workflow._load()["plan"]["work_item_bindings"])
    questions = sum(e.get("event") == "user_decision_requested" for e in process.session.host_ledger)
    apply_answer_updates(process, [{"state_topic": "access_scope", "disposition": "confirmed",
        "value": "Only preview; no writes.", "quote": "Only preview; no writes."}])
    assert workflow._load()["plan"]["work_item_bindings"] == old_binding
    current = next(item for item in confirmed_user_state(process.session.host_ledger) if item["topic"] == "access_scope")
    assert current["source_quote"] == "Only preview; no writes."
    assert current["value"] == current["source_quote"]
    assert current["value_origin"] == "user_quote"
    assert sum(e.get("event") == "user_decision_requested" for e in process.session.host_ledger) == questions
    assert not process.pending_decision and not process.approval_grants


def test_bound_plan_public_question_answer_and_checkpoint_recovery(runtime):
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    from backend.core.loop.recovery import restore_incomplete_processes
    from backend.core.loop.manager import AgentProcessManager
    process = activate(runtime)
    _, _, store, root = runtime
    tool = build_internal_tools(process, {}, str(root), None, None, None)["request_user_decision"]
    request = tool.execute({"kind": "clarification", "state_topic": "audience", "question": "Which audience?",
                            "why_user_must_decide": "Audience is a user preference", "options": []})
    workflow = EngineeringWorkflow(process)
    question = next(node for node in workflow.status()["nodes"] if node["kind"] == "decision")
    process.session.host_ledger.append({"event": "user_decision_received", "task_id": process.active_task_id,
        "decision_id": request["decision_id"], "state_topic": "audience", "answer": "teams"})
    process.pending_decision = None
    workflow.resolve_answer(question["id"], request["decision_id"], {"disposition": "confirmed", "value": "teams", "quote": "teams"})
    workflow.record("retro")
    assert workflow.status()["complete"]
    store.save_process_checkpoint(process)
    manager = AgentProcessManager()
    manager.session_store, manager.storage = store, store._storage
    restore_incomplete_processes(store, manager, root, include_process_ids=[process.process_id])
    restored = manager.get(process.process_id)
    assert EngineeringWorkflow(restored).status()["complete"]
    assert EngineeringWorkflow(restored)._load()["plan"] == workflow._load()["plan"]


def test_alignment_question_extension_preserves_bound_plan(runtime):
    from backend.core.loop.alignment_runtime import AlignmentRuntime
    from tests.test_alignment_scope import proposal as assessment, question
    process = activate(runtime)
    process.task_description = "请帮我撰写综合报告。"
    alignment = AlignmentRuntime(process)
    payload = alignment.payload(process.task_description)
    result = alignment.apply(assessment("deliverable", payload["request"], [question(payload["request"])]), payload)
    assert result["status"] == "needs_input"
    assert process.pending_decision
    assert EngineeringWorkflow(process)._load()["plan"]["work_item_bindings"]["node_versions"].get("retro")


def test_legacy_question_import_preserves_bound_plan(runtime):
    from backend.core.loop.decision_support import create_user_decision
    process = activate(runtime)
    request = create_user_decision(process, {"kind": "clarification", "state_topic": "audience",
        "question": "Which audience?", "why_user_must_decide": "User preference", "options": []})
    process.session.host_ledger.append({"event": "user_decision_received", "task_id": process.active_task_id,
        "decision_id": request["decision_id"], "answer": "teams"})
    process.pending_decision = None
    EngineeringWorkflow(process).resolve_answer(request["decision_id"], request["decision_id"],
        {"disposition": "confirmed", "value": "teams", "quote": "teams"})
    assert EngineeringWorkflow(process)._load()["plan"]["work_item_bindings"]["node_versions"].get("retro")


def test_required_workbench_scope_survives_model_contract_projection(runtime):
    from backend.core.loop.model_feedback import contract_view
    _, process, store, root = runtime
    contract = publish_contract(process, proposal(root, required_workbench_claims=["Count"]), store=store)
    assert contract_view(contract).get("required_workbench_claims") == ["Count"]


@pytest.mark.parametrize("kind", ["answer", "supervisor", "action", "review"])
def test_required_workbench_scope_survives_prompt_recompilation(runtime, kind):
    from backend.core.loop.prompt_compiler import PromptCompiler
    _, process, store, root = runtime
    process.task_kind = kind
    publish_contract(process, proposal(root, required_workbench_claims=["Count"]), store=store)
    text, _ = PromptCompiler.compile(process=process, tools={}, workspace_path=str(root))
    assert '"required_workbench_claims":["Count"]' in text


def test_persisted_python_provenance_can_be_checked_by_existing_material_consumer(work):
    from backend.core.loop.context_store import ContextObjectStore
    from backend.core.workbench.sources import SourceWorkbench
    from backend.core.workbench.data import encode_json, text_digest
    root, storage, _, sources, _ = work
    original = storage.put_tool_result('[1,2,3]', process_id="p",
        task_id="task", tool_name="fixture")
    record = {"kind": "gitgo.workbench.python.v1", "snapshot_state": "recorded", "inputs": {
        "rows": {"locator": original["locator"], "selector": "", "sha256": text_digest('[1,2,3]'),
                 "basis": "current", "redacted": False}},
        "outputs": {"total": {"value": 3, "unit": "", "numeric_modes": ["integer"]}}}
    output = storage.put_tool_result(encode_json(record), process_id="p",
        task_id="task", tool_name="workbench_compute")
    sheet = sources.sheets.create("Computed material", [])
    imported = sources.import_result(sheet["sheet_id"], sheet["revision"], cell_id="result", locator=output["locator"])
    assert imported["applicability"]["state"] == "current"
    document, _, _ = sources.sheets._load(imported["sheet_id"], imported["revision"])
    meta = document["cells"][0]["metadata"]["gitgo"]
    # This is the consumer constructed by workbench.materials.assess_bindings.
    consumer = SourceWorkbench(None, ContextObjectStore(root))
    assert consumer._assess_source(meta)["state"] == "current"


def test_external_bindings_still_rejected_without_side_effects(runtime):
    process = activate(runtime)
    before = deepcopy(process.context_snapshot)
    with pytest.raises(ValueError, match="Runtime-owned"):
        EngineeringWorkflow(process).configure(before["engineering_workflow"]["plan"])
    assert process.context_snapshot == before


@pytest.mark.parametrize("input_kind", ["cell", "locator"])
def test_compute_material_reopens_and_rejects_unavailable_or_changed_inputs(work, input_kind):
    from backend.core.workbench.materials import submit_material, captured_bindings, assess_bindings
    from backend.core.workbench.data import encode_json, text_digest
    from backend.core.workbench.python_compute import execute_python
    from backend.core.storage import StorageRuntime, bind_storage, unbind_storage
    root, storage, sheets, sources, sheet = work
    if input_kind == "cell":
        original = sources.contexts.put("fixture/rows", [1, 2, 3])
        imported = sources.import_context(sheet["sheet_id"], sheet["revision"], cell_id="rows", source_ref=original["pinned"])
        ref = {key: imported[key] for key in ("sheet_id", "revision", "cell_id")}
        encoded, evidence = sources.compute_input(ref)
    else:
        encoded = '[1,2,3]'
        original = storage.put_tool_result(encoded, process_id="p", task_id="task", tool_name="fixture")
        evidence = {"locator": original["locator"], "selector": "", "sha256": text_digest(encoded), "redacted": False}
    evidence["basis"] = "current"
    output = execute_python({"inputs_json": {"rows": encoded}, "output_names": ["total"],
        "code": "def run(inputs):\n    return {'total': len(inputs['rows'])}", "_workspace": str(root)})
    record = {"kind": "gitgo.workbench.python.v1", "snapshot_state": "recorded", "inputs": {"rows": evidence},
              "outputs": output["outputs"]}
    locator = storage.put_tool_result(encode_json(record), process_id="p", task_id="task", tool_name="workbench_compute")["locator"]
    target = sheets.create("Computed result", [])
    imported = sources.import_result(target["sheet_id"], target["revision"], cell_id="computed", locator=locator)
    submitted = submit_material(sheets, sources, sheet_id=imported["sheet_id"], revision=imported["revision"],
        cell_ids=["computed"], purpose="Retain the scoped result", project="p", task_id="task", context={})
    bindings = captured_bindings(root, {"signal_type": "workbench_material", "signal_id": submitted["signal_id"]})
    assert assess_bindings(root, bindings)["state"] == "current"
    with StorageRuntime(root, state_home=root / "state") as reopened:
        unbind_storage(root, storage)
        bind_storage(root, reopened)
        try:
            assert assess_bindings(root, bindings)["state"] == "current"
            # Caller-side modifications do not choose a different read scope.
            forged = deepcopy(bindings)
            forged[0]["owner"] = "0" * 64
            forged[0]["source_bindings"] = []
            assert assess_bindings(root, forged)["state"] == "current"
            forged[0]["material_digest"] = "forged"
            assert assess_bindings(root, forged)["state"] == "needs_recheck"
        finally:
            unbind_storage(root, reopened)
            bind_storage(root, storage)
    if input_kind == "cell":
        sources.contexts.put("fixture/rows", [1, 2, 3, 4])
    else:
        # A well-formed but unavailable locator must not pass a detached check.
        record["inputs"]["rows"]["locator"] = "tool-result:sha256:" + "0" * 64
        from backend.core.workbench.sources import SourceWorkbench
        from backend.core.loop.context_store import ContextObjectStore
        assert SourceWorkbench(None, ContextObjectStore(root)).assess_record(record, owner_process_id="p")["state"] == "needs_recheck"
        return
    assert assess_bindings(root, bindings)["state"] == "needs_recheck"
