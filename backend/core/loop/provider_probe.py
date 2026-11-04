"""Bounded explicit probes; declarations and verified behavior stay distinct."""
from __future__ import annotations
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from .llm import LLMProvider, StreamCancelledError
from .provider_protocol import ProviderCapabilities, ProviderEventType, ProviderProtocol
from .provider_transport import ProviderTransportCancelled


def _model_rejected(error, model_id):
    """Recognize explicit model rejection only; HTTP 400/404 alone is ambiguous."""
    if error.status_code not in {400, 404, 422}:
        return False
    try:
        payload = json.loads(error.body[:8192]).get("error", {})
        if not isinstance(payload, dict):
            return False
        if payload.get("code") in {"model_not_found", "invalid_model", "model_not_supported"}:
            return True
        message = str(payload.get("message", "")).strip().casefold()
        return bool(model_id) and message == "unsupported model " + model_id.casefold()
    except (ValueError, AttributeError):
        return False


@dataclass(frozen=True)
class ProviderProbeResult:
    protocol: str
    capabilities: ProviderCapabilities
    text: str
    observations: tuple[str, ...]
    checks: dict = field(default_factory=dict)

    def to_dict(self):
        return {"protocol": self.protocol, "capabilities": self.capabilities.to_dict(),
                "text": self.text, "observations": list(self.observations), "checks": self.checks}


