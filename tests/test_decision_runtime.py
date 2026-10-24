"""Regression coverage for Host shortcuts, pause/resume, and cache facts."""

from __future__ import annotations

from backend.core.loop.execution_contract import NATIVE_PROCESS, data_broker

import json
import threading
import time
from types import SimpleNamespace

import pytest

from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.decision_support import (
    create_completion_exception_decision, create_user_decision, safe_calculate,
)
from backend.core.loop.decision_timeline import decision_timeline
from backend.core.loop.executor import _build_internal_tools, agent_step
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.completion_protocol import CompletionClaim, HostCompletionEvaluator
from backend.core.loop.loop_guard import LoopGuard
from backend.core.loop.outcome import OutcomeStatus, TaskOutcome
from backend.core.loop.prompt_compiler import PromptCompiler
from backend.core.loop.provider_protocol import (
    CacheIntent,
    ProviderCapabilities,
    ProviderEvent,
    ProviderEventType,
    ProviderProtocol,
)
from backend.core.loop.session import AgentSession
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.question_broker import pending_questions
from backend.core.daemon.dispatch import _resume_context_snapshot


def _supervisor(tmp_path, *, task_id="root", max_steps=8):
    manager = AgentProcessManager(max_concurrency=4)
    process = manager.fork(
        parent_id=None,
        role="supervisor",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("supervisor.control")),
        max_steps=max_steps,
        ring_level=RingLevel.RING_0,
        workspace_path=str(tmp_path),
        task_id=task_id,
        actor_kind="supervisor",
        capability_profile_id="supervisor.control",
        task_kind="supervisor",
        task_budget_limits={"max_seconds": 10},
    )
    return manager, process


def test_permission_resume_preserves_compiled_task_contract(tmp_path_factory):
    _manager, process = _supervisor(tmp_path_factory)
    contract = {
        "revision": 1,
        "execution_mode": "self_execute",
        "capability_profile_id": "development.workspace",
    }
    process.replace_context_snapshot({
        "task_contract": contract,
        "signals": [],
    })

    resumed = _resume_context_snapshot(
        process, None, canonical_user_turn=False,
    )
    assert resumed["task_contract"] == contract

    # A real public turn still starts a fresh admission and therefore must not
    # inherit the previous task's execution authority through this helper.
    assert _resume_context_snapshot(
        process, None, canonical_user_turn=True,
    ) is None


def test_calculator_is_deterministic_and_rejects_code_execution():
    assert safe_calculate("(12 + 3) * 4 == 60") is True
    assert safe_calculate("2 ** 10") == 1024
    with pytest.raises(ValueError, match="unsupported expression"):
        safe_calculate("__import__('os').system('echo unsafe')")
    with pytest.raises(ValueError, match="exponent"):
        safe_calculate("2 ** 101")


def test_decision_checkpoint_failure_keeps_the_exact_card_and_cannot_resume(tmp_path_factory):
    import queue
    from backend.core.daemon.dispatch import _cmd_task
    manager, process = _supervisor(tmp_path_factory)
    pending = create_user_decision(process, _decision_args("Choose the scope"))
    pending = dict(process.pending_decision)
    process.status = ProcessStatus.AWAITING_USER
    ledger = list(process.session.host_ledger)
    class Store:
        def save_process_checkpoint(self, p):
            assert p.pending_decision is None
            assert p.session.host_ledger[-1]["event"] == "user_decision_received"
            raise OSError("storage full")
    emitted = []
    _cmd_task({"action": "decision", "process_id": process.process_id,
        "decision_id": pending["decision_id"], "instruction": "Choose option 1: A"},
        SimpleNamespace(workspace_path=tmp_path_factory), SimpleNamespace(name="project"),
        {"apm": manager, "session_store": Store(), "evq": queue.Queue()}, emitted.append)
    assert "DECISION_CHECKPOINT_FAILED" in emitted[-1]["error"]
    assert process.status is ProcessStatus.AWAITING_USER
    assert process.pending_decision == pending
    assert process.session.host_ledger == ledger
    assert not manager._threads


