"""Provider-neutral LLM facade with three wire protocol adapters.

零新依赖——使用标准库 urllib。用于 daemon llm_call 命令。

v0.38: 支持 tools 参数（function calling）。传 tools 时返回完整 message dict
（含 tool_calls），不传时返回纯文本字符串（向后兼容）。
v0.45: 分类重试引擎 —— 5xx/网络错误退避重试，429 跟 Retry-After，
       400/401/402/403 不重试，context overflow 降 token 重试。
"""

from __future__ import annotations

import json
import random
import socket
import time
import urllib.error
import threading
from dataclasses import replace
from datetime import datetime
from typing import Generator

from backend.core.loop.provider_adapters import get_adapter
from backend.core.loop.provider_protocol import (
    CacheIntent,
    ProviderCapabilities,
    ProviderEvent,
    ProviderEventType,
    ProviderProtocol,
    ProviderRequest,
    deterministic_hash,
)
from backend.core.loop.provider_transport import (
    HttpProviderTransport,
    ProviderHttpError,
    ProviderTransportCancelled,
)


class StreamInterruptedError(RuntimeError):
    """LLM 流式传输中断。"""
    def __init__(self, partial_text: str = "",
                 partial_tool_calls: dict[int, dict] | None = None,
                 *, retryable: bool = True, status_code: int | None = None,
                 error_body: str = ""):
        super().__init__("LLM stream interrupted")
        self.partial_text = partial_text
        self.partial_tool_calls = partial_tool_calls or {}
        self.retryable = retryable
        self.status_code = status_code
        self.error_body = error_body

    @property
    def is_context_overflow(self) -> bool:
        message = self.error_body.casefold()
        return self.status_code in {400, 413, 422} and (
            "context" in message and any(token in message for token in (
                "overflow", "length", "too long", "maximum", "limit", "token",
            ))
        )


class StreamCancelledError(RuntimeError):
    """The caller cancelled an in-flight provider stream."""


