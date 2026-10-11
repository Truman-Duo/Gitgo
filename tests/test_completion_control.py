"""Real Host completion paths; provider calls are forbidden after control choices."""
from backend.core.loop.models import AgentProcess, ProcessStatus, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.completion_protocol import CompletionClaim, HostCompletionEvaluator
from backend.core.loop.completion_control import blocked_delivery, execute_control
from backend.core.loop.completion_gates import accepts_partial
from backend.core.loop.decision_support import create_completion_exception_decision
from backend.core.loop.loop_guard import LoopGuard
from backend.core.loop.executor import agent_step
from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType, ProviderProtocol, ProviderCapabilities
from backend.core.loop.signals import GovernanceSignal, SignalCategory, SignalSeverity
from backend.core.loop.tools import ToolRegistry
import pytest


@pytest.mark.parametrize("task_kind", ["answer", "plan", "supervisor", "review"])
def test_explicit_required_tests_cannot_disappear_on_task_reclassification(tmp_path_factory, task_kind):
    p = process(tmp_path_factory)
    p.task_kind = task_kind
    evaluation = HostCompletionEvaluator.evaluate(p, "Existing answer; environment unavailable")
    assert not evaluation.allowed
    assert any(g["kind"] == "test_evidence" and g["degradable_by_explicit_user_decision"] for g in evaluation.gates)


def process(root):
    p = AgentProcess(process_id="completion-worker", role="worker", ring_level=RingLevel.RING_3,
        actor_kind="worker", task_kind="action", active_task_id="completion-task",
        session=AgentSession(), tool_registry=ToolRegistry([]), worktree_path=str(root),
        required_test_ids=["missing-environment-test"], max_steps=8)
    p.tool_receipts = [{"receipt_id": "write-proof", "tool_name": "write_file",
        "task_id": p.active_task_id, "effect": "workspace_write", "succeeded": True, "committed": True}]
    p.completion_claim = CompletionClaim.from_args({"result": "Existing change; test unavailable."}, step=1)
    return p


def answer(p, action):
    gates = HostCompletionEvaluator.outstanding_gates(p)
    request = create_completion_exception_decision(p, gates)
    p.session.host_ledger.append({"event": "user_decision_received", "task_id": p.active_task_id,
        "process_id": p.process_id, "decision_id": request["decision_id"], "selected_action": action,
        "kind": "completion_exception"})
    if action == "accept_partial":
        p.runtime_preferences["completion_exception"] = {"decision_id": request["decision_id"],
            "gate_ids": request["completion_gate_ids"], "contract_digest": request["contract_digest"]}
    p.pending_decision = None
    p.runtime_preferences["completion_control_action"] = {"request": request, "action": action}
    return request, gates


class NoProviderCalls:
    def bind_session(self, *_):
        raise AssertionError("Host control must finish before even binding a provider")


def test_partial_delivery_needs_actual_answer_and_current_contract(tmp_path_factory):
    p = process(tmp_path_factory)
    request, gates = answer(p, "accept_partial")
    assert accepts_partial(p, gates["gates"])
    received = p.session.host_ledger.pop()
    assert not accepts_partial(p, gates["gates"])
    p.session.host_ledger.append(received)
    p.required_test_ids.append("new-requirement")
    assert not accepts_partial(p, gates["gates"])


def test_host_accepts_partial_without_provider_and_preserves_failed_facts(tmp_path_factory):
    p = process(tmp_path_factory)
    answer(p, "accept_partial")
    outcome = agent_step(p, NoProviderCalls())
    assert outcome["status"] == "degraded"
    assert "Existing change" in outcome["response"]
    assert not HostCompletionEvaluator.evaluate(p, p.completion_claim.result).allowed


def test_host_stop_is_terminal_without_provider(tmp_path_factory):
    p = process(tmp_path_factory)
    answer(p, "stop")
    outcome = agent_step(p, NoProviderCalls())
    assert outcome["status"] == "cancelled"
    assert p.status == ProcessStatus.CANCELLED
    assert p.cancellation_event.is_set()
    assert p.required_test_ids == ["missing-environment-test"]


def test_scope_change_ends_old_lease_without_claiming_to_change_requirements(tmp_path_factory):
    p = process(tmp_path_factory)
    p.tool_receipts.clear()  # Missing actual effect evidence is not degradable.
    answer(p, "amend_scope")
    outcome = agent_step(p, NoProviderCalls())
    assert outcome["status"] == "cancelled"
    assert "新任务范围" in outcome["response"]
    assert p.required_test_ids == ["missing-environment-test"]


def test_control_runtime_flag_without_answer_is_not_authority(tmp_path_factory):
    p = process(tmp_path_factory)
    answer(p, "stop")
    p.session.host_ledger.pop()
    outcome = agent_step(p, NoProviderCalls())
    assert outcome["status"] == "failed"
    assert not p.cancellation_event.is_set()


def test_critical_rule_cannot_be_downgraded_by_words_test_review(tmp_path_factory):
    p = process(tmp_path_factory)
    signal = GovernanceSignal(source="contract", severity=SignalSeverity.CRITICAL,
        category=SignalCategory.BLOCK, rule="test review approval unavailable")
    gates = HostCompletionEvaluator.outstanding_gates(p, [signal])
    hard = [g for g in gates["gates"] if "active critical" in g["message"]]
    assert hard and not hard[0]["degradable_by_explicit_user_decision"]
    request = create_completion_exception_decision(p, gates)
    assert all(option["action"] != "accept_partial" for option in request["options"])


def test_unavailable_lesson_tool_reaches_the_same_control_exit(tmp_path_factory):
    p = process(tmp_path_factory)
    signal = GovernanceSignal(source="lesson_trigger", severity=SignalSeverity.HIGH,
        required_tools=["unavailable_tool"])
    gates = HostCompletionEvaluator.outstanding_gates(p, [signal])
    tool = [g for g in gates["gates"] if g["kind"] == "required_tool_evidence"]
    assert tool and tool[0]["degradable_by_explicit_user_decision"]


class TextClaims:
    protocol = ProviderProtocol.OPENAI_RESPONSES
    capabilities = ProviderCapabilities(protocol=ProviderProtocol.OPENAI_RESPONSES.value,
        tools=True, prompt_cache="automatic", context_window=8192)
    context_window = 8192

    def __init__(self):
        self.calls = 0

    def stream_events(self, *_args, **_kwargs):
        self.calls += 1
        assert self.calls <= 2, "same blocker must pause before a third model call"
        yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="TASK_COMPLETE\nExisting change; test unavailable.")
        yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)


def test_plain_text_claim_pauses_at_same_gate_instead_of_failing_or_looping(tmp_path_factory):
    p = process(tmp_path_factory)
    provider = TextClaims()
    outcome = agent_step(p, provider, workspace_path=str(tmp_path_factory))
    assert outcome["status"] == "awaiting_user", str(outcome)
    assert p.pending_decision["kind"] == "completion_exception"
    assert provider.calls == 2