def test_repeated_degradable_gates_can_finish_only_as_explicit_degraded_outcome(
    tmp_path_factory,
):
    manager = AgentProcessManager()
    process = manager.fork(
        parent_id=None, role="worker", tool_registry=ToolRegistry([]),
        max_steps=4, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id="degraded-gate",
        actor_kind="worker", task_kind="action",
        required_test_ids=["required-test"],
    )
    process.steps_used = 1
    process.completion_claim = CompletionClaim.from_args(
        {"result": "partial result"}, step=1,
    )
    process.tool_receipts.append({
        "receipt_id": "write-1", "task_id": "degraded-gate",
        "effect": "workspace_write", "succeeded": True, "committed": True,
    })
    gates = HostCompletionEvaluator.outstanding_gates(process)
    assert gates["ready"] is False
    assert gates["gates"]
    assert all(
        item["degradable_by_explicit_user_decision"] is True
        for item in gates["gates"]
    )
    decision = create_completion_exception_decision(process, gates)
    assert decision["options"][1]["action"] == "accept_partial"

    process.session.host_ledger.append({"event": "user_decision_received",
        "decision_id": decision["decision_id"], "task_id": process.active_task_id,
        "process_id": process.process_id, "selected_action": "accept_partial"})
    process.runtime_preferences["completion_exception"] = {
        "contract_digest": decision["contract_digest"],
        "decision_id": decision["decision_id"],
        "gate_ids": decision["completion_gate_ids"],
    }
    result = LoopGuard().check(
        process, "TASK_COMPLETE\npartial result", process.session,
    )
    assert result.is_complete is True
    assert result.degraded is True


def _decision_args(question: str) -> dict:
    return {
        "question": question,
        "why_user_must_decide": "This changes the product contract.",
        "options": [
            {"label": "A", "principle": "A", "immediate_effect": "A now",
             "downstream_effect": "A later", "risks": "A risk", "reversibility": "A reversible"},
            {"label": "B", "principle": "B", "immediate_effect": "B now",
             "downstream_effect": "B later", "risks": "B risk", "reversibility": "B reversible"},
        ],
    }


def test_decision_order_is_monotonic_across_context_epochs(tmp_path_factory):
    manager, process = _supervisor(tmp_path_factory)
    first = create_user_decision(process, _decision_args("First?"))
    process.session.host_ledger.append({
        "event": "user_decision_received", "decision_id": first["decision_id"], "answer": "A",
    })
    process.pending_decision = None
    process.session.context_epoch += 1
    process.session.messages[:] = []
    second = create_user_decision(process, _decision_args("Second?"))
    cards = decision_timeline(process.session.host_ledger)

    assert [item["decision_sequence"] for item in cards] == [1, 2]
    assert [item["decision"]["context_epoch"] for item in cards] == [0, 1]
    assert cards[0]["status"] == "answered"
    assert cards[1]["decision"]["decision_id"] == second["decision_id"]
    assert manager.get(process.process_id) is process


def test_worker_question_routes_to_root_without_model_forwarding(tmp_path_factory):
    manager, root = _supervisor(tmp_path_factory)
    worker = manager.fork(
        parent_id=root.process_id, role="executor",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("development.workspace")),
        max_steps=4, ring_level=RingLevel.RING_3, workspace_path=str(tmp_path_factory),
        task_id="child-question", actor_kind="worker",
        capability_profile_id="development.workspace",
    )
    request = create_user_decision(worker, _decision_args("Which visual direction?"))
    assert request["source_process_id"] == worker.process_id
    assert request["owner_process_id"] == root.process_id
    assert request["source_display_name"] == "B1"
    assert [item["decision_id"] for item in pending_questions(manager.list_processes())] == [
        request["decision_id"]
    ]


def test_interrupting_a_parked_decision_closes_the_card_and_process(tmp_path_factory):
    manager, process = _supervisor(tmp_path_factory)
    request = create_user_decision(process, _decision_args("Continue?"))
    process.status = ProcessStatus.AWAITING_USER
    process.lifecycle_finalized = True
    result = manager.kill(process.process_id, reason="user_interrupted")
    cards = decision_timeline(process.session.host_ledger)

    assert result["cancelled_parked"] == [process.process_id]
    assert process.status is ProcessStatus.CANCELLED
    assert process.pending_decision is None
    assert cards[0]["decision"]["decision_id"] == request["decision_id"]
    assert cards[0]["status"] == "cancelled"
    assert cards[0]["decision_cancellation"] == "user_interrupted"


