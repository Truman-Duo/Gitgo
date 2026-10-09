"""Public Host boundaries: ordering, recovery, authority and durable evidence."""
import copy

import pytest

from backend.core.loop.engineering_workflow import EngineeringWorkflow, compile_plan
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.completion_protocol import HostCompletionEvaluator
from backend.core.loop.task_contract import validate_contract_proposal, publish_contract
from backend.core.loop.event_bus import EventBus, ToolEvent


def process(root):
    return AgentProcess(process_id="engineering", role="worker", ring_level=RingLevel.RING_3,
                        active_task_id="task", worktree_path=str(root), session=AgentSession())


def tdd_plan():
    return {"profiles": ["tdd"], "nodes": [
        {"id": "red", "kind": "check", "test_id": "regression", "passed": False},
        {"id": "change", "kind": "change", "files": ["app.py"], "depends_on": ["red"]},
        {"id": "green", "kind": "check", "test_id": "regression", "passed": True, "depends_on": ["change"]},
    ]}


def receipt(p, id, **fields):
    p.tool_receipts.append({"receipt_id": id, "task_id": p.active_task_id,
                            "succeeded": True, "committed": True, **fields})


def check(p, id, passed, **fields):
    receipt(p, id, tool_name="run_test", effect="process", test_id="regression",
            test_passed=passed, test_seeds=[42], test_completed=True, **fields)


def test_tdd_enforces_red_change_green_and_does_not_trust_model_claims(tmp_path_factory):
    p = process(tmp_path_factory)
    workflow = EngineeringWorkflow(p)
    assert workflow.invoke({"operation": "configure", "plan": tdd_plan()})["ready"] == ["red"]
    assert not workflow.guard("edit_file", "workspace_write")["allowed"]
    forged = workflow.invoke({"operation": "record", "node_id": "red", "content": {"passed": False}})
    assert forged["error"]
    assert any(n["code"] == "WORKFLOW_RECOVERY_REQUIRED" for n in p.session.host_ledger)
    # A green test from before reproduction cannot satisfy the green stage.
    check(p, "premature-green", True)
    check(p, "red-run", False, succeeded=False, committed=False)
    assert workflow.status()["ready"] == ["change"]
    assert workflow.guard("edit_file", "workspace_write")["allowed"]
    receipt(p, "write", tool_name="edit_file", effect="workspace_write", files=["app.py"])
    assert not workflow.status()["complete"]
    check(p, "green-run", True)
    assert workflow.status()["complete"]
    assert workflow.completion_reasons() == []


def test_bad_plan_rejected_atomically_and_active_requirements_cannot_disappear(tmp_path_factory):
    p = process(tmp_path_factory)
    workflow = EngineeringWorkflow(p)
    workflow.invoke({"operation": "configure", "plan": tdd_plan()})
    before = copy.deepcopy(p.context_snapshot)
    bad = tdd_plan()
    bad["nodes"][0]["depends_on"] = ["green"]
    assert workflow.invoke({"operation": "configure", "plan": bad})["error"]
    assert p.context_snapshot == before
    replacement = {"profiles": ["retrospective"], "nodes": [{"id": "retro", "kind": "report", "format": "retrospective"}]}
    assert workflow.invoke({"operation": "configure", "plan": replacement})["error"]
    assert p.context_snapshot == before


def test_document_changes_invalidate_dependent_reports_with_visible_notice(tmp_path_factory):
    p = process(tmp_path_factory)
    workflow = EngineeringWorkflow(p)
    (tmp_path_factory / "GLOSSARY.md").write_text("Task: a unit of requested work", encoding="utf-8")
    workflow.configure({"profiles": ["domain_modeling", "retrospective"], "nodes": [
        {"id": "glossary", "kind": "document", "path": "GLOSSARY.md"},
        {"id": "retro", "kind": "report", "format": "retrospective", "depends_on": ["glossary"]},
    ]})
    workflow.record("glossary")
    assert workflow.record("retro")["complete"]
    (tmp_path_factory / "GLOSSARY.md").write_text("Task: amended meaning", encoding="utf-8")
    assert not workflow.status()["complete"]
    assert workflow.status()["ready"] == ["glossary"]
    workflow.record("glossary")
    assert workflow.status()["ready"] == ["retro"]
    assert any(r.get("code") == "WORKFLOW_EVIDENCE_STALE" for r in p.session.host_ledger)


