"""Request observations bind actual selected input, not later live files/state."""
from copy import deepcopy
from types import SimpleNamespace

from backend.core.loop.llm import LLMProvider
from backend.core.loop.provider_protocol import ProviderEventType, ProviderRequest, deterministic_hash
from backend.core.loop.request_input import request_input_detail
from backend.core.loop.provider_turn_runtime import ProviderTurnRuntime
from backend.core.loop.session import AgentSession


def messages():
    return [{"role":"system", "content":"Original project rules", "message_type":"compiled_system_prompt",
             "prompt_schema_version":11,
             "prompt_sections":[{"name":"Project rules", "provenance":"project:rules", "version":"old-version"}]},
            {"role":"user", "content":"Actual task"}]


def test_observation_freezes_declared_sources_and_keeps_unavailable_versions_unknown():
    source = messages()
    source.append({"role":"user", "content":"Current snapshot", "host_authority":True,
                   "message_type":"host_context_update", "context_snapshot":{"set":[["/rules", "original"]]}})
    request = ProviderRequest(messages=tuple(source))
    detail = request_input_detail(request, {"model":"one", "messages":source})
    source[0]["prompt_sections"][0]["version"] = "later-version"
    source[0]["content"] = "Later rules"
    assert detail["messages"][0]["content"] == "Original project rules"
    assert detail["input_provenance"]["sources"][0]["sections"][0]["version"] == "old-version"
    assert detail["input_provenance"]["sources"][1]["sections"] == []
    assert detail["input_provenance"]["server_confirmed"] is False


def test_stream_input_and_observation_are_unchanged_if_the_callers_live_messages_change():
    bodies = []
    class Transport:
        def stream_sse(self, url, body, headers, **kwargs):
            bodies.append(deepcopy(body))
            yield "message", {"choices":[{"index":0,"delta":{"content":"ok"}, "finish_reason":"stop"}]}
    source = messages()
    provider = LLMProvider("https://fixture.invalid/v1", "fixture-key", "model", transport=Transport())
    stream = provider.stream_events(source, before_request=lambda:10)
    notice = next(stream)
    assert notice.type == ProviderEventType.ROUTING_NOTICE
    source[0]["content"] = "Changed after request construction"
    source[0]["prompt_sections"][0]["version"] = "changed"
    list(stream)
    detail = notice.artifact["_trace_detail"]
    assert bodies[0]["messages"][0]["content"] == "Original project rules"
    assert detail["messages"][0]["content"] == "Original project rules"
    assert detail["input_provenance"]["wire_body_sha256"] == deterministic_hash(bodies[0])


def test_runtime_uses_actual_snapshot_without_reopening_current_context_or_exposing_detail_inline():
    process = SimpleNamespace(session=AgentSession(), active_task_id="task", process_id="a")
    details, events = [], []
    def emit(event, **kwargs):
        events.append(event)
        details.append(kwargs["detail"])
    def cannot_reconstruct():
        raise AssertionError("the actual request observation must not reconstruct current context")
    runtime = ProviderTurnRuntime(process, process.session, None, emit=emit, frontend_emit=None,
        route_changed=lambda artifact:None, route=lambda:"route", envelope=cannot_reconstruct, compact=None)
    runtime.request_observation = {"event":"provider_request_started"}
    detail = request_input_detail(ProviderRequest(messages=tuple(messages())), {"input":"exact body"})
    runtime.route_notice({"event":"provider_request_started", "_trace_detail":detail})
    assert details == [detail]
    assert "_trace_detail" not in events[0] and "messages" not in events[0]