def test_child_task_ids_are_atomic_under_parallel_delegation(tmp_path_factory):
    tmp_path = tmp_path_factory
    _manager, process = _supervisor(tmp_path)
    values: list[str] = []
    lock = threading.Lock()

    def allocate():
        value = process.allocate_child_task_id()
        with lock:
            values.append(value)

    threads = [threading.Thread(target=allocate) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(values) == len(set(values)) == 50
    assert {int(value.rsplit(":", 1)[1]) for value in values} == set(range(1, 51))


def test_wait_many_wakes_once_for_any_child(tmp_path_factory):
    tmp_path = tmp_path_factory
    manager, root = _supervisor(tmp_path)
    children = [
        manager.fork(
            parent_id=root.process_id,
            role="executor",
            tool_registry=ToolRegistry([]),
            max_steps=2,
            ring_level=RingLevel.RING_3,
            workspace_path=str(tmp_path),
            task_id=f"child-{index}",
        )
        for index in range(2)
    ]

    def complete_first():
        time.sleep(0.03)
        children[0].status = ProcessStatus.COMPLETED
        children[0].result = {"status": "completed"}

    manager.start(children[0].process_id, complete_first)
    result = manager.wait_many(
        [item.process_id for item in children], timeout=1, return_when="any_terminal",
    )
    assert result["state_changed"] is True
    assert result["all_terminal"] is False
    assert result["processes"][children[0].process_id]["status"] == "completed"


class _DecisionProvider:
    protocol = ProviderProtocol.OPENAI_RESPONSES
    capabilities = ProviderCapabilities(
        protocol=ProviderProtocol.OPENAI_RESPONSES.value,
        tools=True,
        prompt_cache="automatic",
        context_window=8192,
    )
    context_window = 8192

    def __init__(self):
        self.calls = 0

    def stream_events(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            request = {
                "question": "Which compatibility policy should govern the public API?",
                "why_user_must_decide": "Both are technically valid product commitments.",
                "options": [
                    {
                        "label": "Strict compatibility",
                        "principle": "Protect existing callers",
                        "immediate_effect": "Keep the legacy shape",
                        "downstream_effect": "Slower cleanup but fewer migrations",
                        "risks": "Carries legacy complexity",
                        "reversibility": "Can deprecate in a later major version",
                        "recommended": True,
                    },
                    {
                        "label": "Clean break",
                        "principle": "Optimize the long-term API",
                        "immediate_effect": "Remove the legacy shape",
                        "downstream_effect": "Simpler implementation with caller migration",
                        "risks": "Breaks existing callers",
                        "reversibility": "Requires a compatibility adapter to undo",
                    },
                ],
            }
            yield ProviderEvent(
                ProviderEventType.TOOL_CALL_DONE,
                output_index=0,
                tool_call_id="decision-1",
                tool_name="request_user_decision",
                arguments=json.dumps(request),
            )
        else:
            yield ProviderEvent(
                ProviderEventType.TEXT_DELTA,
                text="Proceeding with strict compatibility.\nTASK_COMPLETE",
            )
        yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)


def test_structured_user_decision_pauses_and_resumes_same_process(tmp_path_factory):
    tmp_path = tmp_path_factory
    _manager, process = _supervisor(tmp_path)
    process.tool_registry = ToolRegistry([
        "request_user_decision", "calculate", "decision_evidence",
    ])
    provider = _DecisionProvider()
    events = []
    first = TaskOutcome.from_dict(agent_step(
        process,
        provider,
        instruction="Resolve the public API policy and proceed.",
        workspace_path=str(tmp_path),
        on_stream_event=events.append,
    ))
    assert first.status == OutcomeStatus.AWAITING_USER
    assert process.status == ProcessStatus.AWAITING_USER
    assert first.metadata["pending_decision"]["options"][0]["immediate_effect"]
    assert any(item.get("event") == "decision_required" for item in events)
    assert process.task_budget.snapshot()["used"]["awaiting_user"] is True

    decision_id = process.pending_decision["decision_id"]
    process.session.host_ledger.append({
        "event": "user_decision_received",
        "decision_id": decision_id,
        "answer": "Strict compatibility",
    })
    process.pending_decision = None
    process.task_budget.resume_from_user()
    process.status = ProcessStatus.RUNNING
    second = TaskOutcome.from_dict(agent_step(
        process,
        provider,
        instruction=f"[USER DECISION {decision_id}] Strict compatibility",
        workspace_path=str(tmp_path),
    ))
    assert second.status == OutcomeStatus.COMPLETED
    assert second.process_id == first.process_id
    assert process.task_budget.snapshot()["used"]["awaiting_user"] is False


class _IncompleteDecisionProvider(_DecisionProvider):
    """Ends at the token limit after emitting one complete function call."""

    def stream_events(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls > 1:
            raise AssertionError(
                "the completed call must execute before requesting continuation"
            )
        request = {
            "question": "Choose the durable compatibility policy.",
            "why_user_must_decide": "Both choices encode a product commitment.",
            "options": [
                {
                    "label": "Strict compatibility",
                    "principle": "Protect existing callers",
                    "immediate_effect": "Keep the legacy shape",
                    "downstream_effect": "Fewer migrations with slower cleanup",
                    "risks": "Legacy complexity remains",
                    "reversibility": "Deprecate in a later major version",
                    "recommended": True,
                },
                {
                    "label": "Clean break",
                    "principle": "Optimize the long-term API",
                    "immediate_effect": "Remove the legacy shape",
                    "downstream_effect": "Simpler core with caller migration",
                    "risks": "Existing callers break",
                    "reversibility": "Add a compatibility adapter",
                },
            ],
        }
        arguments = json.dumps(request)
        yield ProviderEvent(
            ProviderEventType.TOOL_CALL_DONE,
            output_index=0,
            tool_call_id="decision-incomplete-1",
            tool_name="request_user_decision",
            arguments=arguments,
        )
        yield ProviderEvent(
            ProviderEventType.RESPONSE_INCOMPLETE,
            artifact={
                "reason": "max_output_tokens",
                "response_output_items": [
                    {
                        "id": "fc-incomplete-1",
                        "type": "function_call",
                        "call_id": "decision-incomplete-1",
                        "name": "request_user_decision",
                        "arguments": arguments,
                        "status": "completed",
                    }
                ],
            },
        )


def test_completed_tool_call_executes_before_incomplete_response_continuation(
    tmp_path_factory,
):
    tmp_path = tmp_path_factory
    _manager, process = _supervisor(tmp_path)
    process.tool_registry = ToolRegistry([
        "request_user_decision", "calculate", "decision_evidence",
    ])
    provider = _IncompleteDecisionProvider()

    outcome = TaskOutcome.from_dict(agent_step(
        process,
        provider,
        instruction="Resolve the policy, asking the user when authority is required.",
        workspace_path=str(tmp_path),
    ))

    assert outcome.status == OutcomeStatus.AWAITING_USER
    assert process.status == ProcessStatus.AWAITING_USER
    assert process.pending_decision is not None
    assert provider.calls == 1
    assert not any(
        message.get("message_type") == "host_provider_continuation"
        for message in process.session.messages
    )


class _CompletionToolProvider:
    protocol = ProviderProtocol.OPENAI_RESPONSES
    capabilities = ProviderCapabilities(
        protocol=ProviderProtocol.OPENAI_RESPONSES.value,
        tools=True,
        prompt_cache="automatic",
        context_window=8192,
    )
    context_window = 8192

    def __init__(self):
        self.calls = 0

    def stream_events(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="applying")
            yield ProviderEvent(
                ProviderEventType.TOOL_CALL_DONE,
                output_index=0,
                tool_call_id="edit-1",
                tool_name="edit",
                arguments="{}",
            )
        elif self.calls == 2:
            yield ProviderEvent(
                ProviderEventType.TOOL_CALL_DONE,
                output_index=0,
                tool_call_id="complete-1",
                tool_name="complete_task",
                arguments=json.dumps({
                    "result": "bounded edit completed",
                    "verification": [],
                    "files": ["bounded.txt"],
                }),
            )
        else:  # A third provider turn would be redundant Host bookkeeping.
            raise AssertionError("complete_task should terminate without another LLM turn")
        yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)


def test_completion_tool_is_terminal_after_host_evidence_passes(tmp_path_factory):
    manager = AgentProcessManager(max_concurrency=2)
    process = manager.fork(
        parent_id=None,
        role="worker",
        tool_registry=ToolRegistry(["edit"]),
        max_steps=5,
        ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory),
        task_id="action-terminal-tool",
        actor_kind="worker",
        capability_profile_id="development.workspace",
        task_kind="action",
        task_budget_limits={"max_seconds": 10},
    )
    edit = AgentTool(
        execution_contract=data_broker("test.fixture"), name="edit",
        description="Apply one bounded test edit.",
        parameters={"type": "object", "properties": {}, "required": []},
        execute=lambda _args: {"success": True, "file": "bounded.txt"},
        read_only=False,
        effect=ToolEffect.WORKSPACE_WRITE,
    )
    dispatcher = type("Dispatcher", (), {"_executors": {"edit": edit}})()
    provider = _CompletionToolProvider()
    events = []

    outcome = TaskOutcome.from_dict(agent_step(
        process,
        provider,
        instruction="Perform the bounded edit and submit completion evidence.",
        dispatcher=dispatcher,
        workspace_path=str(tmp_path_factory),
        on_stream_event=events.append,
    ))

    debug = {
        "error": outcome.error.to_dict() if outcome.error else None,
        "claim": process.completion_claim.to_dict() if process.completion_claim else None,
        "events": [
            {key: item.get(key) for key in ("event", "tool_name", "is_error", "error")}
            for item in events
        ],
        "receipts": [
            {key: item.get(key) for key in ("tool_name", "succeeded", "committed", "effect")}
            for item in process.tool_receipts
        ],
    }
    assert outcome.status == OutcomeStatus.COMPLETED, debug
    assert outcome.response == "bounded edit completed"
    assert provider.calls == 2
    assert outcome.metadata["completion_evidence"]["factual_ready"] is True
    text_event = next(item for item in events if item.get("event") == "text_delta")
    assert text_event["delta"] == "applying"
    assert "accumulated" not in text_event


