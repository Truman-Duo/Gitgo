"""Provider-neutral request, event, usage, and cache contracts.

The Agent loop consumes these objects.  Wire adapters are deliberately kept
outside the loop so OpenAI Responses, Anthropic Messages, and OpenAI-compatible
Chat Completions can share cancellation, retry, budgeting, and evidence logic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ProviderProtocol(str, Enum):
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    AUTO = "auto"


class ProviderEventType(str, Enum):
    RESPONSE_STARTED = "response_started"
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    REASONING_ARTIFACT = "reasoning_artifact"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_DONE = "tool_call_done"
    SERVER_TOOL_STARTED = "server_tool_started"
    SERVER_TOOL_RESULT = "server_tool_result"
    PROVIDER_CAPABILITY_FALLBACK = "provider_capability_fallback"
    USAGE = "usage"
    RESPONSE_INCOMPLETE = "response_incomplete"
    RESPONSE_COMPLETED = "response_completed"


@dataclass(frozen=True)
class ProviderCapabilities:
    protocol: str
    streaming: bool = True
    tools: bool = True
    parallel_tools: bool = True
    reasoning_continuation: bool = False
    prompt_cache: str = "automatic"  # automatic | explicit | unsupported
    context_window: int = 128000
    max_output_tokens: int = 4096
    supports_store: bool = False
    hosted_web_search: bool = False
    hosted_web_fetch: bool = False
    probed_at: str = ""
    probe_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict | None, *, protocol: str) -> "ProviderCapabilities":
        data = dict(raw or {})
        data["protocol"] = protocol
        allowed = {item.name for item in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass(frozen=True)
class CacheIntent:
    mode: str = "automatic"  # automatic | explicit | unsupported
    stable_prefix_key: str = ""
    context_epoch: int = 0
    ttl: str = "5m"
    stable_prefix_hash: str = ""
    tool_schema_hash: str = ""
    prompt_schema_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProviderUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProviderEvent:
    type: ProviderEventType
    text: str = ""
    reasoning: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    arguments_delta: str = ""
    arguments: str = ""
    response_id: str = ""
    output_index: int = -1
    usage: ProviderUsage | None = None
    artifact: Any = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["type"] = self.type.value
        return payload


@dataclass(frozen=True)
class ProviderRequest:
    messages: tuple[dict, ...]
    tools: tuple[dict, ...] = ()
    max_output_tokens: int = 4096
    stream: bool = True
    cache_intent: CacheIntent = field(default_factory=CacheIntent)
    metadata: dict = field(default_factory=dict)


def deterministic_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_usage(raw: dict | None) -> ProviderUsage:
    """Normalize usage fields emitted by all three supported protocols."""
    data = dict(raw or {})
    input_details = data.get("input_tokens_details") or data.get(
        "prompt_tokens_details", {}
    ) or {}
    output_details = data.get("output_tokens_details") or data.get(
        "completion_tokens_details", {}
    ) or {}
    input_tokens = int(data.get("input_tokens", data.get("prompt_tokens", 0)) or 0)
    output_tokens = int(data.get(
        "output_tokens", data.get("completion_tokens", 0),
    ) or 0)
    cache_read = int(data.get(
        "cache_read_input_tokens", input_details.get("cached_tokens", 0),
    ) or 0)
    cache_write = int(data.get(
        "cache_creation_input_tokens", input_details.get("cache_write_tokens", 0),
    ) or 0)
    reasoning = int(output_details.get("reasoning_tokens", 0) or 0)
    total = int(data.get("total_tokens", input_tokens + output_tokens) or 0)
    return ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total or input_tokens + output_tokens,
        reasoning_tokens=reasoning,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        raw=data,
    )