class LLMProvider:
    """Compatibility facade over canonical provider events.

    不绑定特定模型——model_id + base_url + api_key 全部由 config 注入。
    v0.45: 内置分类重试引擎。
    """

    def __init__(
        self, base_url: str, api_key: str, model_id: str,
        protocol: str = ProviderProtocol.OPENAI_CHAT.value,
        capabilities: dict | ProviderCapabilities | None = None,
        transport: HttpProviderTransport | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._model = model_id
        self.protocol = ProviderProtocol(protocol)
        if self.protocol == ProviderProtocol.AUTO:
            raise ValueError("AUTO provider protocol must be probed before runtime")
        self.adapter = get_adapter(self.protocol)
        self.capabilities = (
            capabilities
            if isinstance(capabilities, ProviderCapabilities)
            else ProviderCapabilities.from_dict(
                capabilities, protocol=self.protocol.value,
            )
        )
        self.transport = transport or HttpProviderTransport()

    @property
    def context_window(self) -> int:
        return max(1024, int(self.capabilities.context_window))

    @property
    def max_output_tokens(self) -> int:
        return max(1, int(self.capabilities.max_output_tokens))

    @property
    def route_key(self) -> str:
        """Non-secret identity for provider-native continuation state."""
        return deterministic_hash({
            "protocol": self.protocol.value,
            "base_url": self._base.casefold(),
            "model": self._model,
        })

    # ── Public API ──────────────────────────────────────────

    def chat(self, messages: list[dict], max_tokens: int = 4096,
             timeout: int = 120,
             tools: list[dict] | None = None,
             preserve_provider_state: bool = False,
             cancel_event: threading.Event | None = None,
             max_retries: int = 5,
             base_delay: float = 1.0,
             max_backoff: float = 10.0) -> str | dict:
        """同步 chat completion（带分类重试）。

        Retry strategy:
        - 5xx / connection / timeout → exponential backoff + jitter, max=5
        - 429 → follow Retry-After header, max 30s wait
        - 400/401/402/403 → no retry
        - Context overflow → return to the Host compaction/decision state machine
        """
        from backend.core.loop.error_taxonomy import (
            Retryability, classify_http_error, classify_network_error,
            classify_timeout_error, classify_context_overflow,
        )

        current_max_tokens = max_tokens
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise StreamCancelledError()
            try:
                return self._chat_once(
                    messages, current_max_tokens, timeout, tools,
                    preserve_provider_state=preserve_provider_state,
                    cancel_event=cancel_event,
                )
            except StreamCancelledError:
                raise
            except RuntimeError as e:
                classified = self._classify_chat_error(e)

                # Replaying oversized input with a smaller output allowance is
                # not compaction. Do not hide extra paid retries from the Host.
                if classified.code == "CONTEXT_OVERFLOW":
                    raise

                # Non-retryable: fail immediately
                if classified.retryability == Retryability.NON_RETRYABLE:
                    raise

                # Rate limited: follow server instruction
                if classified.code == "RATE_LIMITED":
                    retry_after = self._parse_retry_after(e)
                    wait = min(retry_after or 5, 30)
                    if cancel_event is not None and cancel_event.wait(wait):
                        raise StreamCancelledError()
                    if cancel_event is None:
                        time.sleep(wait)
                    continue

                # Last attempt: give up
                if attempt >= max_retries:
                    raise RuntimeError(
                        f"LLM API failed after {max_retries} retries: {e}"
                    ) from e

                # Exponential backoff + jitter
                delay = min(base_delay * (2 ** attempt), max_backoff)
                delay += random.uniform(0, delay * 0.25)
                if cancel_event is not None and cancel_event.wait(delay):
                    raise StreamCancelledError()
                if cancel_event is None:
                    time.sleep(delay)
                last_error = e

        raise last_error or RuntimeError("LLM API retry exhausted")

    def stream_chat(self, messages: list[dict], max_tokens: int = 4096,
                    timeout: int = 120,
                    tools: list[dict] | None = None,
                    cancel_event: threading.Event | None = None,
                    ) -> Generator[dict, None, None]:
        """流式 chat completion。yield 原始 SSE chunk dict。

        Stream retry is lighter than sync: connection errors get 1 retry;
        HTTP errors (4xx) are not retried since partial content may have
        already been emitted.

        Raises:
            StreamInterruptedError: 网络中断或 HTTP 错误（含重试后）
        """
        if self.protocol != ProviderProtocol.OPENAI_CHAT:
            for event in self.stream_events(
                messages, max_tokens=max_tokens, timeout=timeout,
                tools=tools, cancel_event=cancel_event,
            ):
                chunk = self._event_to_chat_chunk(event)
                if chunk is not None:
                    yield chunk
            return

        body: dict = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        emitted = False
        for attempt in range(2):  # 1 retry before the first emitted chunk only
            if cancel_event is not None and cancel_event.is_set():
                raise StreamCancelledError()
            try:
                for chunk in self._stream_once(body, timeout, cancel_event=cancel_event):
                    emitted = True
                    yield chunk
                return
            except StreamInterruptedError as exc:
                # Replaying after output was observed duplicates text and tool
                # calls.  Recovery above the provider boundary must receive the
                # partial transcript and decide how to continue.
                if not emitted and exc.retryable and attempt == 0:
                    continue  # retry once
                raise

    def stream_events(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 4096,
        timeout: int = 120,
        tools: list[dict] | None = None,
        cancel_event: threading.Event | None = None,
        cache_intent: CacheIntent | None = None,
        metadata: dict | None = None,
    ) -> Generator[ProviderEvent, None, None]:
        """Yield protocol-neutral events; never replay after the first event."""
        request = ProviderRequest(
            messages=tuple(messages), tools=tuple(tools or []),
            max_output_tokens=min(max_tokens, self.max_output_tokens),
            stream=True,
            cache_intent=cache_intent or CacheIntent(
                mode=self.capabilities.prompt_cache,
            ),
            metadata=dict(metadata or {}),
        )
        body = self.adapter.build_body(request, self._model)
        emitted = False
        hosted_fallback_used = False
        for attempt in range(2):
            if cancel_event is not None and cancel_event.is_set():
                raise StreamCancelledError()
            try:
                wire_events = self.transport.stream_sse(
                    self.adapter.endpoint(self._base), body,
                    self.adapter.headers(self._key), timeout=timeout,
                    cancel_event=cancel_event,
                )
                for event in self.adapter.iter_stream(wire_events):
                    emitted = True
                    yield event
                return
            except ProviderTransportCancelled as exc:
                raise StreamCancelledError() from exc
            except ProviderHttpError as exc:
                if (
                    not emitted
                    and not hosted_fallback_used
                    and request.metadata.get("hosted_web_search")
                    and request.metadata.get("web_search_mode") == "auto"
                    and 400 <= exc.status_code < 500
                ):
                    # A compatible Responses/Messages endpoint may support
                    # function tools but not the vendor-hosted web tool. Auto
                    # mode retries once with Gitgo's SearXNG function schema.
                    self.capabilities = replace(
                        self.capabilities,
                        hosted_web_search=False,
                        probe_version=max(2, self.capabilities.probe_version),
                        probed_at=self.capabilities.probed_at or datetime.now().isoformat(),
                    )
                    yield ProviderEvent(
                        ProviderEventType.PROVIDER_CAPABILITY_FALLBACK,
                        tool_name="web_search",
                        artifact={
                            "capability": "hosted_web_search",
                            "from": "provider",
                            "to": "searxng",
                            "status_code": exc.status_code,
                            "reason": "provider_rejected_hosted_tool",
                        },
                    )
                    request.metadata["hosted_web_search"] = False
                    hosted_fallback_used = True
                    body = self.adapter.build_body(request, self._model)
                    continue
                retryable = exc.status_code == 429 or exc.status_code >= 500
                if not emitted and retryable and attempt == 0:
                    continue
                raise StreamInterruptedError(
                    retryable=retryable, status_code=exc.status_code,
                    error_body=exc.body,
                ) from exc
            except StreamCancelledError:
                raise
            except Exception as exc:
                if not emitted and attempt == 0:
                    continue
                raise StreamInterruptedError() from exc

    # ── Internal: single-call primitives ─────────────────────

    def _chat_once(self, messages: list[dict], max_tokens: int,
                   timeout: int, tools: list[dict] | None,
                   *, preserve_provider_state: bool = False,
                   cancel_event: threading.Event | None = None) -> str | dict:
        """Single chat call, no retry logic."""
        request = ProviderRequest(
            messages=tuple(messages), tools=tuple(tools or []),
            max_output_tokens=min(max_tokens, self.max_output_tokens),
            stream=False,
            cache_intent=CacheIntent(mode=self.capabilities.prompt_cache),
        )
        body = self.adapter.build_body(request, self._model)
        try:
            data = self.transport.post_json(
                self.adapter.endpoint(self._base), body,
                self.adapter.headers(self._key), timeout=timeout,
                cancel_event=cancel_event,
            )
        except ProviderTransportCancelled as exc:
            raise StreamCancelledError() from exc
        except ProviderHttpError as exc:
            wrapped = RuntimeError(str(exc))
            wrapped.__cause__ = exc
            raise wrapped

        response = self._accumulate_events(self.adapter.parse_response(data))
        if tools or preserve_provider_state:
            return response
        return response["content"]

    def _stream_once(
        self, body: dict, timeout: int,
        cancel_event: threading.Event | None = None,
    ) -> Generator[dict, None, None]:
        """Single streaming call, no retry logic."""
        try:
            for _event_name, payload in self.transport.stream_sse(
                self.adapter.endpoint(self._base), body,
                self.adapter.headers(self._key), timeout=timeout,
                cancel_event=cancel_event,
            ):
                yield payload
        except ProviderHttpError as e:
            retryable = e.status_code == 429 or e.status_code >= 500
            raise StreamInterruptedError(
                retryable=retryable, status_code=e.status_code,
                error_body=e.body,
            ) from e
        except ProviderTransportCancelled as e:
            raise StreamCancelledError() from e
        except (socket.timeout, OSError, ValueError, RuntimeError) as e:
            raise StreamInterruptedError() from e

    @staticmethod
    def _accumulate_events(events) -> dict:
        content: list[str] = []
        reasoning: list[str] = []
        tool_calls: list[dict] = []
        artifacts: list[dict] = []
        usage = None
        response_id = ""
        for event in events:
            response_id = event.response_id or response_id
            if event.type == ProviderEventType.TEXT_DELTA:
                content.append(event.text)
            elif event.type == ProviderEventType.REASONING_DELTA:
                reasoning.append(event.reasoning)
            elif event.type == ProviderEventType.TOOL_CALL_DONE:
                tool_calls.append({
                    "id": event.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": event.tool_name,
                        "arguments": event.arguments or "{}",
                    },
                })
            elif event.type == ProviderEventType.REASONING_ARTIFACT and event.artifact:
                artifacts.append(event.artifact)
            elif event.type == ProviderEventType.USAGE and event.usage:
                usage = event.usage.to_dict()
        response = {
            "content": "".join(content),
            "tool_calls": tool_calls,
            "response_id": response_id,
        }
        if reasoning:
            response["reasoning_content"] = "".join(reasoning)
        if artifacts:
            response["provider_artifacts"] = artifacts
        if usage is not None:
            response["usage"] = usage
        return response

    @staticmethod
    def _event_to_chat_chunk(event: ProviderEvent) -> dict | None:
        delta: dict = {}
        if event.type == ProviderEventType.TEXT_DELTA:
            delta["content"] = event.text
        elif event.type == ProviderEventType.REASONING_DELTA:
            delta["reasoning_content"] = event.reasoning
        elif event.type == ProviderEventType.REASONING_ARTIFACT:
            delta["reasoning_details"] = [event.artifact]
        elif event.type in {
            ProviderEventType.TOOL_CALL_STARTED,
            ProviderEventType.TOOL_CALL_DELTA,
            ProviderEventType.TOOL_CALL_DONE,
        }:
            delta["tool_calls"] = [{
                "index": max(0, event.output_index),
                "id": event.tool_call_id,
                "function": {
                    "name": event.tool_name,
                    "arguments": event.arguments_delta or event.arguments,
                },
            }]
        elif event.type == ProviderEventType.USAGE and event.usage:
            return {"choices": [], "usage": event.usage.to_dict()}
        else:
            return None
        return {"choices": [{"delta": delta}]}

    @staticmethod
    def _close_response_on_cancel(resp, cancel_event: threading.Event | None) -> threading.Event:
        """Close the live socket from a watcher so a blocked read is interrupted."""
        done = threading.Event()
        if cancel_event is None:
            return done

        def watch() -> None:
            while not done.is_set():
                if cancel_event.wait(0.1):
                    try:
                        resp.close()
                    except Exception:
                        pass
                    return

        threading.Thread(target=watch, daemon=True).start()
        return done

    # ── Error classification ─────────────────────────────────

    def _classify_chat_error(self, exc: Exception):
        """Classify a RuntimeError from _chat_once into ClassifiedError."""
        from backend.core.loop.error_taxonomy import (
            classify_http_error, classify_network_error,
            classify_timeout_error, classify_context_overflow,
        )

        msg = str(exc)

        if "context" in msg.lower() and ("overflow" in msg.lower() or
                                          "length" in msg.lower() or
                                          "too long" in msg.lower()):
            return classify_context_overflow(msg, original=exc)

        if "timed out" in msg.lower() or "timeout" in msg.lower():
            return classify_timeout_error(msg, original=exc)

        if "connection" in msg.lower() or "resolve" in msg.lower():
            return classify_network_error(msg, original=exc)

        # Try to extract HTTP status code from message
        import re
        code_match = re.search(r'\b(\d{3})\b', msg)
        if code_match:
            return classify_http_error(int(code_match.group(1)), msg, original=exc)

        # Default: treat unknown errors as network errors (retryable)
        return classify_network_error(msg, original=exc)

    @staticmethod
    def _parse_retry_after(exc: Exception) -> int | None:
        """Parse Retry-After header from HTTPError if available."""
        if isinstance(exc, RuntimeError):
            cause = exc.__cause__
            if isinstance(cause, urllib.error.HTTPError):
                val = cause.headers.get("Retry-After", "")
                if val and val.isdigit():
                    return int(val)
            if isinstance(cause, ProviderHttpError):
                val = str(cause.headers.get("Retry-After", ""))
                if val.isdigit():
                    return int(val)
        return None