def test_report_storage_failure_notifies_and_keeps_completion_blocked(tmp_path_factory, monkeypatch):
    p = process(tmp_path_factory)
    workflow = EngineeringWorkflow(p, lambda _event: (_ for _ in ()).throw(OSError("transport down")))
    workflow.configure({"profiles": ["retrospective"], "nodes": [{"id": "retro", "kind": "report", "format": "retrospective"}]})
    from backend.core.loop.context_store import ContextObjectStore
    monkeypatch.setattr(ContextObjectStore, "put", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    result = workflow.invoke({"operation": "record", "node_id": "retro"})
    assert result["error"] == "ENGINEERING_WORKFLOW_RECOVERY_REQUIRED"
    assert not HostCompletionEvaluator.evaluate(p, "done").allowed
    assert any(r.get("code") == "WORKFLOW_RECOVERY_REQUIRED" for r in p.session.host_ledger)


def test_broken_document_pointer_is_recoverable(tmp_path_factory):
    p = process(tmp_path_factory)
    workflow = EngineeringWorkflow(p)
    (tmp_path_factory / "AGENTS.md").write_text("Read [rules](missing.md).", encoding="utf-8")
    workflow.configure({"profiles": ["agent_documentation"], "nodes": [
        {"id": "doc", "kind": "document", "path": "AGENTS.md"},
        {"id": "contract", "kind": "report", "format": "agent_documentation", "depends_on": ["doc"]},
    ]})
    assert "broken local reference" in workflow.invoke({"operation": "record", "node_id": "doc"})["message"]
    assert not workflow.status()["complete"]


def test_contract_admission_installs_workflow_and_rejects_cyclic_plan(tmp_path_factory):
    p = process(tmp_path_factory)
    proposal = validate_contract_proposal({"goal": "fix regression", "execution_mode": "self_execute", "engineering_workflow": tdd_plan()}, str(tmp_path_factory))
    publish_contract(p, proposal)
    assert EngineeringWorkflow(p).status()["active"]
    assert not HostCompletionEvaluator.evaluate(p, "done").allowed
    before = copy.deepcopy(p.context_snapshot)
    replacement = copy.deepcopy(proposal)
    replacement["engineering_workflow"]["nodes"][0]["test_id"] = "easier-check"
    replacement["engineering_workflow"]["digest"] = "changed"
    with pytest.raises(ValueError, match="extended"):
        publish_contract(p, replacement)
    assert p.context_snapshot == before


def test_workflow_recovers_from_existing_checkpoint_without_inheriting_next_task(tmp_path_factory):
    from backend.core.loop.manager import SessionStore
    p = process(tmp_path_factory)
    EngineeringWorkflow(p).configure(tdd_plan())
    check(p, "red", False, succeeded=False, committed=False)
    assert EngineeringWorkflow(p).status()["ready"] == ["change"]
    store = SessionStore(tmp_path_factory, state_home=tmp_path_factory / "state", migrate_legacy=False)
    try:
        store.save_process_checkpoint(p)
        saved = store.load_process_state(p.process_id)
        restored = process(tmp_path_factory)
        restored.context_snapshot = saved["runtime_state"]["context_snapshot"]
        restored.tool_receipts = saved["receipts"]
        assert EngineeringWorkflow(restored).status()["ready"] == ["change"]
        restored.active_task_id = "new-task"
        assert not EngineeringWorkflow(restored).status()["active"]
    finally:
        store.close()


def test_event_subscriber_failure_keeps_other_consumers_and_notifies():
    seen, failures = [], []
    bus = EventBus(on_error=failures.append)
    bus.subscribe("ToolResultReady", lambda _e: (_ for _ in ()).throw(ValueError("failure")))
    bus.subscribe("ToolResultReady", seen.append)
    bus.emit(ToolEvent("ToolResultReady", "execution"))
    assert len(seen) == 1
    assert failures[0]["event_type"] == "ToolResultReady"
    assert bus.delivery_failures == failures


def test_decision_frontier_requires_real_user_answer_and_blocks_downstream(tmp_path_factory):
    p = process(tmp_path_factory)
    workflow = EngineeringWorkflow(p)
    workflow.configure({"profiles": ["alignment"], "nodes": [
        {"id": "scope", "kind": "decision", "state_topic": "scope", "before_mutation": True},
        {"id": "mode", "kind": "decision", "state_topic": "mode", "depends_on": ["scope"]},
    ]})
    assert workflow.invoke({"operation": "ask", "node_id": "mode", "request": {}})["error"]
    request = {"kind": "direction", "question": "Which scope?", "why_user_must_decide": "Product choice", "options": [
        {"label": name, "principle": "scope", "immediate_effect": "select", "downstream_effect": "implement", "risks": "bounded", "reversibility": "amend"} for name in ("Small", "Large")
    ]}
    result = workflow.invoke({"operation": "ask", "node_id": "scope", "request": request})
    assert result["status"] == "awaiting_user"
    assert not workflow.guard("edit_file", "workspace_write")["allowed"]
    p.session.host_ledger.append({"event": "user_decision_received", "task_id": p.active_task_id,
                                  "state_topic": "scope", "decision_id": result["decision_id"], "answer": "Small"})
    p.pending_decision = None
    assert workflow.status()["ready"] == ["mode"]


def test_diagnosis_requires_falsifiable_hypotheses_after_real_failure(tmp_path_factory):
    p = process(tmp_path_factory)
    workflow = EngineeringWorkflow(p)
    plan = {"profiles": ["diagnosis"], "nodes": [
        {"id": "repro", "kind": "check", "test_id": "regression", "passed": False},
        {"id": "hypotheses", "kind": "report", "format": "hypotheses", "depends_on": ["repro"]},
        {"id": "fixed", "kind": "check", "test_id": "regression", "passed": True, "depends_on": ["hypotheses"]},
        {"id": "cleanup", "kind": "report", "format": "cleanup", "depends_on": ["fixed"]},
    ]}
    workflow.configure(plan)
    check(p, "reproduced", False, succeeded=False, committed=False)
    assert not workflow.guard("exec_command", "process")["allowed"]
    assert workflow.invoke({"operation": "record", "node_id": "hypotheses", "content": {"symptom": "crash", "hypotheses": [{"prediction": "fails", "probe": "test"}]}})["error"]
    workflow.record("hypotheses", {"symptom": "crash", "hypotheses": [
        {"prediction": "shrinking width fails", "probe": "vary width"},
        {"prediction": "input close fails", "probe": "close input"},
    ]})
    assert workflow.guard("exec_command", "process")["allowed"]
    check(p, "fixed", True)
    assert not workflow.status()["complete"]
    assert workflow.record("cleanup", {"removed_instrumentation": [], "remaining_limitations": []})["complete"]


def test_presets_compile_all_practices_without_a_skill_loader(tmp_path_factory):
    from backend.core.loop.engineering_workflow import PROFILES
    for profile in PROFILES:
        plan = compile_plan({"profiles": [profile], "test_id": "regression", "target_files": ["app.py"],
                             "agent_document_path": "AGENTS.md"}, tmp_path_factory)
        assert plan["nodes"]
        assert plan["profiles"] == [profile]
    combined = compile_plan({"profiles": list(PROFILES), "test_id": "regression", "target_files": ["app.py"],
                             "agent_document_path": "AGENTS.md"}, tmp_path_factory)
    assert len({n["id"] for n in combined["nodes"]}) == len(combined["nodes"])


def test_preparation_paths_and_exact_checks_preserve_product_gates(tmp_path_factory):
    p = process(tmp_path_factory)
    workflow = EngineeringWorkflow(p)
    plan = {"profiles": ["tdd"], "test_id": "regression", "target_files": ["app.py"],
            "preparation_files": ["tests/regression.py"], "check_argv": ["python", "tests/regression.py"]}
    workflow.configure(plan)
    assert workflow.guard("write_file", "workspace_write", {"path": "tests/regression.py"})["allowed"]
    assert not workflow.guard("write_file", "workspace_write", {"path": "app.py"})["allowed"]
    assert workflow.guard("exec_command", "process", {"argv": ["python", "tests/regression.py"]})["allowed"]
    assert not workflow.guard("exec_command", "process", {"argv": ["python", "different.py"]})["allowed"]
    plan["preparation_files"] = ["app.py"]
    with pytest.raises(ValueError, match="overlap"):
        compile_plan(plan, tmp_path_factory)


def test_real_pipeline_keeps_failed_check_evidence_and_gates_product_changes(tmp_path_factory):
    from backend.core.loop.agent_tool import AgentTool, ToolEffect
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.tools import ToolRegistry
    p = process(tmp_path_factory)
    p.tool_registry = ToolRegistry(["run_test", "edit_file"])
    workflow = EngineeringWorkflow(p)
    workflow.configure(tdd_plan())
    ctx = ExecutionContext(process=p, session=p.session, workspace_path=str(tmp_path_factory), event_bus=EventBus())
    pipeline = ToolPipeline()
    writes = []
    edit = AgentTool(name="edit_file", description="edit", parameters={"type": "object"},
                     execute=lambda args: writes.append(args) or {"path": "app.py"}, effect=ToolEffect.WORKSPACE_WRITE)
    blocked = pipeline.execute({"name": "edit_file", "args": {}}, edit, ctx, "blocked")
    assert blocked.is_error and not writes
    run = AgentTool(name="run_test", description="test", parameters={"type": "object"}, effect=ToolEffect.PROCESS,
                    execute=lambda _args: {"test_id": "regression", "target": "tests/test_app.py",
                                           "passed": False, "seed_results": [{"seed": 42, "exit_code": 1}]})
    red = pipeline.execute({"name": "run_test", "args": {}}, run, ctx, "red")
    assert red.is_error
    assert red.receipt["task_id"] == p.active_task_id
    assert red.receipt["test_completed"] and red.receipt["test_passed"] is False
    p.tool_receipts.append(red.receipt)
    changed = pipeline.execute({"name": "edit_file", "args": {}}, edit, ctx, "change")
    assert not changed.is_error and writes
    p.tool_receipts.append(changed.receipt)
    run.execute = lambda _args: {"test_id": "regression", "target": "tests/test_app.py", "passed": True,
                                 "seed_results": [{"seed": 42, "exit_code": 0}]}
    green = pipeline.execute({"name": "run_test", "args": {}}, run, ctx, "green")
    p.tool_receipts.append(green.receipt)
    assert workflow.status()["complete"]


def test_missing_cas_report_and_timed_out_check_remain_missing(tmp_path_factory):
    from backend.core.loop.context_store import ContextObjectStore
    p = process(tmp_path_factory)
    workflow = EngineeringWorkflow(p)
    workflow.configure({"profiles": ["retrospective"]})
    workflow.record("retro")
    ref = workflow.status()["nodes"][0]["evidence"]["ref"]
    item = ContextObjectStore(tmp_path_factory).resolve(ref)
    (tmp_path_factory / ".gitgo/context_objects/blobs" / f"{item.digest}.json").unlink()
    assert not workflow.status()["complete"]
    other = process(tmp_path_factory)
    EngineeringWorkflow(other).configure(tdd_plan())
    receipt(other, "timeout", tool_name="run_test", effect="process", test_id="regression", test_passed=False,
            test_seeds=[42], test_completed=False)
    assert EngineeringWorkflow(other).status()["ready"] == ["red"]


@pytest.mark.parametrize("action", ["accept_engineering_amendment", "keep_engineering_workflow", ""])
def test_scope_amendment_requires_matching_explicit_user_choice(tmp_path_factory, action):
    p = process(tmp_path_factory)
    workflow = EngineeringWorkflow(p)
    workflow.configure(tdd_plan())
    old = workflow.status()["plan_digest"]
    replacement = {"profiles": ["retrospective"]}
    result = workflow.invoke({"operation": "propose_amendment", "plan": replacement,
                              "reason": "The registered test is unavailable; deliver a documented investigation instead."})
    assert result["status"] == "awaiting_user"
    assert '"test_id": "regression"' in result["question"]
    assert workflow.status()["plan_digest"] == old
    assert not workflow.guard("edit_file", "workspace_write")["allowed"]
    p.session.host_ledger.append({"event": "user_decision_received", "task_id": p.active_task_id,
                                  "decision_id": "unrelated", "selected_action": "accept_engineering_amendment"})
    assert workflow.status()["plan_digest"] == old
    p.session.host_ledger.append({"event": "user_decision_received", "task_id": p.active_task_id,
                                  "decision_id": result["decision_id"], "selected_action": action, "answer": "choice"})
    p.pending_decision = None
    status = workflow.status()
    if action == "accept_engineering_amendment":
        assert status["profiles"] == ["retrospective"]
        assert p.context_snapshot["task_contract"]["engineering_workflow"]["digest"] == status["plan_digest"]
        assert workflow.record("retro")["complete"]
    else:
        assert status["plan_digest"] == old
        assert not status["complete"]
        if not action:
            assert status["amendment"]
            assert any(n.get("code") == "WORKFLOW_SCOPE_UNCONFIRMED" for n in p.session.host_ledger)


def test_multi_file_changes_and_later_edits_require_fresh_green(tmp_path_factory):
    p = process(tmp_path_factory)
    workflow = EngineeringWorkflow(p)
    plan = tdd_plan()
    plan["nodes"][1]["files"] = ["app.py", "helper.py"]
    workflow.configure(plan)
    check(p, "red", False)
    receipt(p, "app", effect="workspace_write", files=[str(tmp_path_factory / "app.py")])
    assert workflow.status()["ready"] == ["change"]
    receipt(p, "helper", effect="workspace_write", files=["helper.py"])
    check(p, "green", True)
    assert workflow.status()["complete"]
    receipt(p, "app-again", effect="workspace_write", files=["app.py"])
    assert not workflow.status()["complete"]
    assert workflow.status()["ready"] == ["green"]
    check(p, "fresh-green", True)
    assert workflow.status()["complete"]


def test_pending_user_question_is_visible_recovery_not_silent_exception(tmp_path_factory):
    p = process(tmp_path_factory)
    workflow = EngineeringWorkflow(p)
    workflow.configure(tdd_plan())
    p.pending_decision = {"decision_id": "already-pending"}
    result = workflow.invoke({"operation": "propose_amendment", "plan": {"profiles": ["retrospective"]}, "reason": "Unavailable test"})
    assert result["error"] == "ENGINEERING_WORKFLOW_RECOVERY_REQUIRED"
    assert p.pending_decision["decision_id"] == "already-pending"
    assert not workflow.status()["complete"]


def test_exact_command_check_still_requires_normal_permission(tmp_path_factory):
    from backend.core.loop.agent_tool import AgentTool, ApprovalMode, ToolEffect
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.tools import ToolRegistry
    p = process(tmp_path_factory)
    p.tool_registry = ToolRegistry(["exec_command"])
    workflow = EngineeringWorkflow(p)
    workflow.configure({"profiles": ["tdd"], "test_id": "regression", "target_files": ["app.py"],
                        "check_argv": ["python", "test_app.py"]})
    calls = []
    command = AgentTool(name="exec_command", description="check", parameters={"type": "object"},
                        execute=lambda args: calls.append(args) or {"exit_code": 1},
                        effect=ToolEffect.PROCESS, approval=ApprovalMode.ASK)
    ctx = ExecutionContext(process=p, session=p.session, workspace_path=str(tmp_path_factory), event_bus=EventBus())
    pipeline = ToolPipeline()
    call = {"name": "exec_command", "args": {"argv": ["python", "test_app.py"]}}
    denied = pipeline.execute(call, command, ctx, "no-permission")
    assert denied.is_error and not calls
    ctx.artifacts["approvals"] = {"exec_command"}
    red = pipeline.execute(call, command, ctx, "red")
    assert red.receipt["command_completed"]
    assert red.receipt["command_exit_code"] == 1
    p.tool_receipts.append(red.receipt)
    assert workflow.status()["ready"] == ["change"]


def test_actual_agent_loop_exposes_workflow_and_streams_recovery_notices(tmp_path_factory):
    import json
    from types import SimpleNamespace
    from backend.core.loop.executor import agent_step
    from backend.core.loop.outcome import TaskOutcome, OutcomeStatus
    from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType
    from backend.core.loop.tools import ToolRegistry
    p = process(tmp_path_factory)
    p.tool_registry = ToolRegistry(["engineering_workflow"])
    events = []

    class Provider:
        protocol = SimpleNamespace(value="openai_responses")
        capabilities = SimpleNamespace(prompt_cache="automatic")
        context_window = 8192
        calls = 0

        def stream_events(self, *_args, **kwargs):
            self.calls += 1
            assert any(t.get("name") == "engineering_workflow" or t.get("function", {}).get("name") == "engineering_workflow" for t in kwargs["tools"])
            operations = [{"operation": "configure", "plan": {"profiles": ["retrospective"]}},
                          {"operation": "record", "node_id": "unknown"},
                          {"operation": "record", "node_id": "retro"}]
            if self.calls <= len(operations):
                id = f"workflow-{self.calls}"
                yield ProviderEvent(ProviderEventType.TOOL_CALL_STARTED, tool_call_id=id,
                                    tool_name="engineering_workflow", output_index=0)
                yield ProviderEvent(ProviderEventType.TOOL_CALL_DONE, tool_call_id=id,
                                    tool_name="engineering_workflow", output_index=0,
                                    arguments=json.dumps(operations[self.calls - 1]))
            else:
                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="Retrospective recorded.\nTASK_COMPLETE")
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)

    result = TaskOutcome.from_dict(agent_step(p, Provider(), instruction="Record a retrospective",
                                              dispatcher=SimpleNamespace(_executors={}),
                                              workspace_path=str(tmp_path_factory), on_stream_event=events.append))
    assert result.status == OutcomeStatus.COMPLETED, result.to_dict()
    assert EngineeringWorkflow(p).status()["complete"]
    assert any(e.get("code") == "WORKFLOW_RECOVERY_REQUIRED" for e in events)
    assert any(e.get("code") == "WORKFLOW_EVIDENCE_RECORDED" for e in events)


