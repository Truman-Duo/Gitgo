"""Recorded-wire and context-model regression tests for phase two."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest

from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.context_policy import context_admission_policy
from backend.core.dispatch.truncation import format_tool_result
from backend.core.loop.context_store import ContextObjectStore, build_context_tools
from backend.core.loop.context_window import ContextWindow
from backend.core.loop.executor import agent_step
from backend.core.loop.llm import (
    LLMProvider, StreamCancelledError, StreamInterruptedError,
)
from backend.core.loop.models import RingLevel, ProcessStatus
from backend.core.loop.outcome import OutcomeStatus, TaskOutcome
from backend.core.loop.prompt_compiler import PromptCompiler, PromptSection
from backend.core.loop.provider_adapters import (
    AnthropicMessagesAdapter,
    OpenAIResponsesAdapter,
)
from backend.core.loop.provider_probe import ProviderProbe
from backend.core.loop.provider_transport import (
    HttpProviderTransport,
    ProviderHttpError,
    ProviderTransportCancelled,
)
from backend.core.loop.provider_protocol import (
    CacheIntent,
    ProviderEvent,
    ProviderEventType,
    ProviderRequest,
    normalize_usage,
)
from backend.core.loop.runtime import AgentRuntimeFactory, RuntimeSpec
from backend.core.loop.session import AgentSession
from backend.core.loop.tools import ToolRegistry


def _request(messages, *, tools=(), protocol_cache="automatic", metadata=None):
    return ProviderRequest(
        messages=tuple(messages), tools=tuple(tools), max_output_tokens=256,
        stream=True,
        cache_intent=CacheIntent(
            mode=protocol_cache, stable_prefix_key="stable-key",
            stable_prefix_hash="a" * 64, tool_schema_hash="b" * 64,
        ),
        metadata=dict(metadata or {}),
    )


def _web_search_tool():
    return {"type": "function", "function": {
        "name": "web_search", "description": "search", "parameters": {
            "type": "object", "properties": {"query": {"type": "string"}},
            "required": ["query"], "additionalProperties": False,
        },
    }}


def test_dynamic_envelope_is_tail_only_and_never_persisted():
    session = AgentSession()
    session.append_system("stable")
    session.append_user("task")
    before = list(session.messages)

    canonical = session.to_provider_messages(dynamic_envelope="current boundary")
    legacy = session.to_openai_messages(dynamic_envelope="current boundary")

    assert canonical[-1]["content"] == "current boundary"
    assert canonical[-1]["host_authority"] is True
    assert legacy[-1] == {"role": "user", "content": "current boundary"}
    assert session.messages == before


def test_provider_switch_keeps_public_dialogue_but_quarantines_native_state():
    session = AgentSession()
    route_a = "route-a"
    route_b = "route-b"
    session.bind_provider_route(route_a)
    session.append_user("remember MIMO-2718")
    session.append_assistant_provider(
        "recorded MIMO-2718",
        tool_calls=[{
            "id": "call-old", "type": "function",
            "function": {"name": "write_file", "arguments": "{}"},
        }],
        continuation_state={
            "reasoning_content": "old private reasoning",
            "response_output_items": [{
                "type": "function_call", "call_id": "call-old",
                "name": "write_file", "arguments": "{}",
            }],
        },
    )
    session.append_tool_result(
        "old write result", tool_name="write_file", tool_call_id="call-old",
    )

    same_route = session.to_provider_messages(provider_route=route_a)
    assert any(item.get("provider_state") for item in same_route)
    assert any(item.get("tool_call_id") == "call-old" for item in same_route)

    assert session.bind_provider_route(route_b) is True
    switched = session.to_provider_messages(provider_route=route_b)
    assert [item["content"] for item in switched] == [
        "remember MIMO-2718", "recorded MIMO-2718",
    ]
    assert not any(item.get("provider_state") for item in switched)
    assert not any(item.get("tool_call_id") == "call-old" for item in switched)


def test_first_provider_bind_quarantines_unscoped_legacy_continuation():
    session = AgentSession()
    session.append_assistant_provider(
        "legacy public answer",
        continuation_state={"reasoning_content": "ambiguous legacy state"},
    )
    assert session.active_provider_route == ""
    assert session.bind_provider_route("new-route") is True
    assert next(iter(session.provider_state.values()))["provider_route"] == (
        "legacy:unscoped"
    )
    messages = session.to_provider_messages(provider_route="new-route")
    assert messages == [{"role": "assistant", "content": "legacy public answer"}]


def test_provider_route_is_durable_session_metadata():
    session = AgentSession(active_provider_route="route-a")
    restored = AgentSession.from_durable_state({
        "session_id": session.session_id,
        "active_provider_route": session.active_provider_route,
    })
    assert restored.active_provider_route == "route-a"


def test_responses_hosted_search_replaces_only_the_host_fallback_tool():
    body = OpenAIResponsesAdapter().build_body(_request(
        [{"role": "user", "content": "search"}],
        tools=(_web_search_tool(),), metadata={"hosted_web_search": True},
    ), "model")
    assert body["tools"] == [{"type": "web_search"}]


def test_anthropic_hosted_search_uses_server_tool_contract():
    body = AnthropicMessagesAdapter().build_body(_request(
        [{"role": "user", "content": "search"}],
        tools=(_web_search_tool(),), metadata={"hosted_web_search": True},
    ), "model")
    assert body["tools"] == [{
        "type": "web_search_20250305", "name": "web_search", "max_uses": 5,
    }]


def test_responses_hosted_search_events_are_not_client_function_calls():
    events = list(OpenAIResponsesAdapter().iter_stream([
        ("response.output_item.added", {
            "type": "response.output_item.added", "output_index": 0,
            "item": {"type": "web_search_call", "id": "ws-1"},
        }),
        ("response.output_item.done", {
            "type": "response.output_item.done", "output_index": 0,
            "item": {"type": "web_search_call", "id": "ws-1", "status": "completed"},
        }),
    ]))
    assert [event.type for event in events] == [
        ProviderEventType.SERVER_TOOL_STARTED,
        ProviderEventType.SERVER_TOOL_RESULT,
        ProviderEventType.REASONING_ARTIFACT,
    ]
    assert not any(event.type == ProviderEventType.TOOL_CALL_DONE for event in events)


def test_responses_adapter_preserves_output_items_and_function_outputs():
    adapter = OpenAIResponsesAdapter()
    messages = [
        {"role": "system", "content": "stable constitution"},
        {"role": "user", "content": "inspect"},
        {"role": "assistant", "content": "", "provider_state": {
            "response_output_items": [{
                "type": "reasoning", "id": "r1", "encrypted_content": "opaque",
            }, {
                "type": "function_call", "call_id": "call-1",
                "name": "read_file", "arguments": "{\"path\":\"a.py\"}",
            }],
        }},
        {"role": "tool", "tool_call_id": "call-1", "content": "file contents"},
    ]
    tools = ({"type": "function", "function": {
        "name": "read_file", "description": "read", "parameters": {
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }},)
    body = adapter.build_body(_request(messages, tools=tools), "model")
    assert body["instructions"] == "stable constitution"
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["prompt_cache_key"] == "stable-key"
    assert any(item.get("encrypted_content") == "opaque" for item in body["input"])
    assert any(item.get("type") == "function_call_output" for item in body["input"])
    assert body["tools"][0]["name"] == "read_file"


def test_responses_typed_stream_normalizes_tool_usage_and_artifacts():
    adapter = OpenAIResponsesAdapter()
    wire = [
        ("response.created", {"type": "response.created", "response": {"id": "resp-1"}}),
        ("response.output_text.delta", {
            "type": "response.output_text.delta", "delta": "hello", "output_index": 0,
        }),
        ("response.output_item.added", {
            "type": "response.output_item.added", "output_index": 1,
            "item": {"type": "function_call", "call_id": "call-1", "name": "scan"},
        }),
        ("response.function_call_arguments.delta", {
            "type": "response.function_call_arguments.delta", "output_index": 1,
            "call_id": "call-1", "delta": "{\"path\":",
        }),
        ("response.function_call_arguments.delta", {
            "type": "response.function_call_arguments.delta", "output_index": 1,
            "call_id": "call-1", "delta": "\".\"}",
        }),
        ("response.output_item.done", {
            "type": "response.output_item.done", "output_index": 1,
            "item": {"type": "function_call", "call_id": "call-1", "name": "scan",
                     "arguments": "{\"path\":\".\"}"},
        }),
        ("response.completed", {"type": "response.completed", "response": {
            "id": "resp-1", "usage": {"input_tokens": 100, "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 10},
                "output_tokens_details": {"reasoning_tokens": 5}},
        }}),
    ]
    events = list(adapter.iter_stream(wire))
    assert "".join(e.text for e in events if e.type == ProviderEventType.TEXT_DELTA) == "hello"
    assert "".join(
        e.arguments_delta for e in events if e.type == ProviderEventType.TOOL_CALL_DELTA
    ) == '{"path":"."}'
    assert any(e.type == ProviderEventType.REASONING_ARTIFACT for e in events)
    usage = next(e.usage for e in events if e.type == ProviderEventType.USAGE)
    assert usage.cache_read_tokens == 80
    assert usage.cache_write_tokens == 10
    assert usage.reasoning_tokens == 5


def test_responses_nonstream_incomplete_is_not_treated_as_success():
    adapter = OpenAIResponsesAdapter()
    with pytest.raises(RuntimeError, match="response.incomplete"):
        adapter.parse_response({
            "id": "resp-short",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "reasoning", "id": "reasoning-only"}],
        })


def test_responses_stream_incomplete_is_a_typed_continuation_event():
    adapter = OpenAIResponsesAdapter()
    events = list(adapter.iter_stream([
        ("response.incomplete", {
            "type": "response.incomplete",
            "response": {
                "id": "resp-short", "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "reasoning", "id": "rs-short"}],
                "usage": {"input_tokens": 10, "output_tokens": 4096},
            },
        }),
    ]))
    incomplete = next(
        event for event in events
        if event.type == ProviderEventType.RESPONSE_INCOMPLETE
    )
    assert incomplete.artifact["reason"] == "max_output_tokens"
    assert incomplete.artifact["response_output_items"][0]["id"] == "rs-short"
    assert any(event.type == ProviderEventType.USAGE for event in events)


def test_responses_incomplete_function_item_is_never_executable():
    adapter = OpenAIResponsesAdapter()
    events = list(adapter.iter_stream([
        ("response.output_item.added", {
            "type": "response.output_item.added", "output_index": 0,
            "item": {"type": "function_call", "call_id": "call-cut",
                     "name": "write_file"},
        }),
        ("response.function_call_arguments.delta", {
            "type": "response.function_call_arguments.delta", "output_index": 0,
            "call_id": "call-cut", "delta": '{"path":"page.html","content":"<html>',
        }),
        ("response.output_item.done", {
            "type": "response.output_item.done", "output_index": 0,
            "item": {"type": "function_call", "call_id": "call-cut",
                     "name": "write_file", "status": "incomplete",
                     "arguments": '{"path":"page.html","content":"<html>'},
        }),
        ("response.incomplete", {
            "type": "response.incomplete",
            "response": {
                "id": "resp-cut", "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "function_call", "call_id": "call-cut",
                            "name": "write_file", "status": "incomplete"}],
            },
        }),
    ]))
    assert not any(
        event.type == ProviderEventType.TOOL_CALL_DONE for event in events
    )
    assert any(
        event.type == ProviderEventType.RESPONSE_INCOMPLETE for event in events
    )


def test_transport_normalizes_windows_read_error_after_cancel(monkeypatch):
    class ClosedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            raise AttributeError("closed chunk reader")

        def close(self):
            return None

    monkeypatch.setattr(
        "backend.core.loop.provider_transport.urllib.request.urlopen",
        lambda *_args, **_kwargs: ClosedResponse(),
    )
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ProviderTransportCancelled):
        list(HttpProviderTransport().stream_sse(
            "https://example.test/responses", {}, {}, timeout=1,
            cancel_event=cancelled,
        ))


def test_transport_normalizes_legacy_unpaired_surrogates_before_utf8():
    request = HttpProviderTransport()._request(
        "https://example.test/responses",
        {"input": "中文\udc80tail"},
        {},
    )
    assert request.data is not None
    decoded = request.data.decode("utf-8")
    assert "中文" in decoded
    assert "\\\\udc80tail" in decoded


def test_transport_heartbeats_do_not_mask_semantic_idle_timeout(monkeypatch):
    class HeartbeatResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter((b": keep-alive\n", b": keep-alive\n"))

        def close(self):
            return None

    monkeypatch.setattr(
        "backend.core.loop.provider_transport.urllib.request.urlopen",
        lambda *_args, **_kwargs: HeartbeatResponse(),
    )
    ticks = iter((0.0, 0.5, 1.1))
    monkeypatch.setattr(
        "backend.core.loop.provider_transport.time.monotonic",
        lambda: next(ticks),
    )

    with pytest.raises(RuntimeError, match="no semantic SSE data"):
        list(HttpProviderTransport().stream_sse(
            "https://example.test/responses", {}, {}, timeout=1,
        ))


def test_anthropic_adapter_uses_top_level_system_tool_result_and_automatic_cache():
    adapter = AnthropicMessagesAdapter()
    messages = [
        {"role": "system", "content": "constitution"},
        {"role": "user", "content": "work"},
        {"role": "assistant", "content": "", "provider_state": {
            "anthropic_content_blocks": [
                {"type": "thinking", "thinking": "private", "signature": "sig"},
                {"type": "tool_use", "id": "tool-1", "name": "scan", "input": {}},
            ],
        }},
        {"role": "tool", "tool_call_id": "tool-1", "content": "ok"},
    ]
    body = adapter.build_body(_request(messages), "claude-model")
    assert body["system"][0]["text"] == "constitution"
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-1]["role"] == "user"
    assert body["messages"][-1]["content"][0]["type"] == "tool_result"
    assistant = next(item for item in body["messages"] if item["role"] == "assistant")
    assert assistant["content"][0]["signature"] == "sig"


def test_anthropic_stream_preserves_thinking_signature_and_fragmented_tool_json():
    adapter = AnthropicMessagesAdapter()
    wire = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg-1", "usage": {"input_tokens": 12,
                "cache_read_input_tokens": 8, "cache_creation_input_tokens": 2},
        }}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
            "content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "consider"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1,
            "content_block": {"type": "tool_use", "id": "tool-1", "name": "scan"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": "{\"x\":"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": "1}"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 9}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    events = list(adapter.iter_stream(wire))
    artifact = next(
        e.artifact["anthropic_content_block"] for e in events
        if e.type == ProviderEventType.REASONING_ARTIFACT
        and e.artifact["anthropic_content_block"].get("type") == "thinking"
    )
    assert artifact["thinking"] == "consider"
    assert artifact["signature"] == "sig"
    done = next(e for e in events if e.type == ProviderEventType.TOOL_CALL_DONE)
    assert done.arguments == '{"x":1}'


def test_context_store_latest_pinned_and_session_memo(tmp_path_factory):
    store = ContextObjectStore(tmp_path_factory)
    first = store.put("governance/active", "line1\nold\nline3", media_type="text/plain")
    session = AgentSession()
    materialized = store.materialize(session, first["latest"])
    assert materialized["memo_hit"] is False
    assert store.materialize(session, first["latest"])["memo_hit"] is True

    second = store.put("governance/active", "line1\nnew\nline3", media_type="text/plain")
    assert store.resolve(first["pinned"]).content == "line1\nold\nline3"
    changed = store.materialize(session, second["latest"])
    assert changed["digest"] == second["digest"]
    assert changed["memo_hit"] is False


def test_context_store_pages_large_objects_and_serializes_ref_updates(tmp_path_factory):
    store = ContextObjectStore(tmp_path_factory)
    large = store.put("knowledge/large", "a" * 2500, media_type="text/plain")
    session = AgentSession()
    first_page = store.materialize(session, large["latest"], max_chars=1000)
    assert first_page["next_offset"] == 1000
    second_page = store.materialize(
        session, large["latest"], max_chars=1000,
        offset=first_page["next_offset"],
    )
    assert second_page["offset"] == 1000
    assert second_page["memo_hit"] is False

    def publish(index: int):
        return ContextObjectStore(tmp_path_factory).put(
            f"parallel/ref-{index}", {"index": index},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(publish, range(20)))
    for index in range(20):
        resolved = store.resolve(f"context:parallel/ref-{index}@latest")
        assert f'"index":{index}' in resolved.content


def test_context_and_artifact_pages_never_recursively_externalize(tmp_path_factory):
    hostile = ('"\\\n' * 20_000) + "done"
    store = ContextObjectStore(tmp_path_factory)
    ref = store.put("audit/escaped", hostile, media_type="text/plain")
    opened = store.materialize(
        AgentSession(), ref["pinned"], max_chars=100_000,
    )
    assert opened["next_offset"] is not None
    assert opened["transport_page_limited"] is True
    assert "完整内容已保存至" not in format_tool_result(
        "context_open", opened, str(tmp_path_factory),
    )

    artifact = tmp_path_factory / "escaped.txt"
    artifact.write_text(hostile, encoding="utf-8")
    process = SimpleNamespace(session=AgentSession())
    result = build_context_tools(process, str(tmp_path_factory))[
        "artifact_read"
    ].execute({"path": "escaped.txt", "max_chars": 100_000})
    assert result["next_offset"] is not None
    assert result["transport_page_limited"] is True
    assert "完整内容已保存至" not in format_tool_result(
        "artifact_read", result, str(tmp_path_factory),
    )


def test_prompt_update_is_append_only_and_date_is_host_only():
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="supervisor", actor_kind="supervisor",
        capability_profile_id="supervisor.control", ring_level=RingLevel.RING_0,
        tool_registry=ToolRegistry([]), max_steps=5, task_kind="supervisor",
    ))
    session = process.session
    text1, sections1 = PromptCompiler.compile(
        process=process, tools={}, workspace_path="C:/ws", governance_brief="v1",
    )
    PromptCompiler.upsert(session, text1, sections1)
    original = dict(session.messages[0])
    text2, sections2 = PromptCompiler.compile(
        process=process, tools={}, workspace_path="C:/ws", governance_brief="v2",
    )
    PromptCompiler.upsert(session, text2, sections2)
    assert session.messages[0] == original
    assert session.messages[-1]["message_type"] == "host_steering_delta"
    assert "v2" in session.messages[-1]["content"]
    assert "date=" not in text1
    assert session.host_ledger[0]["timestamp"]


def test_prompt_rom_update_contains_only_changed_sections():
    session = AgentSession()
    first = [
        PromptSection("Base identity", "stable identity", "host:identity"),
        PromptSection("Capabilities", "read_file", "host:capabilities", "1"),
        PromptSection("Runtime governance", "current rule", "host:governance", "1"),
    ]
    first_text = "\n\n".join(f"## {item.name}\n{item.content}" for item in first)
    PromptCompiler.upsert(session, first_text, first)

    second = [
        first[0],
        PromptSection("Capabilities", "read_file, web_search", "host:capabilities", "2"),
        first[2],
    ]
    second_text = "\n\n".join(f"## {item.name}\n{item.content}" for item in second)
    PromptCompiler.upsert(session, second_text, second)

    delta = session.messages[-1]["content"]
    assert "[HOST TASK CONTRACT UPDATE]" in delta
    assert "## Capabilities\nread_file, web_search" in delta
    assert "stable identity" not in delta
    assert "current rule" not in delta


def test_dynamic_role_and_capabilities_are_task_pinned_not_stable_rom():
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="supervisor", actor_kind="supervisor",
        capability_profile_id="supervisor.control", ring_level=RingLevel.RING_0,
        tool_registry=ToolRegistry([]), max_steps=5, task_kind="answer",
    ))
    text, sections = PromptCompiler.compile(
        process=process, tools={}, workspace_path="C:/ws",
    )
    PromptCompiler.upsert(process.session, text, sections)

    stable = process.session.messages[0]
    task = process.session.messages[1]
    stable_names = {item["name"] for item in stable["prompt_sections"]}
    task_names = {item["name"] for item in task["prompt_sections"]}
    assert stable_names == {
        "Behavior and delivery standard", "User collaboration protocol",
    }
    assert {"Base identity", "Role and authority", "Capabilities"} <= task_names


def test_missing_task_prefix_is_seeded_after_stable_without_host_delta():
    session = AgentSession()
    sections = [
        PromptSection("Behavior and delivery standard", "stable", "host:behavior"),
        PromptSection("Base identity", "current role", "host:identity"),
        PromptSection("Task contract", "current task", "host:task"),
    ]
    text = "\n\n".join(f"## {item.name}\n{item.content}" for item in sections)
    PromptCompiler.upsert(session, text, sections)
    session.messages = [
        item for item in session.messages
        if item.get("message_type") != "compiled_task_contract"
    ]
    session.compiled_prompt_hash = ""

    PromptCompiler.upsert(session, text, sections)

    assert [item.get("message_type") for item in session.messages] == [
        "compiled_system_prompt", "compiled_task_contract",
    ]


def test_compaction_creates_auditable_epoch_instead_of_editing_old_log():
    session = AgentSession()
    session.append_system("stable", message_type="compiled_system_prompt")
    session.append_assistant_provider(
        "old provider turn", continuation_state={"reasoning_content": "old"},
    )
    old_state_id = session.messages[-1]["provider_state_id"]
    for index in range(8):
        session.append_user(f"u{index}")
        session.append_assistant(f"a{index}")
    original = list(session.messages)

    class SummaryProvider:
        max_output_tokens = 4096

        def chat(self, *_args, **_kwargs):
            return {
                "content": "decisions and pending work",
                "reasoning_content": "raw compact reasoning",
                "provider_artifacts": [{"opaque": "signed"}],
            }

    assert ContextWindow(100).compact(session, SummaryProvider())
    assert session.context_epoch == 1
    assert session.epoch_archive[0]["messages"] == original
    assert old_state_id in session.epoch_archive[0]["provider_state"]
    assert old_state_id not in session.provider_state
    assert session.messages[0]["content"] == "stable"
    assert session.messages[1]["message_type"] == "compact_transcript"
    assert session.host_ledger[-1]["provider_state"]["reasoning_content"] == (
        "raw compact reasoning"
    )


def test_user_approved_force_compaction_is_bounded_and_keeps_local_epoch():
    session = AgentSession(model_context_limit=256_000)
    session.append_system("stable constitution", message_type="compiled_system_prompt")
    session.append_user("task contract", message_type="compiled_task_contract")
    for index in range(20):
        session.append_user(f"user {index} " + "x" * 2000)
        session.append_assistant(f"assistant {index} " + "y" * 2000)
    original_count = len(session.messages)

    window = ContextWindow(session.model_context_limit)
    assert window.force_compact(session, reason="test approval")

    assert session.context_epoch == 1
    assert len(session.epoch_archive[0]["messages"]) == original_count
    assert len(session.messages) == 3
    assert "USER APPROVED" in session.messages[-1]["content"]
    assert session.estimate_tokens() < 10_000


def test_completed_task_boundary_drops_stale_provider_mechanics_but_keeps_public_tail():
    session = AgentSession(model_context_limit=1_000_000)
    session.append_system("stable constitution", message_type="compiled_system_prompt")
    for index in range(10):
        session.append_user(f"public request {index}")
        session.append_assistant_provider(
            f"public result {index}",
            continuation_state={
                "reasoning_content": "old reasoning " + "x" * 30000,
                "response_output_items": [{"type": "function_call", "name": "old_tool"}],
            },
        )
        session.append_tool_result(
            "old internal result " + "y" * 5000,
            tool_name="old_tool", tool_call_id=f"call-{index}",
        )
        session.append_host_steering(
            "old capability update " + "z" * 5000,
            steering_type="prompt_contract_update", version=str(index),
        )
    old_tokens = session.estimate_tokens()

    assert session.compact_completed_task_boundary(
        checkpoint={
            "status": "completed",
            "response": "created solution.py and tests passed",
            "deliverables": ["solution.py"],
        },
        token_threshold=1000,
        retained_public_messages=4,
    ) is True

    assert session.context_epoch == 1
    assert session.provider_state == {}
    assert not any(item.get("role") == "tool" for item in session.messages)
    assert not any(item.get("message_type") == "host_steering_delta" for item in session.messages)
    assert [item["content"] for item in session.messages[-5:-1]] == [
        "public request 8", "public result 8",
        "public request 9", "public result 9",
    ]
    assert "created solution.py" in session.messages[-1]["content"]
    assert session.estimate_tokens() < old_tokens // 10


def test_completed_boundary_reseeds_legacy_dynamic_prompt_on_next_task():
    session = AgentSession()
    session.messages = [{
        "role": "system", "content": "old capabilities",
        "message_type": "compiled_system_prompt",
        "prompt_sections": [
            {"name": "Behavior and delivery standard"},
            {"name": "Capabilities"},
        ],
    }]
    session.append_user("old request " + "x" * 6000)
    session.append_assistant("old answer")

    assert session.compact_completed_task_boundary(
        checkpoint={"status": "completed", "response": "old answer"},
        token_threshold=64_000,
    )
    assert not any(
        item.get("message_type") in {"compiled_system_prompt", "compiled_task_contract"}
        for item in session.messages
    )
    assert session.messages[-1]["message_type"] == "compact_transcript"
    assert session.compiled_prompt_hash == ""
    assert session.compiled_prompt_sections == {}


def test_completed_task_boundary_preserves_small_iterative_session():
    session = AgentSession()
    session.append_user("adjust the same component")
    session.append_assistant("which detail should change?")
    before = list(session.messages)
    assert session.compact_completed_task_boundary(
        checkpoint={"status": "completed"}, token_threshold=1000,
    ) is False
    assert session.messages == before
    assert session.context_epoch == 0


def test_three_overflow_compaction_failures_pause_for_force_decision():
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="supervisor", actor_kind="supervisor",
        capability_profile_id="supervisor.control", ring_level=RingLevel.RING_0,
        tool_registry=ToolRegistry([]), max_steps=5, task_kind="answer",
        task_id="overflow-decision",
    ))
    for index in range(8):
        process.session.append_user(f"prior user {index}")
        process.session.append_assistant(f"prior assistant {index}")

    class OverflowProvider:
        protocol = SimpleNamespace(value="openai_responses")
        capabilities = SimpleNamespace(prompt_cache="automatic")
        context_window = 256_000
        max_output_tokens = 4096

        def stream_events(self, *_args, **_kwargs):
            if False:
                yield None
            raise StreamInterruptedError(
                retryable=False, status_code=400,
                error_body="maximum context length exceeded",
            )

        def chat(self, *_args, **_kwargs):
            raise RuntimeError("summary provider unavailable")

    outcome = TaskOutcome.from_dict(agent_step(
        process, OverflowProvider(), instruction="continue",
    ))

    assert outcome.status == OutcomeStatus.AWAITING_USER
    assert process.session.compaction_failure_count == 3
    assert process.pending_decision["kind"] == "context_force_compaction"
    assert process.pending_decision["error_catalog_id"] == "GITGO-E5203"


def test_sync_overflow_returns_to_host_without_hidden_output_token_retries(monkeypatch):
    from backend.core.loop.llm import LLMProvider
    provider = LLMProvider("https://example.test/v1", "test-only", "test-model")
    calls = []

    def overflow(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("maximum context length exceeded")

    monkeypatch.setattr(provider, "_chat_once", overflow)
    with pytest.raises(RuntimeError, match="context length"):
        provider.chat([{"role": "user", "content": "oversized"}], max_tokens=8192)
    assert len(calls) == 1


def test_outcome_duration_covers_the_whole_round_and_uses_shared_tree_trace(monkeypatch):
    from backend.core.loop.executor import _make_result
    from backend.core.loop.budget import TaskTreeBudget
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="worker", actor_kind="worker", capability_profile_id="text.only",
        ring_level=RingLevel.RING_3, tool_registry=ToolRegistry([]), max_steps=2,
        task_kind="answer", task_id="child-duration",
    ))
    process.task_budget = TaskTreeBudget.create("root-trace")
    process._turn_started_monotonic = 100.0
    process.status = ProcessStatus.COMPLETED
    monkeypatch.setattr("backend.core.loop.executor.time.monotonic", lambda: 162.125)
    outcome = _make_result(process, process.session, "done", duration_ms=12)
    assert outcome["duration_ms"] == 62125
    assert outcome["metadata"]["trace_id"] == "root-trace"


def test_outcome_contains_task_local_telemetry_and_bounds_session_checkpoint():
    from backend.core.loop.executor import _make_result
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="worker", actor_kind="worker", capability_profile_id="text.only",
        ring_level=RingLevel.RING_3, tool_registry=ToolRegistry([]), max_steps=2,
        task_kind="answer", task_id="incremental-outcome",
    ))
    session = process.session
    for index in range(70):
        session.record_provider_usage({"input_tokens": index + 1, "cache_read_tokens": index})
    process._turn_provider_usage_start = len(session.provider_usage)
    process._turn_cache_telemetry_start = len(session.cache_telemetry)
    process._turn_tool_receipts_start = 0
    session.record_provider_usage({"input_tokens": 100, "cache_read_tokens": 90})
    process.status = ProcessStatus.COMPLETED

    outcome = _make_result(process, session, "done")

    assert [item["input_tokens"] for item in outcome["metadata"]["provider_usage"]] == [100]
    assert len(outcome["metadata"]["cache_telemetry"]) == 1
    assert outcome["metadata"]["cache_summary"]["raw_hit_ratio"] == 0.9
    assert len(session.provider_usage) == 64
    assert len(session.cache_telemetry) == 64


def test_prompt_cache_key_reuses_stable_prefix_across_sessions_and_epochs():
    left = AgentSession()
    right = AgentSession()
    for session in (left, right):
        session.append_system("stable", message_type="compiled_system_prompt")
        session.compiled_prompt_hash = "schema"
    left.context_epoch = 3
    assert left.cache_intent([], mode="automatic").stable_prefix_key == (
        right.cache_intent([], mode="automatic").stable_prefix_key
    )
    assert left.cache_intent([{"name": "x"}], mode="automatic").stable_prefix_key != (
        right.cache_intent([], mode="automatic").stable_prefix_key
    )


def test_context_estimate_includes_provider_continuation_state():
    session = AgentSession()
    baseline = session.estimate_tokens()
    session.append_assistant_provider(
        "", continuation_state={"encrypted_content": "x" * 4000},
    )
    assert session.estimate_tokens() >= baseline + 1000


def test_restored_legacy_surrogate_markers_remain_visible_but_not_provider_visible():
    session = AgentSession.from_durable_state({
        "session_id": "legacy-corrupt",
        "messages": [
            {"role": "user", "content": "bad \\udcaf text \\udc80"},
            {"role": "assistant", "content": "answer derived from corrupt input"},
            {"role": "user", "content": "clean follow-up"},
        ],
    })
    assert session.messages[0]["integrity_status"] == "quarantined_legacy_unicode"
    assert session.messages[1]["integrity_status"] == "quarantined_legacy_unicode"
    assert [item["content"] for item in session.to_provider_messages()] == ["clean follow-up"]


def test_live_session_quarantines_legacy_unicode_chain_without_daemon_restart():
    session = AgentSession()
    session.messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "bad \\udcaf text \\udc80"},
        {"role": "assistant", "content": "derived diagnosis"},
        {"role": "user", "content": "what now"},
    ]
    assert [item["content"] for item in session.to_provider_messages()] == [
        "stable", "what now",
    ]


def test_canonical_executor_records_reasoning_usage_and_cache_telemetry():
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="worker", actor_kind="worker", capability_profile_id="text.only",
        ring_level=RingLevel.RING_3, tool_registry=ToolRegistry([]), max_steps=3,
        task_kind="answer", task_id="canonical-events",
    ))

    class CanonicalProvider:
        protocol = SimpleNamespace(value="openai_responses")
        capabilities = SimpleNamespace(prompt_cache="automatic")
        context_window = 4096

        def stream_events(self, *_args, **_kwargs):
            yield ProviderEvent(
                ProviderEventType.REASONING_ARTIFACT,
                artifact={"response_output_item": {
                    "type": "reasoning", "encrypted_content": "opaque",
                }},
            )
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="done\nTASK_COMPLETE")
            yield ProviderEvent(
                ProviderEventType.USAGE,
                usage=normalize_usage({
                    "input_tokens": 100, "output_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 75},
                }),
            )
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)

    outcome = TaskOutcome.from_dict(agent_step(
        process, CanonicalProvider(), instruction="answer",
    ))
    assert outcome.status == OutcomeStatus.COMPLETED
    state = list(process.session.provider_state.values())[-1]
    assert state["response_output_items"][0]["encrypted_content"] == "opaque"
    assert process.session.cache_telemetry[-1]["cache_read_tokens"] == 75


def test_executor_continues_typed_provider_truncation_without_repeating_work():
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="worker", actor_kind="worker", capability_profile_id="text.only",
        ring_level=RingLevel.RING_3, tool_registry=ToolRegistry([]), max_steps=3,
        task_kind="answer", task_id="typed-incomplete",
    ))

    class TruncatingProvider:
        protocol = SimpleNamespace(value="openai_responses")
        capabilities = SimpleNamespace(prompt_cache="automatic")
        context_window = 16_384
        calls = 0

        def stream_events(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                yield ProviderEvent(
                    ProviderEventType.REASONING_DELTA, reasoning="partial reasoning",
                )
                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="partial answer")
                yield ProviderEvent(
                    ProviderEventType.RESPONSE_INCOMPLETE,
                    artifact={
                        "reason": "max_output_tokens",
                        "response_output_items": [{
                            "type": "reasoning", "id": "rs-1",
                            "encrypted_content": "opaque",
                        }],
                    },
                )
                return
            yield ProviderEvent(
                ProviderEventType.TEXT_DELTA,
                text="final answer\nTASK_COMPLETE",
            )
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)

    provider = TruncatingProvider()
    outcome = TaskOutcome.from_dict(agent_step(
        process, provider, instruction="answer",
    ))
    assert outcome.status == OutcomeStatus.COMPLETED
    assert provider.calls == 2
    assert any(
        message.get("message_type") == "host_provider_continuation"
        for message in process.session.messages
    )
    assert any(
        state.get("response_output_items", [{}])[0].get("encrypted_content") == "opaque"
        for state in process.session.provider_state.values()
        if state.get("response_output_items")
    )


def test_direct_answer_bounds_reasoning_only_recovery_and_isolates_failed_turn():
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="worker", actor_kind="worker", capability_profile_id="text.only",
        ring_level=RingLevel.RING_3, tool_registry=ToolRegistry([]), max_steps=3,
        task_kind="answer", task_id="reasoning-only-incomplete",
    ))

    class ReasoningOnlyProvider:
        protocol = SimpleNamespace(value="openai_responses")
        capabilities = SimpleNamespace(prompt_cache="automatic")
        context_window = 128_000
        max_output_tokens = 16_384
        calls = 0
        requested_max_tokens = 0

        def stream_events(self, *_args, **kwargs):
            self.calls += 1
            self.requested_max_tokens = kwargs["max_tokens"]
            yield ProviderEvent(
                ProviderEventType.REASONING_DELTA, reasoning="inconclusive recovery attempt",
            )
            yield ProviderEvent(
                ProviderEventType.RESPONSE_INCOMPLETE,
                artifact={"reason": "max_output_tokens"},
            )

    provider = ReasoningOnlyProvider()
    outcome = TaskOutcome.from_dict(agent_step(
        process, provider, instruction="explain this character",
    ))

    assert outcome.status == OutcomeStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "PROVIDER_NO_PROGRESS"
    assert provider.calls == 2
    assert provider.requested_max_tokens == 8192
    recovery_messages = [
        message for message in process.session.messages
        if message.get("message_type") == "host_provider_continuation"
    ]
    assert recovery_messages
    assert all(message.get("provider_visible") is False for message in recovery_messages)
    assert process.session.provider_state == {}
    assert process.session.host_ledger[-1]["event"] == "provider_turn_quarantined"
    failed_user = next(
        message for message in process.session.messages
        if message.get("content") == "explain this character"
    )
    assert failed_user["provider_visible"] is False

    process.session.append_user("你好", message_type="conversation")
    visible = process.session.to_provider_messages()
    visible_user_text = [
        message.get("content") for message in visible
        if message.get("role") == "user"
    ]
    assert "你好" in visible_user_text
    assert "explain this character" not in visible_user_text
    assert not any("NO-PROGRESS RECOVERY" in str(text) for text in visible_user_text)


def test_direct_answer_no_progress_recovery_can_finish_the_same_user_turn():
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="worker", actor_kind="worker", capability_profile_id="text.only",
        ring_level=RingLevel.RING_3, tool_registry=ToolRegistry([]), max_steps=3,
        task_kind="answer", task_id="reasoning-recovery-success",
    ))

    class RecoveringProvider:
        protocol = SimpleNamespace(value="openai_responses")
        capabilities = SimpleNamespace(prompt_cache="automatic")
        context_window = 128_000
        max_output_tokens = 16_384
        calls = 0

        def stream_events(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                yield ProviderEvent(
                    ProviderEventType.REASONING_DELTA,
                    reasoning="unnecessarily long hidden analysis",
                )
                yield ProviderEvent(
                    ProviderEventType.RESPONSE_INCOMPLETE,
                    artifact={"reason": "max_output_tokens"},
                )
                return
            yield ProviderEvent(
                ProviderEventType.TEXT_DELTA,
                text="literal concise answer",
            )
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)

    provider = RecoveringProvider()
    outcome = TaskOutcome.from_dict(agent_step(
        process, provider, instruction="explain the quoted text literally",
    ))

    assert outcome.status == OutcomeStatus.COMPLETED
    assert outcome.response == "literal concise answer"
    assert provider.calls == 2
    assert not any(
        item.get("event") == "provider_turn_quarantined"
        for item in process.session.host_ledger
    )


def test_cancelled_stream_keeps_native_provider_artifacts_for_replay():
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="worker", actor_kind="worker", capability_profile_id="text.only",
        ring_level=RingLevel.RING_3, tool_registry=ToolRegistry([]), max_steps=2,
        task_kind="answer", task_id="cancelled-provider-state",
    ))

    class CancelledProvider:
        protocol = SimpleNamespace(value="anthropic_messages")
        capabilities = SimpleNamespace(prompt_cache="automatic")
        context_window = 4096

        def stream_events(self, *_args, **_kwargs):
            yield ProviderEvent(
                ProviderEventType.REASONING_ARTIFACT,
                artifact={"anthropic_content_block": {
                    "type": "thinking", "thinking": "private",
                    "signature": "signed",
                }},
            )
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="partial")
            raise StreamCancelledError()

    outcome = TaskOutcome.from_dict(agent_step(
        process, CancelledProvider(), instruction="answer",
    ))
    assert outcome.status == OutcomeStatus.CANCELLED
    state = list(process.session.provider_state.values())[-1]
    assert state["anthropic_content_blocks"][0]["signature"] == "signed"


def test_interrupted_responses_call_is_not_replayed_without_output():
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="worker", actor_kind="worker", capability_profile_id="text.only",
        ring_level=RingLevel.RING_3, tool_registry=ToolRegistry([]), max_steps=3,
        task_kind="answer", task_id="interrupted-responses-call",
    ))

    class InterruptedResponsesProvider:
        protocol = SimpleNamespace(value="openai_responses")
        capabilities = SimpleNamespace(prompt_cache="automatic")
        context_window = 4096
        calls = 0

        def stream_events(self, messages, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                yield ProviderEvent(
                    ProviderEventType.REASONING_DELTA,
                    reasoning="partial reasoning before the tool call",
                )
                yield ProviderEvent(
                    ProviderEventType.REASONING_ARTIFACT,
                    artifact={"response_output_item": {
                        "type": "reasoning", "id": "reasoning-1",
                        "encrypted_content": "opaque-reasoning",
                    }},
                )
                yield ProviderEvent(
                    ProviderEventType.TOOL_CALL_STARTED,
                    tool_call_id="call-interrupted", tool_name="read_file",
                    output_index=0,
                )
                yield ProviderEvent(
                    ProviderEventType.TOOL_CALL_DONE,
                    tool_call_id="call-interrupted", tool_name="read_file",
                    arguments='{"path":"README.md"}', output_index=0,
                )
                yield ProviderEvent(
                    ProviderEventType.REASONING_ARTIFACT,
                    artifact={"response_output_item": {
                        "type": "function_call", "call_id": "call-interrupted",
                        "name": "read_file", "arguments": '{"path":"README.md"}',
                    }},
                )
                raise StreamInterruptedError()

            native_items = [
                item
                for message in messages
                for item in (message.get("provider_state", {}).get(
                    "response_output_items", []
                ) or [])
            ]
            assert any(item.get("type") == "reasoning" for item in native_items)
            assert not any(item.get("type") == "function_call" for item in native_items)
            yield ProviderEvent(
                ProviderEventType.TEXT_DELTA,
                text="continued safely\nTASK_COMPLETE",
            )
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)

    provider = InterruptedResponsesProvider()
    outcome = TaskOutcome.from_dict(agent_step(
        process, provider, instruction="answer",
    ))
    assert outcome.status == OutcomeStatus.COMPLETED
    assert provider.calls == 2


def test_stream_recovery_sync_fallback_does_not_crash_on_unbound_invalid_calls():
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="worker", actor_kind="worker", capability_profile_id="text.only",
        ring_level=RingLevel.RING_3, tool_registry=ToolRegistry([]), max_steps=3,
        task_kind="answer", task_id="stream-sync-fallback",
    ))

    class FallbackProvider:
        protocol = SimpleNamespace(value="openai_responses")
        capabilities = SimpleNamespace(prompt_cache="automatic")
        context_window = 4096
        max_output_tokens = 4096
        stream_calls = 0
        chat_calls = 0

        def stream_events(self, *_args, **_kwargs):
            self.stream_calls += 1
            if False:
                yield None
            raise StreamInterruptedError()

        def chat(self, *_args, **_kwargs):
            self.chat_calls += 1
            return {"content": "recovered\nTASK_COMPLETE", "tool_calls": []}

    provider = FallbackProvider()
    outcome = TaskOutcome.from_dict(agent_step(
        process, provider, instruction="answer",
    ))
    assert outcome.status == OutcomeStatus.COMPLETED
    assert provider.stream_calls == 2
    assert provider.chat_calls == 1


def test_live_turn_seals_legacy_dangling_responses_call_before_request():
    process = AgentRuntimeFactory.create(RuntimeSpec(
        role="worker", actor_kind="worker", capability_profile_id="text.only",
        ring_level=RingLevel.RING_3, tool_registry=ToolRegistry([]), max_steps=2,
        task_kind="answer", task_id="repair-live-dangling-call",
    ))
    process.session.append_assistant_provider(
        "",
        continuation_state={"response_output_items": [{
            "type": "function_call", "call_id": "call-legacy-dangling",
            "name": "read_file", "arguments": '{"path":"README.md"}',
        }]},
    )

    class RepairAwareProvider:
        protocol = SimpleNamespace(value="openai_responses")
        capabilities = SimpleNamespace(prompt_cache="automatic")
        context_window = 4096

        def stream_events(self, messages, **_kwargs):
            assistant_index = next(
                index for index, message in enumerate(messages)
                if message.get("provider_state", {}).get("response_output_items")
            )
            tool_index = next(
                index for index, message in enumerate(messages)
                if message.get("tool_call_id") == "call-legacy-dangling"
            )
            user_index = next(
                index for index, message in enumerate(messages)
                if message.get("role") == "user" and message.get("content") == "continue"
            )
            assert assistant_index < tool_index < user_index
            outputs = {
                str(message.get("tool_call_id")): message
                for message in messages if message.get("role") == "tool"
            }
            repaired = outputs["call-legacy-dangling"]
            assert repaired["data"]["execution_state"] == "unknown"
            assert repaired["data"]["code"] == "LIVE_TOOL_RESULT_UNAVAILABLE"
            yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="repaired")
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)

    outcome = TaskOutcome.from_dict(agent_step(
        process, RepairAwareProvider(), instruction="continue",
    ))
    assert outcome.status == OutcomeStatus.COMPLETED


class _ProbeTransport:
    def post_json(self, url, body, headers, **_kwargs):
        assert url.endswith("/responses")
        return {
            "id": "resp-sync", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "pong"},
            ]}], "usage": {"input_tokens": 4, "output_tokens": 1},
        }

    def stream_sse(self, url, body, headers, **_kwargs):
        assert url.endswith("/responses")
        if any(tool.get("type") == "web_search" for tool in body.get("tools", [])):
            yield "response.output_item.added", {
                "type": "response.output_item.added", "output_index": 0,
                "item": {"type": "web_search_call", "id": "ws-probe"},
            }
            yield "response.output_item.done", {
                "type": "response.output_item.done", "output_index": 0,
                "item": {"type": "web_search_call", "id": "ws-probe", "status": "completed"},
            }
            yield "response.completed", {"type": "response.completed", "response": {
                "id": "r-web", "usage": {"input_tokens": 8, "output_tokens": 2},
            }}
            return
        yield "response.created", {"type": "response.created", "response": {"id": "r"}}
        yield "response.output_item.added", {
            "type": "response.output_item.added", "output_index": 0,
            "item": {"type": "function_call", "call_id": "c", "name": "gitgo_probe_echo"},
        }
        yield "response.output_item.done", {
            "type": "response.output_item.done", "output_index": 0,
            "item": {"type": "function_call", "call_id": "c",
                     "name": "gitgo_probe_echo", "arguments": "{\"value\":\"probe\"}"},
        }
        yield "response.completed", {"type": "response.completed", "response": {
            "id": "r", "usage": {"input_tokens": 10, "output_tokens": 3},
        }}


def test_explicit_probe_resolves_auto_once_and_returns_manifest():
    result = ProviderProbe(_ProbeTransport()).probe(
        base_url="https://example.test/v1", api_key="key", model_id="model",
        protocol="auto", timeout=1,
    )
    assert result.protocol == "openai_responses"
    assert result.capabilities.streaming is True
    assert result.capabilities.tools is True
    assert result.capabilities.hosted_web_search is True
    assert result.capabilities.probe_version == 2
    assert "tool_call_observed" in result.observations
    assert "hosted_web_search_observed" in result.observations


def test_auto_hosted_search_fallback_is_visible_and_learned_for_the_provider():
    class FallbackTransport:
        def __init__(self):
            self.bodies = []

        def stream_sse(self, _url, body, _headers, **_kwargs):
            self.bodies.append(body)
            if any(tool.get("type") == "web_search" for tool in body.get("tools", [])):
                raise ProviderHttpError(400, "hosted tool unsupported", {})
            yield "response.created", {"type": "response.created", "response": {"id": "r"}}
            yield "response.completed", {"type": "response.completed", "response": {"id": "r"}}

    transport = FallbackTransport()
    provider = LLMProvider(
        "https://example.test/v1", "key", "model",
        protocol="openai_responses", transport=transport,
    )
    events = list(provider.stream_events(
        [{"role": "user", "content": "search"}],
        tools=[_web_search_tool()],
        metadata={"hosted_web_search": True, "web_search_mode": "auto"},
    ))

    fallback = [event for event in events
                if event.type == ProviderEventType.PROVIDER_CAPABILITY_FALLBACK]
    assert len(fallback) == 1
    assert fallback[0].artifact["to"] == "searxng"
    assert provider.capabilities.hosted_web_search is False
    assert provider.capabilities.probe_version == 2
    assert transport.bodies[0]["tools"] == [{"type": "web_search"}]
    assert transport.bodies[1]["tools"][0]["type"] == "function"


def test_role_context_policy_prefetches_governance_and_never_raw_reasoning():
    reviewer = context_admission_policy("reviewer", "review")
    assert "governance_active" in reviewer["eager_privileged"]
    assert "project_lessons" in reviewer["lazy"]
    assert "raw_reasoning" in reviewer["never"]


def test_answer_context_policy_does_not_prefetch_project_governance():
    answer = context_admission_policy("supervisor", "answer")
    assert answer["eager_privileged"] == []
    assert "project_lessons" in answer["lazy"]
    assert "raw_reasoning" in answer["never"]
