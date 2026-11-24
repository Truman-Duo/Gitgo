"""Explicit model updates survive replay without becoming final answers."""
from types import SimpleNamespace

import pytest

from backend.core.loop.llm import LLMProvider
from backend.core.loop.provider_adapters import OpenAIResponsesAdapter
from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType
from backend.core.loop.provider_response import ProviderResponse, ProviderResponseCollector
from backend.core.loop.public_progress import progress_timeline, record_progress
from backend.core.loop.session import AgentSession
from backend.core.storage import StorageRuntime
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import RingLevel
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.protocol_tools.registry import build_internal_tools


def collector():
    session = AgentSession()
    process = SimpleNamespace(session=session, process_id="a", active_task_id="task",
                              steps_used=1, task_budget=None, task_kind="action")
    state, events = ProviderResponse(), []
    instance = ProviderResponseCollector(process, session, SimpleNamespace(protocol="openai_responses"),
        lambda event, **kw: events.append(event), events.append, None, "request:", state)
    return instance, process, state, events


def event(kind, **fields):
    return ProviderEvent(kind, response_id="response", message_id="message", output_index=0, **fields)


def test_commentary_streams_and_commits_once_without_answer_or_reasoning_promotion():
    instance, process, state, events = collector()
    instance.consume(event(ProviderEventType.TEXT_DELTA, text="Confirmed; ", phase="commentary"))
    instance.consume(event(ProviderEventType.REASONING_DELTA, reasoning="private"))
    instance.consume(event(ProviderEventType.TEXT_DELTA, text="checking callers.", phase="commentary"))
    assert state.accumulated_text == ""
    assert process.session.host_ledger == []
    for _ in range(2):
        instance.consume(event(ProviderEventType.MESSAGE_COMPLETED, phase="commentary"))
    instance.finish_partial_progress()
    instance.consume(event(ProviderEventType.TEXT_DELTA, text="private draft"))
    instance.consume(event(ProviderEventType.TEXT_DELTA, text="final", phase="final_answer"))
    assert state.accumulated_text == "private draftfinal"
    assert state.accumulated_reasoning == "private"
    assert len(process.session.host_ledger) == 1
    assert process.session.host_ledger[0]["message"] == "Confirmed; checking callers."
    assert [item["visibility"] for item in events if item["event"] == "text_delta"] == ["verbose", "public"]
    assert len([item for item in events if item["event"] == "progress_summary"]) == 1


def test_interrupted_public_message_is_retained_with_explicit_status():
    instance, process, _, _ = collector()
    instance.consume(event(ProviderEventType.TEXT_DELTA, text="Still checking", phase="commentary"))
    instance.finish_partial_progress()
    assert progress_timeline(process.session.host_ledger)[0]["progress_status"] == "interrupted"


def test_identity_is_scoped_to_task_and_conflicting_retries_are_rejected():
    _, process, _, _ = collector()
    record, created = record_progress(process, "One", "update")
    assert created
    assert record_progress(process, "One", "update") == (record, False)
    with pytest.raises(ValueError, match="ID_CONFLICT"):
        record_progress(process, "Different", "update")
    process.active_task_id = "next"
    record_progress(process, "Two", "update")
    rows = progress_timeline(process.session.host_ledger * 2)
    assert len(rows) == 2 and rows[0]["message_id"] != rows[1]["message_id"]


def test_native_phase_survives_sse_and_provider_replay():
    adapter = OpenAIResponsesAdapter()
    item = {"type": "message", "id": "message", "phase": "commentary", "role": "assistant",
            "content": [{"type": "output_text", "text": "Update"}]}
    events = list(adapter.iter_stream([
        ("response.created", {"response": {"id": "response"}}),
        ("response.output_item.added", {"output_index": 0, "item": item}),
        ("response.output_text.delta", {"output_index": 0, "delta": "Update"}),
        ("response.output_item.done", {"output_index": 0, "item": item}),
    ]))
    text = next(item for item in events if item.type == ProviderEventType.TEXT_DELTA)
    assert (text.phase, text.response_id, text.message_id) == ("commentary", "response", "message")
    assert next(item for item in events if item.type == ProviderEventType.MESSAGE_COMPLETED).phase == "commentary"
    artifact = next(item.artifact for item in events if item.type == ProviderEventType.REASONING_ARTIFACT)
    assert artifact["response_output_item"]["phase"] == "commentary"
    assert LLMProvider._accumulate_events(events)["content"] == ""