class _PlainTextAfterEvidenceProvider(_CompletionToolProvider):
    def stream_events(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="applying")
            yield ProviderEvent(
                ProviderEventType.TOOL_CALL_DONE,
                output_index=0,
                tool_call_id="edit-plain-1",
                tool_name="edit",
                arguments="{}",
            )
        elif self.calls == 2:
            yield ProviderEvent(
                ProviderEventType.TEXT_DELTA,
                text="The bounded edit is complete.",
            )
        else:
            raise AssertionError("Host evidence should compile the final text claim")
        yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)


def test_plain_text_after_host_evidence_compiles_terminal_claim(tmp_path_factory):
    manager = AgentProcessManager(max_concurrency=2)
    process = manager.fork(
        parent_id=None,
        role="worker",
        tool_registry=ToolRegistry(["edit"]),
        max_steps=5,
        ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory),
        task_id="action-terminal-plain-text",
        actor_kind="worker",
        capability_profile_id="development.workspace",
        task_kind="action",
        task_budget_limits={"max_seconds": 10},
    )
    edit = AgentTool(
        execution_contract=data_broker("test.fixture"), name="edit",
        description="Apply one bounded test edit.",
        parameters={"type": "object", "properties": {}, "required": []},
        execute=lambda _args: {"success": True, "file": "bounded.txt"},
        read_only=False,
        effect=ToolEffect.WORKSPACE_WRITE,
    )
    dispatcher = type("Dispatcher", (), {"_executors": {"edit": edit}})()
    provider = _PlainTextAfterEvidenceProvider()
    events = []

    outcome = TaskOutcome.from_dict(agent_step(
        process,
        provider,
        instruction="Perform the bounded edit and report the result.",
        dispatcher=dispatcher,
        workspace_path=str(tmp_path_factory),
        on_stream_event=events.append,
    ))

    assert outcome.status == OutcomeStatus.COMPLETED
    assert outcome.response == "The bounded edit is complete."
    assert provider.calls == 2
    assert process.completion_claim is not None
    assert process.completion_claim.source == "final_response"
    assert any(
        item.get("event") == "completion_gate"
        and item.get("source") == "host_evidence_plain_text"
        for item in events
    )


