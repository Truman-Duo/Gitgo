"""Provider protocol adaptation and bounded continuation transport."""
from __future__ import annotations

from backend.core.prompt_templates import load_prompt
from .turn_guidance import _BASE_CURRENT_TURN_ENVELOPE
from .provider_protocol import ProviderEvent, ProviderEventType


def _stream_recovery_message(has_text: bool, had_partial_tool: bool) -> str:
    """生成流中断恢复提示消息。"""
    if had_partial_tool:
        return (
            load_prompt('recovery/stream_tool')
        )
    if has_text:
        return (
            load_prompt('recovery/stream_text')
        )
    return (
        load_prompt('recovery/stream_start')
    )


def _bounded_exception_chain(exc: BaseException, limit: int = 400) -> str:
    """Expose a bounded transport diagnostic without leaking request headers."""
    parts = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(parts) < 4:
        seen.add(id(current))
        message = str(current).strip()
        parts.append(type(current).__name__ + (f": {message}" if message else ""))
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)[:max(80, min(limit, 1000))]


def _reasoning_continuation(
    reasoning_content: str,
    reasoning_detail_chunks: list,
) -> dict:
    """Preserve raw provider reasoning without interpreting its authority."""
    continuation = {}
    if reasoning_content:
        continuation["reasoning_content"] = reasoning_content
    if reasoning_detail_chunks:
        if all(isinstance(item, str) for item in reasoning_detail_chunks):
            continuation["reasoning_details"] = "".join(reasoning_detail_chunks)
        else:
            continuation["reasoning_details"] = reasoning_detail_chunks
    return continuation


def _provider_continuation(
    reasoning_content: str,
    reasoning_detail_chunks: list,
    provider_artifacts: list[dict] | None = None,
) -> dict:
    """Build replay state for both normal and interrupted provider streams."""
    continuation = _reasoning_continuation(
        reasoning_content, reasoning_detail_chunks,
    )
    artifacts = list(provider_artifacts or [])
    if not artifacts:
        return continuation
    continuation["provider_artifacts"] = artifacts
    response_items = [
        item["response_output_item"] for item in artifacts
        if isinstance(item, dict) and item.get("response_output_item")
    ]
    anthropic_blocks = [
        item["anthropic_content_block"] for item in artifacts
        if isinstance(item, dict) and item.get("anthropic_content_block")
    ]
    if response_items:
        continuation["response_output_items"] = response_items
    if anthropic_blocks:
        continuation["anthropic_content_blocks"] = anthropic_blocks
    return continuation


def _filter_incomplete_response_artifacts(
    provider_artifacts: list[dict],
    completed_call_ids: set[str],
) -> list[dict]:
    """Drop unanswered native tool calls before replaying provider state.

    The Responses API requires every replayed ``function_call`` to be followed by
    a matching ``function_call_output``; Anthropic has the equivalent invariant
    for ``tool_use``/``tool_result``. An interrupted response can contain a call
    whose wire item is complete even though Gitgo never reached tool execution,
    so only IDs explicitly accepted for execution are replayable. Non-call
    artifacts (reasoning and partial assistant messages) remain replayable.
    """
    filtered: list[dict] = []
    for artifact in provider_artifacts:
        if not isinstance(artifact, dict):
            continue
        response_item = artifact.get("response_output_item")
        if isinstance(response_item, dict) and response_item.get("type") == "function_call":
            call_id = str(response_item.get("call_id", response_item.get("id", "")))
            if call_id and call_id in completed_call_ids:
                filtered.append(artifact)
            continue
        anthropic_block = artifact.get("anthropic_content_block")
        if isinstance(anthropic_block, dict) and anthropic_block.get("type") == "tool_use":
            call_id = str(anthropic_block.get("id", ""))
            if call_id and call_id in completed_call_ids:
                filtered.append(artifact)
            continue
        filtered.append(artifact)
    return filtered


