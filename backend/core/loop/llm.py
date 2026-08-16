"""LLMProvider —— OpenAI-compatible /chat/completions wrapper.

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
import urllib.request
import urllib.error
from typing import Generator


class StreamInterruptedError(RuntimeError):
    """LLM 流式传输中断。"""
    def __init__(self, partial_text: str = "",
                 partial_tool_calls: dict[int, dict] | None = None):
        super().__init__("LLM stream interrupted")
        self.partial_text = partial_text
        self.partial_tool_calls = partial_tool_calls or {}


class LLMProvider:
    """OpenAI-compatible /chat/completions wrapper.

    不绑定特定模型——model_id + base_url + api_key 全部由 config 注入。
    v0.45: 内置分类重试引擎。
    """

    def __init__(self, base_url: str, api_key: str, model_id: str):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._model = model_id

    # ── Public API ──────────────────────────────────────────

    def chat(self, messages: list[dict], max_tokens: int = 4096,
             timeout: int = 120,
             tools: list[dict] | None = None,
             max_retries: int = 5,
             base_delay: float = 1.0,
             max_backoff: float = 10.0) -> str | dict:
        """同步 chat completion（带分类重试）。

        Retry strategy:
        - 5xx / connection / timeout → exponential backoff + jitter, max=5
        - 429 → follow Retry-After header, max 30s wait
        - 400/401/402/403 → no retry
        - Context overflow → halve max_tokens, retry once
        """
        from backend.core.loop.error_taxonomy import (
            Retryability, classify_http_error, classify_network_error,
            classify_timeout_error, classify_context_overflow,
        )

        current_max_tokens = max_tokens
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                return self._chat_once(messages, current_max_tokens, timeout, tools)
            except RuntimeError as e:
                classified = self._classify_chat_error(e)

                # Context overflow: halve tokens, retry once
                if classified.code == "CONTEXT_OVERFLOW" and current_max_tokens > 1024:
                    current_max_tokens = max(current_max_tokens // 2, 1024)
                    continue

                # Non-retryable: fail immediately
                if classified.retryability == Retryability.NON_RETRYABLE:
                    raise

                # Rate limited: follow server instruction
                if classified.code == "RATE_LIMITED":
                    retry_after = self._parse_retry_after(e)
                    wait = min(retry_after or 5, 30)
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
                time.sleep(delay)
                last_error = e

        raise last_error or RuntimeError("LLM API retry exhausted")

    def stream_chat(self, messages: list[dict], max_tokens: int = 4096,
                    timeout: int = 120,
                    tools: list[dict] | None = None) -> Generator[dict, None, None]:
        """流式 chat completion。yield 原始 SSE chunk dict。

        Stream retry is lighter than sync: connection errors get 1 retry;
        HTTP errors (4xx) are not retried since partial content may have
        already been emitted.

        Raises:
            StreamInterruptedError: 网络中断或 HTTP 错误（含重试后）
        """
        body: dict = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        for attempt in range(2):  # 1 retry for transient network errors
            try:
                yield from self._stream_once(body, timeout)
                return
            except StreamInterruptedError:
                if attempt == 0:
                    continue  # retry once
                raise

    # ── Internal: single-call primitives ─────────────────────

    def _chat_once(self, messages: list[dict], max_tokens: int,
                   timeout: int, tools: list[dict] | None) -> str | dict:
        """Single chat call, no retry logic."""
        body: dict = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        req = urllib.request.Request(
            f"{self._base}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"LLM API error {e.code}: {error_body[:500]}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"LLM API connection failed: {e.reason}"
            ) from e

        msg = data["choices"][0]["message"]
        if tools:
            return {
                "content": msg.get("content", "") or "",
                "tool_calls": msg.get("tool_calls", []),
            }
        return msg.get("content", "") or ""

    def _stream_once(self, body: dict, timeout: int) -> Generator[dict, None, None]:
        """Single streaming call, no retry logic."""
        req = urllib.request.Request(
            f"{self._base}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for line in resp:
                    line = line.decode("utf-8").strip()
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload == "[DONE]":
                        break
                    yield json.loads(payload)
        except (urllib.error.URLError, socket.timeout) as e:
            raise StreamInterruptedError() from e
        except urllib.error.HTTPError as e:
            raise StreamInterruptedError() from e

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
        return None