def test_prompt_rom_is_independent_of_task_contract(tmp_path_factory):
    tmp_path = tmp_path_factory
    _manager, process = _supervisor(tmp_path)
    text_a, sections_a = PromptCompiler.compile(
        process=process, tools={}, workspace_path=str(tmp_path), governance_brief="rule A",
    )
    PromptCompiler.upsert(process.session, text_a, sections_a)
    rom = next(item for item in process.session.messages
               if item.get("message_type") == "compiled_system_prompt")
    task = next(item for item in process.session.messages
                if item.get("message_type") == "compiled_task_contract")
    assert "rule A" not in rom["content"]
    assert "rule A" in task["content"]


def test_cache_telemetry_distinguishes_cold_start_epoch_and_provider_miss():
    session = AgentSession()
    intent = CacheIntent(mode="automatic", stable_prefix_key="same")
    session.record_provider_usage({"input_tokens": 100}, cache_intent=intent)
    session.record_provider_usage({"input_tokens": 100}, cache_intent=intent)
    session.context_epoch += 1
    session.record_provider_usage({"input_tokens": 100}, cache_intent=intent)
    assert [item["miss_reason"] for item in session.cache_telemetry] == [
        "cold_start", "provider_miss_or_unreported", "context_epoch_reset",
    ]
    assert session.cache_summary()["eligible_input_tokens"] == 100


