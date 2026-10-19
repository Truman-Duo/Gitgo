"""Procedure help connects to existing facts; it cannot grant or clear a gate."""
import copy
import hashlib
import json

import pytest

from backend.core.errors import ERROR_CATALOG, NOTICE_CATALOG, catalog_entry, diagnostic_entry, error_payload
from backend.core.loop.decision_support import collect_decision_evidence
from backend.core.loop.recovery_guidance import lookup_guidance
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.event_bus import EventBus
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.engineering_workflow import EngineeringWorkflow
from backend.core.loop.completion_gates import CompletionReasons
from backend.core.loop.completion_protocol import CompletionClaim, HostCompletionEvaluator
from backend.core.loop.loop_guard import LoopGuard
from backend.core.loop.turn_guidance import _build_current_turn_envelope
from backend.core.loop.capabilities import CapabilityLease
from backend.core.loop.executor import _build_internal_tools


def process(root, **kwargs):
    return AgentProcess(process_id="guidance", role="worker", ring_level=RingLevel.RING_3,
        active_task_id="task", worktree_path=str(root), session=AgentSession(), **kwargs)


def test_entire_catalog_resolves_by_symbol_and_stable_id_to_packaged_help():
    definitions = [*ERROR_CATALOG.values(), *NOTICE_CATALOG.values()]
    assert len({d.catalog_id for d in definitions}) == len(definitions)
    assert len({d.name for d in definitions}) == len(definitions)
    for definition in definitions:
        named = lookup_guidance(definition.name)
        assert named == lookup_guidance(definition.catalog_id)
        assert named["found"] and named["instructions"]
        assert named["diagnostic"]["name"] == definition.name
        assert named["authority_granted"] is False and named["is_live_diagnostic"] is False
    with pytest.raises(KeyError):
        catalog_entry("UNREGISTERED")


@pytest.mark.parametrize("identifier", ["../identity/answer", "C:/private", "GITGO-E9999", "error: COMMAND_EXIT_NONZERO"])
def test_unknown_help_cannot_become_a_resource_path_or_guessed_recovery(identifier):
    result = lookup_guidance(identifier)
    assert not result["found"] and not result["state_changed"]
    assert "instructions" not in result and "next_actions" not in result


def test_registered_read_only_api_preserves_pending_decision_and_all_task_state(tmp_path_factory):
    p = process(tmp_path_factory)
    p.pending_decision = {"decision_id": "real-pending", "kind": "permission"}
    tools = _build_internal_tools(p, {}, str(tmp_path_factory))
    tool = tools["decision_evidence"]
    assert tool.effect == ToolEffect.READ and tool.idempotent
    assert "guidance" in tool.parameters["properties"]["focus"]["enum"]
    before = copy.deepcopy((p.context_snapshot, p.session.host_ledger, p.pending_decision, p.tool_receipts, p.capability_lease))
    ref = error_payload("ENGINEERING_PREREQUISITE_REQUIRED")["error_info"]["guidance_ref"]
    help_result = tool.execute(ref["arguments"])
    assert help_result["procedure"] == "workflow"
    assert before == (p.context_snapshot, p.session.host_ledger, p.pending_decision, p.tool_receipts, p.capability_lease)
    assert "workflow" in tool.execute({"focus": "guidance"})["procedures"]
    with pytest.raises(ValueError, match="only valid"):
        collect_decision_evidence(p, "completion", identifier="workflow")


def test_preflight_block_help_real_evidence_then_single_effect(tmp_path_factory):
    p = process(tmp_path_factory)
    p.tool_registry = ToolRegistry(["edit_file"])
    workflow = EngineeringWorkflow(p)
    workflow.configure({"profiles": ["tdd"], "nodes": [
        {"id": "red", "kind": "check", "test_id": "regression", "passed": False},
        {"id": "change", "kind": "change", "files": ["app.py"], "depends_on": ["red"]},
        {"id": "green", "kind": "check", "test_id": "regression", "passed": True, "depends_on": ["change"]}]})
    calls = []
    tool = AgentTool(name="edit_file", description="test", parameters={"type": "object"},
        execute=lambda args: calls.append(args) or {"path": "app.py"}, effect=ToolEffect.WORKSPACE_WRITE)
    ctx = ExecutionContext(process=p, session=p.session, workspace_path=str(tmp_path_factory), event_bus=EventBus())
    pipeline = ToolPipeline()
    blocked = pipeline.execute({"name": tool.name, "args": {}}, tool, ctx, "blocked")
    info = blocked.diagnostics["error_info"]
    assert info["catalog_id"] == "GITGO-E6202"
    assert info["effect_state"] == "not_committed" and info["execution_state"] == "not_started"
    assert info["next_actions"] == [{"action": "engineering_workflow", "operation": "status"}]
    p.session.append_tool_result(blocked.formatted, tool_name=tool.name, is_error=True)
    assert info["catalog_id"] in p.session.messages[-1]["content"]
    assert collect_decision_evidence(p, **info["guidance_ref"]["arguments"])["procedure"] == "workflow"
    assert not workflow.guard("edit_file", "workspace_write")["allowed"] and not calls
    # A real observation record (not the model's help lookup/claim) opens the frontier.
    p.tool_receipts.append({"receipt_id": "red", "task_id": "task", "succeeded": False,
        "committed": False, "tool_name": "run_test", "effect": "process", "test_id": "regression",
        "test_passed": False, "test_completed": True, "test_seeds": [42]})
    allowed = pipeline.execute({"name": tool.name, "args": {}}, tool, ctx, "allowed")
    assert not allowed.is_error and len(calls) == 1