def test_composite_cannot_hide_an_unready_leaf_write(tmp_path_factory):
    from backend.core.loop.agent_tool import AgentTool, ToolEffect
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.tools import ToolRegistry
    p = process(tmp_path_factory)
    p.tool_registry = ToolRegistry(["bundle", "edit_file"])
    EngineeringWorkflow(p).configure(tdd_plan())
    writes = []
    leaf = AgentTool(name="edit_file", description="edit", parameters={"type": "object"},
                     effect=ToolEffect.WORKSPACE_WRITE, execute=lambda args: writes.append(args) or {"path": "app.py"})
    bundle = AgentTool(name="bundle", description="bundle", parameters={"type": "object"},
                       effect=ToolEffect.WORKSPACE_WRITE, execute=lambda _args: {},
                       composite_spec={"steps": [{"id": "edit", "tool": "edit_file", "arguments": {"path": "app.py"}}]})
    ctx = ExecutionContext(process=p, session=p.session, workspace_path=str(tmp_path_factory), event_bus=EventBus())
    ctx.artifacts["tool_catalog"] = {"bundle": bundle, "edit_file": leaf}
    result = ToolPipeline().execute({"name": "bundle", "args": {}}, bundle, ctx, "bundle")
    assert result.is_error and not writes
    assert result.receipt["child_receipts"][0]["succeeded"] is False
    assert any(e.get("code") == "WORKFLOW_PREREQUISITE_BLOCKED" for e in p.session.host_ledger)


def test_document_link_titles_uri_and_examples_are_not_false_failures(tmp_path_factory):
    p = process(tmp_path_factory)
    (tmp_path_factory / "rules space.md").write_text("Rules", encoding="utf-8")
    (tmp_path_factory / "AGENTS.md").write_text('''Read [rules](rules%20space.md "Rules").
[mail](mailto:owner@example.com)
[site](https://example.com)
```md
[example](not-a-real-file.md)
```
''', encoding="utf-8")
    workflow = EngineeringWorkflow(p)
    workflow.configure({"profiles": ["agent_documentation"], "agent_document_path": "AGENTS.md"})
    assert not workflow.invoke({"operation": "record", "node_id": "agent_document"}).get("error")


def test_red_green_cannot_substitute_a_different_command_under_same_test_label(tmp_path_factory):
    plan = tdd_plan()
    for index in (0, 2):
        plan["nodes"][index].update(tool_name="exec_command", argv=["python", "regression.py"])
    assert compile_plan(plan, tmp_path_factory)
    plan["nodes"][2]["argv"] = ["python", "unrelated_passing_test.py"]
    with pytest.raises(ValueError, match="same test"):
        compile_plan(plan, tmp_path_factory)