def test_child_decision_resume_binds_its_own_session_even_from_parent_view(tmp_path_factory, monkeypatch):
    import queue
    from backend.core.daemon.dispatch import _cmd_task
    manager, root = _supervisor(tmp_path_factory)
    child = manager.fork(parent_id=root.process_id, role='worker', actor_kind='worker',
        tool_registry=ToolRegistry([]), max_steps=6, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id='child-task', capability_profile_id='text.only')
    pending = create_user_decision(child, _decision_args('Which language?'))
    child.status = ProcessStatus.AWAITING_USER
    calls = []
    def resolve(apm, store, workspace, process_id, session_id=''):
        calls.append((process_id, session_id))
        raise ValueError('TEST_STOP_AFTER_IDENTITY_BOUNDARY')
    monkeypatch.setattr('backend.core.loop.conversation_continuation.resolve_conversation_process', resolve)
    emitted = []
    _cmd_task({'action': 'decision', 'process_id': child.process_id,
        'decision_id': pending['decision_id'], 'session_id': root.session.session_id,
        'conversation_process_id': root.process_id, 'instruction': 'Choose option 1: A'},
        SimpleNamespace(workspace_path=tmp_path_factory), SimpleNamespace(name='project'),
        {'apm': manager, 'evq': queue.Queue(), 'dispatcher': object()}, emitted.append)
    assert calls == [(child.process_id, child.session.session_id)]
    assert emitted[-1]['error'] == 'TEST_STOP_AFTER_IDENTITY_BOUNDARY'
    assert child.session.host_ledger[-1]['event'] == 'user_decision_received'
    assert root.session.session_id != child.session.session_id


