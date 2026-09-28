"""Explicit provider capability probe; runtime never guesses a protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.core.loop.llm import LLMProvider
from backend.core.loop.provider_protocol import (
    ProviderCapabilities,
    ProviderEventType,
    ProviderProtocol,
)


@dataclass(frozen=True)
class ProviderProbeResult:
    protocol: str
    capabilities: ProviderCapabilities
    text: str
    observations: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "protocol": self.protocol,
            "capabilities": self.capabilities.to_dict(),
            "text": self.text,
            "observations": list(self.observations),
        }


class ProviderProbe:
    """Probe text, typed streaming, tools, continuation artifacts, and usage."""

    def __init__(self, transport=None):
        self.transport = transport

    def probe(
        self, *, base_url: str, api_key: str, model_id: str,
        protocol: str = "auto", timeout: int = 30,
    ) -> ProviderProbeResult:
        requested = ProviderProtocol(protocol)
        candidates = self._candidates(base_url, requested)
        failures = []
        for candidate in candidates:
            try:
                return self._probe_one(
                    base_url, api_key, model_id, candidate, timeout,
                )
            except Exception as exc:
                failures.append(f"{candidate.value}: {exc}")
                if requested != ProviderProtocol.AUTO:
                    break
        raise RuntimeError("Provider probe failed: " + " | ".join(failures))

    def _probe_one(
        self, base_url: str, api_key: str, model_id: str,
        protocol: ProviderProtocol, timeout: int,
    ) -> ProviderProbeResult:
        provider = LLMProvider(
            base_url, api_key, model_id, protocol=protocol.value,
            transport=self.transport,
        )
        response = provider.chat(
            [{"role": "user", "content": "Reply with exactly: pong"}],
            # Thinking models may spend their first tokens entirely on
            # reasoning.  Keep this bounded, but leave enough room for the
            # requested final text so an incomplete response is not mistaken
            # for protocol compatibility.
            max_tokens=512, timeout=timeout, preserve_provider_state=True,
            max_retries=0,
        )
        text = str(response.get("content", "") if isinstance(response, dict) else response)
        if text.strip().casefold() != "pong":
            raise RuntimeError("text probe did not return the required pong response")
        observations = ["sync_text"]
        saw_tool = False
        saw_reasoning = bool(isinstance(response, dict) and (
            response.get("reasoning_content") or response.get("provider_artifacts")
        ))
        saw_usage = bool(isinstance(response, dict) and response.get("usage"))
        tools_accepted = True
        probe_tools = [{
            "type": "function",
            "function": {
                "name": "gitgo_probe_echo",
                "description": "Return the supplied probe value.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        }]
        try:
            for event in provider.stream_events(
                [{
                    "role": "user",
                    "content": (
                        "Call gitgo_probe_echo once with value='probe'. "
                        "Do not answer with prose."
                    ),
                }],
                max_tokens=96, timeout=timeout, tools=probe_tools,
            ):
                if event.type in {
                    ProviderEventType.TOOL_CALL_STARTED,
                    ProviderEventType.TOOL_CALL_DONE,
                }:
                    saw_tool = True
                if event.type in {
                    ProviderEventType.REASONING_DELTA,
                    ProviderEventType.REASONING_ARTIFACT,
                }:
                    saw_reasoning = True
                if event.type == ProviderEventType.USAGE:
                    saw_usage = True
            observations.append("typed_stream")
        except Exception as exc:
            tools_accepted = False
            observations.append(f"tool_stream_unavailable:{type(exc).__name__}")
        if saw_tool:
            observations.append("tool_call_observed")
        elif tools_accepted:
            observations.append("tool_call_not_observed")
        if saw_reasoning:
            observations.append("reasoning_continuation_observed")
        if saw_usage:
            observations.append("usage_observed")
        hosted_web_search = False
        if protocol in {
            ProviderProtocol.OPENAI_RESPONSES,
            ProviderProtocol.ANTHROPIC_MESSAGES,
        }:
            hosted_tool = [{
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the public web for current information.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            }]
            try:
                for event in provider.stream_events(
                    [{
                        "role": "user",
                        "content": (
                            "Use the web search tool once for the exact query "
                            "'Gitgo provider capability probe'."
                        ),
                    }],
                    max_tokens=128,
                    timeout=timeout,
                    tools=hosted_tool,
                    metadata={
                        "hosted_web_search": True,
                        "web_search_mode": "provider",
                    },
                ):
                    if event.type in {
                        ProviderEventType.SERVER_TOOL_STARTED,
                        ProviderEventType.SERVER_TOOL_RESULT,
                    }:
                        hosted_web_search = True
                observations.append(
                    "hosted_web_search_observed"
                    if hosted_web_search else "hosted_web_search_not_observed"
                )
            except Exception as exc:
                observations.append(
                    f"hosted_web_search_unavailable:{type(exc).__name__}"
                )
        capabilities = ProviderCapabilities(
            protocol=protocol.value,
            streaming="typed_stream" in observations,
            tools=saw_tool,
            # One forced call cannot establish safe parallel-call semantics.
            parallel_tools=False,
            reasoning_continuation=saw_reasoning,
            prompt_cache=(
                "automatic"
                if protocol in {
                    ProviderProtocol.OPENAI_CHAT,
                    ProviderProtocol.OPENAI_RESPONSES,
                    ProviderProtocol.ANTHROPIC_MESSAGES,
                } else "unsupported"
            ),
            # A Responses-shaped endpoint does not imply server-side storage;
            # compatible providers may deliberately be stateless.  A future
            # dedicated storage probe can promote this capability.
            supports_store=False,
            hosted_web_search=hosted_web_search,
            probed_at=datetime.now().isoformat(),
            probe_version=2,
        )
        return ProviderProbeResult(
            protocol.value, capabilities, text, tuple(observations),
        )

    @staticmethod
    def _candidates(base_url: str, requested: ProviderProtocol) -> list[ProviderProtocol]:
        if requested != ProviderProtocol.AUTO:
            return [requested]
        if "anthropic" in base_url.casefold() or "claude" in base_url.casefold():
            return [
                ProviderProtocol.ANTHROPIC_MESSAGES,
                ProviderProtocol.OPENAI_RESPONSES,
                ProviderProtocol.OPENAI_CHAT,
            ]
        return [
            ProviderProtocol.OPENAI_RESPONSES,
            ProviderProtocol.OPENAI_CHAT,
            ProviderProtocol.ANTHROPIC_MESSAGES,
        ]