def test_known_business_error_preserves_effect_and_exact_producer_actions(tmp_path_factory):
    p = process(tmp_path_factory); p.tool_registry = ToolRegistry(["probe"])
    payload = error_payload("FILE_CHANGED", state_changed=True, occurrence_id="original",
        next_actions=[{"action": "inspect_this_exact_target"}])
    payload.update(effect_state="partial", execution_state="started", detail="concurrent writer")
    tool = AgentTool(name="probe", description="probe", parameters={"type": "object"}, execute=lambda _: payload,
        effect=ToolEffect.WORKSPACE_WRITE)
    ctx = ExecutionContext(process=p, session=p.session, workspace_path=str(tmp_path_factory), event_bus=EventBus())
    result = ToolPipeline().execute({"name": "probe", "args": {}}, tool, ctx, "probe")
    info = result.diagnostics["error_info"]
    assert info["occurrence_id"] == "original" and info["state_changed"] is True
    assert info["next_actions"] == [{"action": "inspect_this_exact_target"}]
    assert result.receipt["effect_state"] == "partial"
    assert "guidance_ref" in result.formatted and "concurrent writer" in result.formatted


def test_postcommit_warning_is_still_success_and_is_delivered_with_help(tmp_path_factory):
    p = process(tmp_path_factory); p.tool_registry = ToolRegistry(["probe"])
    calls = []
    def commit(_):
        calls.append(1)
        return {"path": "app.py", "knowledge_capture_warning": {
            "code": "SOURCE_EXCERPTS_UNAVAILABLE", "message": "Committed; observer failed."}}
    tool = AgentTool(name="probe", description="probe", parameters={"type": "object"}, execute=commit,
        effect=ToolEffect.WORKSPACE_WRITE)
    ctx = ExecutionContext(process=p, session=p.session, workspace_path=str(tmp_path_factory), event_bus=EventBus())
    result = ToolPipeline().execute({"name": "probe", "args": {}}, tool, ctx, "committed")
    assert not result.is_error and result.receipt["committed"] is True and len(calls) == 1
    notice = result.data["knowledge_capture_warning"]
    assert notice["catalog_id"] == "GITGO-W6302" and "guidance_ref" in result.formatted
    assert any(e.get("catalog_id") == "GITGO-W6302" for e in p.session.host_ledger)
    assert lookup_guidance(notice["code"])["diagnostic"]["kind"] == "warning"


def test_gate_metadata_keeps_old_exception_identity_and_tail_is_append_only(tmp_path_factory):
    reasons = CompletionReasons()
    message = "independent review missing"
    with reasons.scope("verification", degradable=True):
        reasons.append(message, code="INDEPENDENT_REVIEW_REQUIRED")
    gate = reasons.gates[0]
    expected = hashlib.sha256(json.dumps(["verification", message], ensure_ascii=False).encode()).hexdigest()[:20]
    assert gate["gate_id"] == "gate_" + expected
    assert gate["degradable_by_explicit_user_decision"] is True
    assert gate["catalog_id"] == "GITGO-E6106"
    p = process(tmp_path_factory, task_kind="action")
    p.review_required = True
    p.completion_claim = CompletionClaim.from_response("done", step=1)
    p.session.append_user("original user request")
    history = copy.deepcopy(p.session.messages)
    guard = LoopGuard().check(p, "TASK_COMPLETE", p.session)
    assert guard.blocked and any(g["code"] == "INDEPENDENT_REVIEW_REQUIRED" for g in guard.gates)
    assert "GITGO-E6106" in guard.nudge_text and "guidance_ref" in guard.nudge_text
    p.session.append_host_steering(guard.nudge_text, steering_type="completion_tool_rejection", version="1")
    assert p.session.messages[:-1] == history
    assert p.session.messages[-1]["host_authority"] is True
    lookup_guidance("GITGO-E6106")
    assert not HostCompletionEvaluator.evaluate(p, "TASK_COMPLETE").allowed