def test_cancelling_parked_child_refunds_retained_escrow_once(tmp_path_factory):
    manager, root = _supervisor(tmp_path_factory)
    child = manager.fork(parent_id=root.process_id, role='worker', actor_kind='worker',
        tool_registry=ToolRegistry([]), max_steps=6, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id='parked-child', capability_profile_id='text.only')
    create_user_decision(child, _decision_args('Choose a language'))
    child.status = ProcessStatus.AWAITING_USER
    child.lifecycle_finalized = True
    assert child.task_budget.decision_card(child.process_id)['current_process']['state'] == 'active'
    manager.kill(child.process_id, reason='synthetic user cancel')
    lease = child.task_budget.decision_card(child.process_id)['current_process']
    assert lease['state'] == 'released'
    assert lease['terminal_status'] == 'cancelled'
    assert lease['refunded_provider_calls'] > 0
    manager.kill(child.process_id, reason='duplicate cancel')
    assert child.task_budget.decision_card(child.process_id)['current_process'] == lease


def test_supervisor_decision_projection_reads_real_child_answers_and_keeps_task_scope(tmp_path_factory):
    from backend.core.loop.question_broker import decision_evidence
    manager, root = _supervisor(tmp_path_factory)
    child = manager.fork(parent_id=root.process_id, role='worker', actor_kind='worker',
        tool_registry=ToolRegistry([]), max_steps=6, ring_level=RingLevel.RING_3,
        workspace_path=str(tmp_path_factory), task_id='child-task', capability_profile_id='text.only')
    request = create_user_decision(child, _decision_args('Language?'))
    before = decision_evidence(child)
    assert before['items'][0]['answer_state'] == 'not_received'
    child.session.host_ledger.append({'event': 'user_decision_received', 'task_id': child.active_task_id,
        'decision_id': request['decision_id'], 'answer': 'Chinese'})
    child.pending_decision = None
    projected = decision_evidence(child)
    assert projected['items'][0]['status'] == 'answered'
    assert projected['items'][0]['answer_state'] == 'received'
    assert projected['items'][0]['answer'] == 'Chinese'
    assert projected['items'][0]['source_process_id'] == child.process_id
    assert projected['items'][0]['owner_process_id'] == root.process_id
    assert projected['items'][0]['options'][0]['label'] == 'A'
    assert not decision_evidence(root)['items']
    child.active_task_id = 'next-task'
    assert not decision_evidence(child)['items']

def test_semantic_choices_always_allow_other_text_without_opening_permission_controls(tmp_path_factory):
    manager,root=_supervisor(tmp_path_factory)
    args=_decision_args('Which language?')
    args['allow_free_form']=False
    request=create_user_decision(root,args)
    assert request['allow_free_form'] is True
    assert not any(e.get('event')=='user_decision_received' for e in root.session.host_ledger)
    root.pending_decision=None
    args['kind']='permission'
    request=create_user_decision(root,args)
    assert request['allow_free_form'] is False


def test_readiness_evidence_reports_its_task_and_candidate_scope(tmp_path_factory):
    from backend.core.loop.decision_support import collect_decision_evidence
    from backend.core.loop.tool_surface import admission_snapshot
    _manager,process=_supervisor(tmp_path_factory)
    process.task_kind="answer"
    facts=collect_decision_evidence(process,"completion")
    assert facts["completion"]["factual_ready"] is False
    assert facts["completion"]["effectful_receipts_required"] is False
    assert facts["completion"]["scope"]=="current_task_action_evidence"
    assert facts["outstanding_gates"]["ready"] is True
    assert facts["outstanding_gates"]["assessment_kind"]=="readiness_probe"
    assert facts["outstanding_gates"]["candidate_source"]=="placeholder"
    access=admission_snapshot(process,["declare_task_contract"],["declare_task_contract","delegate_task"])
    assert access["scope"]=="current_process_current_contract"
    assert access["inactive_catalog_scope"]=="self_execution_capability_profiles_only"
    assert access["control_protocols_require_task_routing"] is True
    assert access["authority_granted"] is False


def test_action_receipt_scope_does_not_waive_required_tests_for_answers(tmp_path_factory):
    from backend.core.loop.decision_support import collect_decision_evidence
    _manager,process=_supervisor(tmp_path_factory)
    process.task_kind="answer"
    process.required_test_ids=["missing-required-test"]
    facts=collect_decision_evidence(process,"completion")
    assert facts["completion"]["effectful_receipts_required"] is False
    assert facts["completion"]["test_failures"]
    assert facts["outstanding_gates"]["ready"] is False
