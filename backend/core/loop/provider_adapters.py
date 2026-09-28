"""Wire adapters for the three provider protocols supported by Gitgo."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator

from backend.core.loop.provider_protocol import (
    ProviderEvent,
    ProviderEventType,
    ProviderProtocol,
    ProviderRequest,
    normalize_usage,
)
from backend.core.loop.agent_tool import normalize_tool_parameters


class ProviderAdapter(ABC):
    protocol: ProviderProtocol

    @abstractmethod
    def endpoint(self, base_url: str) -> str: ...

    @abstractmethod
    def headers(self, api_key: str) -> dict: ...

    @abstractmethod
    def build_body(self, request: ProviderRequest, model_id: str) -> dict: ...

    @abstractmethod
    def parse_response(self, data: dict) -> list[ProviderEvent]: ...

    @abstractmethod
    def iter_stream(
        self, events: Iterable[tuple[str, dict]],
    ) -> Iterator[ProviderEvent]: ...


def _canonical_tools(tools: tuple[dict, ...]) -> list[dict]:
    result = []
    for raw in tools:
        function = raw.get("function", raw)
        result.append({
            "name": str(function.get("name", "")),
            "description": str(function.get("description", "")),
            "parameters": normalize_tool_parameters(function.get("parameters")),
        })
    return result


class OpenAIChatAdapter(ProviderAdapter):
    protocol = ProviderProtocol.OPENAI_CHAT

    def endpoint(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/chat/completions"

    def headers(self, api_key: str) -> dict:
        return {"Authorization": f"Bearer {api_key}"}

    def build_body(self, request: ProviderRequest, model_id: str) -> dict:
        body = {
            "model": model_id,
            "messages": [self._chat_message(item) for item in request.messages],
            "max_tokens": request.max_output_tokens,
            "stream": request.stream,
        }
        body["messages"] = [item for item in body["messages"] if item is not None]
        if request.tools:
            body["tools"] = list(request.tools)
            body["tool_choice"] = "auto"
        return body

    @staticmethod
    def _chat_message(message: dict) -> dict | None:
        role = message.get("role", "user")
        wire = {"role": role, "content": message.get("content", "")}
        state = message.get("provider_state", {}) or {}
        for key in ("tool_calls", "reasoning_content", "reasoning_details"):
            if key in state:
                wire[key] = state[key]
            elif key in message:
                wire[key] = message[key]
        if role == "tool":
            call_id = message.get("tool_call_id", "")
            if not call_id:
                return {"role": "user", "content": message.get("content", "")}
            wire["tool_call_id"] = call_id
        return wire

    def parse_response(self, data: dict) -> list[ProviderEvent]:
        message = data.get("choices", [{}])[0].get("message", {})
        events: list[ProviderEvent] = [ProviderEvent(
            ProviderEventType.RESPONSE_STARTED,
            response_id=str(data.get("id", "")), raw=data,
        )]
        content = message.get("content", "") or ""
        if content:
            events.append(ProviderEvent(ProviderEventType.TEXT_DELTA, text=content))
        if message.get("reasoning_content"):
            events.append(ProviderEvent(
                ProviderEventType.REASONING_DELTA,
                reasoning=str(message["reasoning_content"]),
            ))
        if message.get("reasoning_details") is not None:
            events.append(ProviderEvent(
                ProviderEventType.REASONING_ARTIFACT,
                artifact={"reasoning_details": message["reasoning_details"]},
            ))
        for index, call in enumerate(message.get("tool_calls", []) or []):
            function = call.get("function", {})
            events.append(ProviderEvent(
                ProviderEventType.TOOL_CALL_DONE,
                tool_call_id=str(call.get("id", "")),
                tool_name=str(function.get("name", "")),
                arguments=str(function.get("arguments", "{}")),
                output_index=index,
            ))
        if data.get("usage"):
            events.append(ProviderEvent(
                ProviderEventType.USAGE, usage=normalize_usage(data["usage"]),
            ))
        events.append(ProviderEvent(
            ProviderEventType.RESPONSE_COMPLETED,
            response_id=str(data.get("id", "")), raw=data,
        ))
        return events

    def iter_stream(self, events: Iterable[tuple[str, dict]]) -> Iterator[ProviderEvent]:
        started: set[int] = set()
        response_id = ""
        response_started = False
        for _event_name, data in events:
            response_id = response_id or str(data.get("id", ""))
            if response_id and not response_started:
                response_started = True
                yield ProviderEvent(
                    ProviderEventType.RESPONSE_STARTED, response_id=response_id,
                )
            if data.get("usage"):
                yield ProviderEvent(
                    ProviderEventType.USAGE, usage=normalize_usage(data["usage"]),
                )
            for choice in data.get("choices", []) or []:
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
                    call_id = str(call.get("id", ""))
                    if index not in started:
                        started.add(index)
                        yield ProviderEvent(
                            ProviderEventType.TOOL_CALL_STARTED,
                            tool_call_id=call_id,
                            tool_name=str(function.get("name", "")),
                            output_index=index,
                        )
                    fragment = str(function.get("arguments", ""))
                    if fragment:
                        yield ProviderEvent(
                            ProviderEventType.TOOL_CALL_DELTA,
                            tool_call_id=call_id,
                            tool_name=str(function.get("name", "")),
                            arguments_delta=fragment,
                            output_index=index,
                        )
        yield ProviderEvent(
            ProviderEventType.RESPONSE_COMPLETED, response_id=response_id,
        )


class OpenAIResponsesAdapter(ProviderAdapter):
    protocol = ProviderProtocol.OPENAI_RESPONSES

    def endpoint(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/responses"

    def headers(self, api_key: str) -> dict:
        return {"Authorization": f"Bearer {api_key}"}

    def build_body(self, request: ProviderRequest, model_id: str) -> dict:
        instructions: list[str] = []
        inputs: list[dict] = []
        for message in request.messages:
            role = str(message.get("role", "user"))
            state = message.get("provider_state", {}) or {}
            if role == "system":
                instructions.append(str(message.get("content", "")))
                continue
            output_items = state.get("response_output_items")
            if role == "assistant" and isinstance(output_items, list):
                inputs.extend(output_items)
                continue
            if role == "tool" and message.get("tool_call_id"):
                inputs.append({
                    "type": "function_call_output",
                    "call_id": str(message["tool_call_id"]),
                    "output": str(message.get("content", "")),
                })
                continue
            content = str(message.get("content", ""))
            if content:
                inputs.append({
                    "role": "assistant" if role == "assistant" else "user",
                    "content": content,
                })
            if role == "assistant":
                for call in state.get("tool_calls", message.get("tool_calls", [])) or []:
                    function = call.get("function", call)
                    inputs.append({
                        "type": "function_call",
                        "call_id": str(call.get("id", "")),
                        "name": str(function.get("name", "")),
                        "arguments": str(function.get("arguments", "{}")),
                    })
        body = {
            "model": model_id,
            "input": inputs,
            "max_output_tokens": request.max_output_tokens,
            "stream": request.stream,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if instructions:
            body["instructions"] = "\n\n".join(instructions)
        if request.tools:
            canonical = _canonical_tools(request.tools)
            hosted_search = bool(request.metadata.get("hosted_web_search"))
            body["tools"] = [
                {
                    "type": "function", **item,
                    "strict": item["parameters"].get("additionalProperties") is False,
                }
                for item in canonical
                if not (hosted_search and item.get("name") == "web_search")
            ]
            if hosted_search:
                body["tools"].append({"type": "web_search"})
            body["tool_choice"] = "auto"
        intent = request.cache_intent
        if intent.mode != "unsupported" and intent.stable_prefix_key:
            body["prompt_cache_key"] = intent.stable_prefix_key[:64]
        return body

    def parse_response(self, data: dict) -> list[ProviderEvent]:
        status = str(data.get("status", ""))
        if status in {"failed", "incomplete"}:
            details = data.get("error") or data.get("incomplete_details")
            raise RuntimeError(
                f"Responses API terminated with response.{status}: "
                f"{json.dumps(details, ensure_ascii=False)}"
            )
        events = [ProviderEvent(
            ProviderEventType.RESPONSE_STARTED,
            response_id=str(data.get("id", "")), raw=data,
        )]
        for index, item in enumerate(data.get("output", []) or []):
            events.extend(self._output_item_events(item, index))
        if data.get("usage"):
            events.append(ProviderEvent(
                ProviderEventType.USAGE, usage=normalize_usage(data["usage"]),
            ))
        events.append(ProviderEvent(
            ProviderEventType.RESPONSE_COMPLETED,
            response_id=str(data.get("id", "")), raw=data,
        ))
        return events

    def iter_stream(self, events: Iterable[tuple[str, dict]]) -> Iterator[ProviderEvent]:
        response_id = ""
        for event_name, data in events:
            event_type = str(data.get("type") or event_name)
            response = data.get("response", {}) or {}
            response_id = response_id or str(response.get("id", ""))
            if event_type == "response.created":
                yield ProviderEvent(
                    ProviderEventType.RESPONSE_STARTED,
                    response_id=str(response.get("id", "")), raw=data,
                )
            elif event_type == "response.output_text.delta":
                yield ProviderEvent(
                    ProviderEventType.TEXT_DELTA,
                    text=str(data.get("delta", "")),
                    output_index=int(data.get("output_index", -1)),
                )
            elif event_type in {
                "response.reasoning_summary_text.delta",
                "response.reasoning_text.delta",
            }:
                yield ProviderEvent(
                    ProviderEventType.REASONING_DELTA,
                    reasoning=str(data.get("delta", "")), raw=data,
                )
            elif event_type == "response.function_call_arguments.delta":
                yield ProviderEvent(
                    ProviderEventType.TOOL_CALL_DELTA,
                    tool_call_id=str(data.get("call_id", data.get("item_id", ""))),
                    arguments_delta=str(data.get("delta", "")),
                    output_index=int(data.get("output_index", -1)), raw=data,
                )
            elif event_type == "response.output_item.added":
                item = data.get("item", {}) or {}
                if item.get("type") == "function_call":
                    yield ProviderEvent(
                        ProviderEventType.TOOL_CALL_STARTED,
                        tool_call_id=str(item.get("call_id", "")),
                        tool_name=str(item.get("name", "")),
                        output_index=int(data.get("output_index", -1)), raw=item,
                    )
                elif item.get("type") == "web_search_call":
                    yield ProviderEvent(
                        ProviderEventType.SERVER_TOOL_STARTED,
                        tool_call_id=str(item.get("id", "")),
                        tool_name="web_search",
                        output_index=int(data.get("output_index", -1)), raw=item,
                    )
            elif event_type == "response.output_item.done":
                item = data.get("item", {}) or {}
                item_status = str(item.get("status", "")).strip().lower()
                # Responses may emit output_item.done for an item whose payload
                # is nevertheless incomplete (for example when
                # max_output_tokens cuts a long function argument in half).
                # "done" describes the SSE item lifecycle, not executability.
                # Missing status remains accepted for compatible endpoints that
                # omit it on otherwise completed items.
                if (
                    item.get("type") == "function_call"
                    and item_status not in {"incomplete", "failed", "in_progress"}
                ):
                    yield ProviderEvent(
                        ProviderEventType.TOOL_CALL_DONE,
                        tool_call_id=str(item.get("call_id", "")),
                        tool_name=str(item.get("name", "")),
                        arguments=str(item.get("arguments", "{}")),
                        output_index=int(data.get("output_index", -1)), raw=item,
                    )
                if item.get("type") == "web_search_call":
                    yield ProviderEvent(
                        ProviderEventType.SERVER_TOOL_RESULT,
                        tool_call_id=str(item.get("id", "")),
                        tool_name="web_search",
                        output_index=int(data.get("output_index", -1)), raw=item,
                    )
                yield ProviderEvent(
                    ProviderEventType.REASONING_ARTIFACT,
                    artifact={"response_output_item": item}, raw=item,
                )
            elif event_type == "response.completed":
                if response.get("usage"):
                    yield ProviderEvent(
                        ProviderEventType.USAGE,
                        usage=normalize_usage(response["usage"]),
                    )
                yield ProviderEvent(
                    ProviderEventType.RESPONSE_COMPLETED,
                    response_id=str(response.get("id", response_id)), raw=response,
                )
            elif event_type == "response.incomplete":
                if response.get("usage"):
                    yield ProviderEvent(
                        ProviderEventType.USAGE,
                        usage=normalize_usage(response["usage"]),
                    )
                details = response.get("incomplete_details") or {}
                yield ProviderEvent(
                    ProviderEventType.RESPONSE_INCOMPLETE,
                    response_id=str(response.get("id", response_id)),
                    artifact={
                        "reason": str(details.get("reason", "unknown")),
                        "response_output_items": list(response.get("output", []) or []),
                    },
                    raw=response,
                )
            elif event_type == "response.failed":
                raise RuntimeError(
                    f"Responses API terminated with {event_type}: "
                    f"{json.dumps(response.get('error') or response.get('incomplete_details'))}"
                )

    def _output_item_events(self, item: dict, index: int) -> list[ProviderEvent]:
        result: list[ProviderEvent] = []
        if item.get("type") == "message":
            for part in item.get("content", []) or []:
                if part.get("type") == "output_text" and part.get("text"):
                    result.append(ProviderEvent(
                        ProviderEventType.TEXT_DELTA, text=str(part["text"]),
                        output_index=index,
                    ))
        elif item.get("type") == "function_call":
            result.append(ProviderEvent(
                ProviderEventType.TOOL_CALL_DONE,
                tool_call_id=str(item.get("call_id", "")),
                tool_name=str(item.get("name", "")),
                arguments=str(item.get("arguments", "{}")), output_index=index,
            ))
        elif item.get("type") == "web_search_call":
            result.extend([
                ProviderEvent(
                    ProviderEventType.SERVER_TOOL_STARTED,
                    tool_call_id=str(item.get("id", "")), tool_name="web_search",
                    output_index=index, raw=item,
                ),
                ProviderEvent(
                    ProviderEventType.SERVER_TOOL_RESULT,
                    tool_call_id=str(item.get("id", "")), tool_name="web_search",
                    output_index=index, raw=item,
                ),
            ])
        if item.get("type") in {"reasoning", "function_call", "message"}:
            result.append(ProviderEvent(
                ProviderEventType.REASONING_ARTIFACT,
                artifact={"response_output_item": item}, output_index=index,
            ))
        return result


class AnthropicMessagesAdapter(ProviderAdapter):
    protocol = ProviderProtocol.ANTHROPIC_MESSAGES

    def endpoint(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/messages"

    def headers(self, api_key: str) -> dict:
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}

    def build_body(self, request: ProviderRequest, model_id: str) -> dict:
        system_blocks = []
        messages: list[dict] = []
        for raw in request.messages:
            role = str(raw.get("role", "user"))
            state = raw.get("provider_state", {}) or {}
            if role == "system":
                block = {"type": "text", "text": str(raw.get("content", ""))}
                if not system_blocks and request.cache_intent.mode == "explicit":
                    block["cache_control"] = {"type": "ephemeral"}
                system_blocks.append(block)
                continue
            if role == "tool" and raw.get("tool_call_id"):
                content = [{
                    "type": "tool_result",
                    "tool_use_id": str(raw["tool_call_id"]),
                    "content": str(raw.get("content", "")),
                    "is_error": bool(raw.get("is_error", False)),
                }]
                self._append_message(messages, "user", content)
                continue
            target_role = "assistant" if role == "assistant" else "user"
            blocks = state.get("anthropic_content_blocks")
            if target_role == "assistant" and isinstance(blocks, list):
                self._append_message(messages, target_role, blocks)
                continue
            content: list[dict] = []
            if raw.get("content"):
                content.append({"type": "text", "text": str(raw["content"])})
            if target_role == "assistant":
                for call in state.get("tool_calls", raw.get("tool_calls", [])) or []:
                    function = call.get("function", call)
                    arguments = function.get("arguments", "{}")
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {"raw": arguments}
                    content.append({
                        "type": "tool_use",
                        "id": str(call.get("id", "")),
                        "name": str(function.get("name", "")),
                        "input": arguments,
                    })
            if content:
                self._append_message(messages, target_role, content)
        body = {
            "model": model_id,
            "max_tokens": request.max_output_tokens,
            "stream": request.stream,
            "system": system_blocks,
            "messages": messages,
        }
        if request.tools:
            canonical = _canonical_tools(request.tools)
            hosted_search = bool(request.metadata.get("hosted_web_search"))
            body["tools"] = [{
                "name": item["name"],
                "description": item["description"],
                "input_schema": item["parameters"],
            } for item in canonical if not (
                hosted_search and item.get("name") == "web_search"
            )]
            if hosted_search:
                body["tools"].append({
                    "type": "web_search_20250305", "name": "web_search",
                    "max_uses": 5,
                })
            body["tool_choice"] = {"type": "auto"}
        if request.cache_intent.mode == "automatic":
            body["cache_control"] = {"type": "ephemeral"}
        return body

    @staticmethod
    def _append_message(messages: list[dict], role: str, content: list[dict]) -> None:
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(content)
        else:
            messages.append({"role": role, "content": content})

    def parse_response(self, data: dict) -> list[ProviderEvent]:
        events = [ProviderEvent(
            ProviderEventType.RESPONSE_STARTED,
            response_id=str(data.get("id", "")), raw=data,
        )]
        for index, block in enumerate(data.get("content", []) or []):
            events.extend(self._block_events(block, index))
        if data.get("usage"):
            events.append(ProviderEvent(
                ProviderEventType.USAGE, usage=normalize_usage(data["usage"]),
            ))
        events.append(ProviderEvent(
            ProviderEventType.RESPONSE_COMPLETED,
            response_id=str(data.get("id", "")), raw=data,
        ))
        return events

    def iter_stream(self, events: Iterable[tuple[str, dict]]) -> Iterator[ProviderEvent]:
        blocks: dict[int, dict] = {}
        response_id = ""
        for event_name, data in events:
            event_type = str(data.get("type") or event_name)
            if event_type == "message_start":
                message = data.get("message", {}) or {}
                response_id = str(message.get("id", ""))
                yield ProviderEvent(
                    ProviderEventType.RESPONSE_STARTED,
                    response_id=response_id, raw=message,
                )
                if message.get("usage"):
                    yield ProviderEvent(
                        ProviderEventType.USAGE,
                        usage=normalize_usage(message["usage"]),
                    )
            elif event_type == "content_block_start":
                index = int(data.get("index", 0))
                block = dict(data.get("content_block", {}) or {})
                blocks[index] = block
                if block.get("type") == "tool_use":
                    yield ProviderEvent(
                        ProviderEventType.TOOL_CALL_STARTED,
                        tool_call_id=str(block.get("id", "")),
                        tool_name=str(block.get("name", "")), output_index=index,
                    )
                elif block.get("type") == "server_tool_use":
                    yield ProviderEvent(
                        ProviderEventType.SERVER_TOOL_STARTED,
                        tool_call_id=str(block.get("id", "")),
                        tool_name=str(block.get("name", "web_search")),
                        output_index=index, raw=block,
                    )
                elif block.get("type") == "web_search_tool_result":
                    yield ProviderEvent(
                        ProviderEventType.SERVER_TOOL_RESULT,
                        tool_call_id=str(block.get("tool_use_id", "")),
                        tool_name="web_search", output_index=index, raw=block,
                    )
            elif event_type == "content_block_delta":
                index = int(data.get("index", 0))
                delta = data.get("delta", {}) or {}
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    text = str(delta.get("text", ""))
                    blocks.setdefault(index, {"type": "text", "text": ""})
                    blocks[index]["text"] = str(blocks[index].get("text", "")) + text
                    yield ProviderEvent(ProviderEventType.TEXT_DELTA, text=text)
                elif delta_type == "thinking_delta":
                    thinking = str(delta.get("thinking", ""))
                    blocks.setdefault(index, {"type": "thinking", "thinking": ""})
                    blocks[index]["thinking"] = (
                        str(blocks[index].get("thinking", "")) + thinking
                    )
                    yield ProviderEvent(
                        ProviderEventType.REASONING_DELTA, reasoning=thinking,
                    )
                elif delta_type == "signature_delta":
                    blocks.setdefault(index, {"type": "thinking"})["signature"] = (
                        str(blocks[index].get("signature", ""))
                        + str(delta.get("signature", ""))
                    )
                elif delta_type == "input_json_delta":
                    fragment = str(delta.get("partial_json", ""))
                    block = blocks.setdefault(index, {"type": "tool_use"})
                    block["partial_json"] = str(block.get("partial_json", "")) + fragment
                    yield ProviderEvent(
                        ProviderEventType.TOOL_CALL_DELTA,
                        tool_call_id=str(block.get("id", "")),
                        tool_name=str(block.get("name", "")),
                        arguments_delta=fragment, output_index=index,
                    )
            elif event_type == "content_block_stop":
                index = int(data.get("index", 0))
                block = blocks.get(index, {})
                if block.get("type") == "tool_use":
                    arguments = str(block.pop("partial_json", "{}") or "{}")
                    yield ProviderEvent(
                        ProviderEventType.TOOL_CALL_DONE,
                        tool_call_id=str(block.get("id", "")),
                        tool_name=str(block.get("name", "")),
                        arguments=arguments, output_index=index,
                    )
                yield ProviderEvent(
                    ProviderEventType.REASONING_ARTIFACT,
                    artifact={"anthropic_content_block": block}, output_index=index,
                )
            elif event_type == "message_delta":
                if data.get("usage"):
                    yield ProviderEvent(
                        ProviderEventType.USAGE,
                        usage=normalize_usage(data["usage"]),
                    )
            elif event_type == "message_stop":
                yield ProviderEvent(
                    ProviderEventType.RESPONSE_COMPLETED,
                    response_id=response_id, raw=data,
                )
            elif event_type == "error":
                raise RuntimeError(f"Anthropic stream error: {data.get('error')}")

    @staticmethod
    def _block_events(block: dict, index: int) -> list[ProviderEvent]:
        kind = block.get("type")
        result: list[ProviderEvent] = []
        if kind == "text" and block.get("text"):
            result.append(ProviderEvent(
                ProviderEventType.TEXT_DELTA, text=str(block["text"]),
                output_index=index,
            ))
        elif kind == "thinking" and block.get("thinking"):
            result.append(ProviderEvent(
                ProviderEventType.REASONING_DELTA,
                reasoning=str(block["thinking"]), output_index=index,
            ))
        elif kind == "tool_use":
            result.append(ProviderEvent(
                ProviderEventType.TOOL_CALL_DONE,
                tool_call_id=str(block.get("id", "")),
                tool_name=str(block.get("name", "")),
                arguments=json.dumps(block.get("input", {}), ensure_ascii=False),
                output_index=index,
            ))
        elif kind == "server_tool_use":
            result.append(ProviderEvent(
                ProviderEventType.SERVER_TOOL_STARTED,
                tool_call_id=str(block.get("id", "")),
                tool_name=str(block.get("name", "web_search")),
                output_index=index, raw=block,
            ))
        elif kind == "web_search_tool_result":
            result.append(ProviderEvent(
                ProviderEventType.SERVER_TOOL_RESULT,
                tool_call_id=str(block.get("tool_use_id", "")),
                tool_name="web_search", output_index=index, raw=block,
            ))
        result.append(ProviderEvent(
            ProviderEventType.REASONING_ARTIFACT,
            artifact={"anthropic_content_block": block}, output_index=index,
        ))
        return result


def get_adapter(protocol: str | ProviderProtocol) -> ProviderAdapter:
    resolved = ProviderProtocol(protocol)
    adapters = {
        ProviderProtocol.OPENAI_CHAT: OpenAIChatAdapter,
        ProviderProtocol.OPENAI_RESPONSES: OpenAIResponsesAdapter,
        ProviderProtocol.ANTHROPIC_MESSAGES: AnthropicMessagesAdapter,
    }
    if resolved == ProviderProtocol.AUTO:
        raise ValueError("AUTO protocol must be resolved by ProviderProbe before runtime")
    return adapters[resolved]()