def _iter_provider_events(
    llm_provider,
    session: "AgentSession",
    *,
    tools: list[dict] | None,
    cancel_event,
    cache_intent,
    metadata: dict | None = None,
    timeout: int = 45,
    current_turn_envelope: str = _BASE_CURRENT_TURN_ENVELOPE,
    max_tokens: int | None = None,
    before_attempt=None,
):
    """Use canonical events, with a compatibility bridge for test/legacy providers."""
    if hasattr(llm_provider, "stream_session_events"):
        yield from llm_provider.stream_session_events(
            session, current_turn_envelope=current_turn_envelope, tools=tools,
            cancel_event=cancel_event, cache_intent=cache_intent, metadata=metadata,
            timeout=timeout, max_tokens=max_tokens or llm_provider.max_output_tokens,
            before_attempt=before_attempt,
        )
        return
    if hasattr(llm_provider, "stream_events"):
        yield from llm_provider.stream_events(
            session.to_provider_messages(
                dynamic_envelope=current_turn_envelope,
                provider_route=str(getattr(llm_provider, "route_key", "") or ""),
            ), tools=tools,
            cancel_event=cancel_event, cache_intent=cache_intent,
            metadata=metadata,
            timeout=timeout,
            # The loop previously relied on stream_events' 4096-token default
            # even after provider probing had established a larger safe output
            # limit.  Reasoning tokens and long tool arguments share this
            # budget, so the hidden default could truncate ordinary writes.
            max_tokens=max(1, int(max_tokens or getattr(
                llm_provider, "max_output_tokens", 4096,
            ))),
        )
        return
    started: set[int] = set()
    for chunk in llm_provider.stream_chat(
        session.to_openai_messages(
            dynamic_envelope=current_turn_envelope,
            provider_route=str(getattr(llm_provider, "route_key", "") or ""),
        ),
        tools=tools, cancel_event=cancel_event,
        timeout=timeout,
        max_tokens=max(1, int(max_tokens or getattr(
            llm_provider, "max_output_tokens", 4096,
        ))),
    ):
        if chunk.get("usage"):
            from backend.core.loop.provider_protocol import normalize_usage
            yield ProviderEvent(
                ProviderEventType.USAGE, usage=normalize_usage(chunk["usage"]),
            )
        for choice in chunk.get("choices", []) or []:
            delta = choice.get("delta", {}) or {}
            if delta.get("content"):
                yield ProviderEvent(
                    ProviderEventType.TEXT_DELTA, text=str(delta["content"]),
                )
            if delta.get("reasoning_content"):
                yield ProviderEvent(
                    ProviderEventType.REASONING_DELTA,
                    reasoning=str(delta["reasoning_content"]),
                )
            if delta.get("reasoning_details") is not None:
                yield ProviderEvent(
                    ProviderEventType.REASONING_ARTIFACT,
                    artifact={"reasoning_details": delta["reasoning_details"]},
                )
            for call in delta.get("tool_calls", []) or []:
                index = int(call.get("index", 0) or 0)
                function = call.get("function", {}) or {}
                if index not in started:
                    started.add(index)
                    yield ProviderEvent(
                        ProviderEventType.TOOL_CALL_STARTED,
                        tool_call_id=str(call.get("id", "")),
                        tool_name=str(function.get("name", "")),
                        output_index=index,
                    )
                if function.get("arguments"):
                    yield ProviderEvent(
                        ProviderEventType.TOOL_CALL_DELTA,
                        tool_call_id=str(call.get("id", "")),
                        tool_name=str(function.get("name", "")),
                        arguments_delta=str(function["arguments"]),
                        output_index=index,
                    )
    yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)


def _provider_idle_timeout(process, *, cap_seconds: int = 120) -> int:
    """Bound semantic inactivity without limiting a productive long stream.

    The transport renews this allowance only for actual SSE data, not comment
    heartbeats. Reasoning can therefore stream for more than five minutes;
    a connection producing no model data for two minutes enters recovery.
    """
    budget = getattr(process, "task_budget", None)
    if budget is None:
        return cap_seconds
    remaining = float(budget.remaining_seconds())
    # Keep a small tail for cancellation, outcome persistence and A review.
    usable = max(1.0, remaining)
    return max(1, min(cap_seconds, int(usable)))