def test_nonstream_commentary_is_normalized_for_runtime_without_polluting_final_content():
    adapter = OpenAIResponsesAdapter()
    parsed = LLMProvider._accumulate_events(adapter.parse_response({"id":"r", "output":[
        {"type":"message", "id":"comment", "phase":"commentary", "content":[{"type":"output_text", "text":"Progress"}]},
        {"type":"message", "id":"answer", "phase":"final_answer", "content":[{"type":"output_text", "text":"Answer"}]},
    ]}))
    assert parsed["content"] == "Answer"
    assert parsed["public_progress"] == [{"response_id":"r", "message_id":"comment", "output_index":0, "text":"Progress"}]
    assert parsed["provider_artifacts"][0]["response_output_item"]["phase"] == "commentary"


def test_explicit_progress_tool_uses_the_existing_ledger_without_answer_or_permission_state(tmp_path_factory):
    root = tmp_path_factory / "progress-tool"
    root.mkdir()
    manager = AgentProcessManager()
    process = manager.fork(parent_id=None, role="supervisor", actor_kind="supervisor", task_kind="action",
        workspace_path=str(root), task_id="task", tool_registry=ToolRegistry([]), max_steps=8, ring_level=RingLevel.RING_0)
    events = []
    tools = build_internal_tools(process, {}, str(root), None, None, events.append)
    update = tools["report_progress"]
    first = update.execute({"message":"Confirmed; checking callers.", "update_id":"one"})
    assert update.execute({"message":"Confirmed; checking callers.", "update_id":"one"}) == first
    assert first == {"status":"recorded", "update_id":"one"}
    assert len(events) == 1 and events[0]["event"] == "progress_summary"
    assert not process.session.messages
    assert process.session.host_ledger[-1]["source"] == "agent"


def test_unknown_phase_is_never_inferred_from_prose_or_tool_boundary():
    instance, process, _, events = collector()
    instance.consume(event(ProviderEventType.TEXT_DELTA, text="I will check this next."))
    instance.consume(event(ProviderEventType.TOOL_CALL_STARTED, tool_name="read_file"))
    assert process.session.host_ledger == []
    assert not any(item["event"].startswith("progress") for item in events)


def test_progress_survives_checkpoint_reopen_in_order_and_outside_outcome(tmp_path_factory):
    workspace = tmp_path_factory / "progress-history"
    workspace.mkdir()
    _, process, _, _ = collector()
    process.session.messages = [{"role": "user", "content": "Investigate"}]
    first, _ = record_progress(process, "Confirmed the boundary", "one")
    decision = {"event": "user_decision_requested", "decision_id": "choice", "task_id": "task",
                "process_id": "a", "question": "Choose", "options": [], "message_sequence": 1}
    process.session.host_ledger.append(decision)
    record_progress(process, "Checking the remaining callers", "two")
    snapshot = {"process_id": "a", "session_id": "s", "task_id": "task", "parent_id": None,
        "actor_kind": "supervisor", "status": "completed", "messages": process.session.messages,
        "session_metadata": {"host_ledger": process.session.host_ledger + [first]},
        "result": {"status": "completed", "response": "Final answer", "process_id": "a"}}
    with StorageRuntime(workspace) as storage:
        storage.save_agent_checkpoint(snapshot)
    with StorageRuntime(workspace) as reopened:
        rows = reopened.read_latest_conversations()["main_conversation"]
        assert [row["kind"] for row in rows] == ["conversation", "progress", "decision", "progress", "outcome"]
        assert rows[-1]["content"] == "Final answer"
        assert rows[-1]["public_progress_keys"] == ["a:one", "a:two"]