class ProviderProbe:
    def __init__(self, transport=None):
        self.transport = transport

    def probe(self, *, base_url, api_key, model_id, protocol="auto", timeout=30,
              kind="remote", auth_mode="api_key", cancel_event=None, request_preferences=None, connection_only=False):
        from .provider_policy import provider_endpoint
        from ..provider_catalog import safe_opener
        from .provider_transport import HttpProviderTransport
        base_url = provider_endpoint(base_url, kind=kind, auth_mode=auth_mode)
        api_key = api_key if auth_mode == "api_key" else ""
        event = cancel_event or threading.Event()
        duration = max(1, min(float(timeout), 60))
        deadline = time.monotonic() + duration
        timer = threading.Timer(duration, event.set)
        timer.daemon = True
        timer.start()
        requested = ProviderProtocol(protocol)
        try:
            for candidate in self._candidates(base_url, requested):
                if event.is_set():
                    raise ProviderTransportCancelled()
                try:
                    return self._probe_one(base_url, api_key, model_id, candidate, deadline, event,
                                           self.transport or HttpProviderTransport(safe_opener(kind)), request_preferences, connection_only)
                except (ProviderTransportCancelled, StreamCancelledError):
                    raise
                except Exception as exc:
                    from .provider_transport import ProviderHttpError
                    cause = exc
                    while not isinstance(cause, ProviderHttpError) and cause.__cause__ is not None:
                        cause = cause.__cause__
                    if isinstance(cause, ProviderHttpError):
                        if _model_rejected(cause, model_id):
                            raise ValueError("PROVIDER_MODEL_UNAVAILABLE") from None
                        if requested != ProviderProtocol.AUTO or cause.status_code in {401, 402, 403, 429}:
                            raise ValueError(f"PROVIDER_PROBE_HTTP_{cause.status_code}") from None
                    if isinstance(exc, ValueError) and str(exc).startswith("PROVIDER_"):
                        raise
                    if requested != ProviderProtocol.AUTO:
                        raise ValueError("PROVIDER_TEXT_PROBE_FAILED") from None
            raise RuntimeError("PROVIDER_PROTOCOL_NOT_VERIFIED")
        finally:
            timer.cancel()

    def _probe_one(self, base, key, model, protocol, deadline, event, transport, request_preferences=None, connection_only=False):
        def remaining():
            if event.is_set() or time.monotonic() >= deadline:
                raise ProviderTransportCancelled()
            return max(0.1, deadline - time.monotonic())

        provider = LLMProvider(base, key, model, protocol=protocol.value, transport=transport, request_preferences=request_preferences)
        response = provider.chat([{"role": "user", "content": "Reply with exactly: pong"}],
                                 max_tokens=512, timeout=remaining(), preserve_provider_state=True,
                                 cancel_event=event, max_retries=0)
        text = str(response.get("content", "") if isinstance(response, dict) else response)
        if not text.strip() or not connection_only and text.strip().casefold() != "pong":
            raise ValueError("PROVIDER_TEXT_RESPONSE_MISMATCH")
        observations = ["sync_text"]
        checks = {"connection": "verified", "authentication": "verified", "model_response": "verified",
                  "streaming": "unknown", "tools": "unknown", "images": "unknown",
                  "hosted_web_search": "unknown", "reasoning_continuation": "unknown"}
        if connection_only:
            caps = ProviderCapabilities(protocol=protocol.value, streaming=False, tools=False, parallel_tools=False,
                                        probed_at=datetime.now(timezone.utc).isoformat(), probe_version=3)
            return ProviderProbeResult(protocol.value, caps, text, ("sync_connection",), checks)
        tool = {"type": "function", "function": {"name": "gitgo_probe_echo",
                "description": "Echo a value. The local implementation returns a secret receipt.",
                "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                               "required": ["value"], "additionalProperties": False}}}
        messages = [{"role": "user", "content": "Call gitgo_probe_echo exactly once with value='probe'. Then reply with only the receipt returned by the tool."}]
        saw_reasoning = False
        try:
            events = list(provider.stream_events(messages, tools=[tool], max_tokens=512,
                                                timeout=remaining(), cancel_event=event, max_attempts=1))
            completed = any(e.type == ProviderEventType.RESPONSE_COMPLETED for e in events)
            checks["streaming"] = "verified" if completed else "unknown"
            if completed:
                observations.append("typed_stream")
            assistant = provider._accumulate_events(events)
            saw_reasoning = bool(assistant.get("reasoning_content") or assistant.get("provider_artifacts"))
            calls = assistant.get("tool_calls", [])
            if len(calls) == 1 and calls[0].get("id") and calls[0].get("function", {}).get("name") == "gitgo_probe_echo" and json.loads(calls[0]["function"]["arguments"]) == {"value": "probe"}:
                receipt = "echo-" + secrets.token_hex(8)
                assistant.pop("usage", None)
                assistant["role"] = "assistant"
                from .provider_turn_protocol import _provider_continuation
                assistant["provider_state"] = _provider_continuation(assistant.get("reasoning_content", ""), [], assistant.get("provider_artifacts", []))
                continuation = provider.chat(messages + [assistant, {"role": "tool", "tool_call_id": calls[0]["id"], "content": receipt}],
                                             tools=[tool], max_tokens=512, timeout=remaining(),
                                             cancel_event=event, max_retries=0, preserve_provider_state=True)
                if not continuation.get("tool_calls") and continuation.get("content", "").strip() == receipt:
                    checks["tools"] = "verified"
                    observations.append("tool_roundtrip_verified")
                    if saw_reasoning:
                        checks["reasoning_continuation"] = "verified"
            else:
                observations.append("tool_call_not_verified")
        except (ProviderTransportCancelled, StreamCancelledError):
            raise
        except Exception as exc:
            observations.append("tool_roundtrip_unverified:" + type(exc).__name__)
        remaining()
        caps = ProviderCapabilities(protocol=protocol.value, streaming=checks["streaming"] == "verified",
                                    tools=checks["tools"] == "verified", parallel_tools=False,
                                    reasoning_continuation=checks["reasoning_continuation"] == "verified",
                                    prompt_cache="automatic", supports_store=False, hosted_web_search=False,
                                    probed_at=datetime.now(timezone.utc).isoformat(), probe_version=3)
        return ProviderProbeResult(protocol.value, caps, text, tuple(observations), checks)

    @staticmethod
    def _candidates(base_url, requested):
        return [requested] if requested != ProviderProtocol.AUTO else [
            ProviderProtocol.OPENAI_CHAT, ProviderProtocol.OPENAI_RESPONSES, ProviderProtocol.ANTHROPIC_MESSAGES,
        ]