def test_live_tail_lease_readonly_delegation_and_revocation(tmp_path_factory):
    p = process(tmp_path_factory, actor_kind="supervisor")
    names = ["declare_task_contract", "request_self_execute", "delegate_task", "decision_evidence"]
    schemas = [{"type": "function", "function": {"name": n}} for n in names]
    assert "surface_phase: contract_admission" in _build_current_turn_envelope(p, schemas)
    def contract(mode, next_action):
        p.replace_context_snapshot({"task_contract": {"revision": 1, "execution_mode": mode,
            "routing_compilation": {"next_action": next_action}}})
    contract("self_execute", "request_self_execute")
    assert "surface_phase: lease_admission" in _build_current_turn_envelope(p, schemas)
    p.capability_lease = CapabilityLease.issue(task_id="task", requested_by=p.process_id,
        profile_id="development.workspace", reason="bounded delivery", intended_actions=["write"])
    active = _build_current_turn_envelope(p, schemas)
    assert "surface_phase: active_execution" in active and "next_host_action: continue_current_task" in active
    assert "next_host_action: request_self_execute" not in active
    p.capability_lease = None
    assert "surface_phase: lease_admission" in _build_current_turn_envelope(p, schemas)
    contract("self_execute", "continue_answer")
    readonly = _build_current_turn_envelope(p, schemas)
    assert "surface_phase: active_execution" in readonly and "next_host_action: continue_answer" in readonly
    contract("delegate", "delegate_task")
    delegation = _build_current_turn_envelope(p, schemas)
    assert "surface_phase: active_execution" in delegation and "next_host_action: delegate_task" in delegation


def test_unknown_diagnostic_never_gets_a_fabricated_catalog_identity():
    result = ToolPipeline().error_result("probe", "unknown", 1, "ENGINEERING_PREREQUISITE_REQUIRED in arbitrary prose",
        diagnostics={"code": "UNREGISTERED_ERROR"})
    assert "error_info" not in result.diagnostics
    assert diagnostic_entry("UNREGISTERED_ERROR") is None


def test_provider_hint_preserves_bounded_continuation_and_failure_quarantine():
    from types import SimpleNamespace
    from backend.core.loop.provider_progress import ProviderProgress
    from backend.core.loop.runtime import AgentRuntimeFactory, RuntimeSpec
    from backend.core.loop.models import ProcessStatus
    p = AgentRuntimeFactory.create(RuntimeSpec(role="worker", actor_kind="worker",
        capability_profile_id="text.only", ring_level=RingLevel.RING_3,
        tool_registry=ToolRegistry([]), max_steps=3, task_kind="answer", task_id="bounded-provider"))
    p.session.append_user("Explain the quoted input", message_type="conversation")
    events = []
    runtime = ProviderProgress(p, p.session, LoopGuard(), lambda event, **kwargs: events.append(event))
    response = SimpleNamespace(accumulated_text="", provider_incomplete_reason="max_output_tokens", pending_tool_calls={})
    first = runtime._incomplete(response, 0)
    assert first.retry
    message = p.session.messages[-1]
    assert "GITGO-E5101" in message["content"] and "currently mounted" in message["content"]
    assert message["message_type"] == "host_provider_continuation"
    second = runtime._incomplete(response, 0)
    assert second.result is not None and p.status == ProcessStatus.FAILED
    assert runtime.no_progress_recoveries == 1
    assert message["provider_visible"] is False
    assert not any("GITGO-E5101" in str(m.get("content", "")) for m in p.session.to_provider_messages())
    assert p.session.host_ledger[-1]["event"] == "provider_turn_quarantined"


def test_rejected_supervised_failure_retains_concrete_reasons_in_tail(tmp_path_factory):
    from types import SimpleNamespace
    from backend.core.loop.tool_outcome_runtime import ToolOutcomeRuntime
    p = process(tmp_path_factory, task_kind="supervisor", actor_kind="supervisor")
    p.replace_context_snapshot({"task_contract": {"revision": 1, "execution_mode": "delegate",
        "delegation_required": True, "minimum_delegated_outcomes": 1}})
    evaluation = HostCompletionEvaluator.evaluate_supervisor_failure(p, "")
    assert not evaluation.allowed and evaluation.reasons
    assert len(evaluation.gates) == len(evaluation.reasons)
    assert all(g["code"] == "FAILURE_EVIDENCE_INSUFFICIENT" for g in evaluation.gates)
    runtime = ToolOutcomeRuntime(p, p.session, None, lambda event: None, None, None)
    boundary = runtime._supervised_failure(SimpleNamespace(data={"result": ""}), 0, {})
    assert boundary.retry
    tail = p.session.messages[-1]
    assert tail["steering_type"] == "completion_tool_rejection"
    assert all(reason in tail["content"] for reason in evaluation.reasons)
    assert "GITGO-E6104" in tail["content"]
