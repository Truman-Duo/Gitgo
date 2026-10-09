"""Allowlisted numeric projections; never retain prompt/tool/response bodies."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

EVENTS = (
    "task_admitted", "provider_request_started", "provider_usage",
    "provider_response_completed", "provider_response_incomplete", "tool_result",
    "decision_required", "completion_gate", "agent_complete", "stream_recovery",
    "session_recovery_resumed", "session_recovery_discarded", "session_recovery_blocked",
    "context_compaction_completed", "context_window_action", "governance_snapshot",
)


class DetailUnavailable(ValueError):
    pass


class DetailReader:
    def __init__(self, cas: Path):
        self.cas = cas
        self.remaining = 8 * 1024**2
        self.visited: set[str] = set()

    def read(self, reference: str) -> dict:
        digest = reference.split(":")[-1]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DetailUnavailable("invalid_reference")
        path = self.cas / digest[:2] / digest[2:]
        if not path.is_file():
            raise DetailUnavailable("missing_detail")
        with path.open("rb") as handle:
            data = handle.read(self.remaining + 1)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise DetailUnavailable("detail_size_limit")
        if hashlib.sha256(data).hexdigest() != digest:
            raise DetailUnavailable("detail_hash_mismatch")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise DetailUnavailable("invalid_detail")
        return value

    def snapshot(self, reference: str) -> dict:
        if reference in self.visited or len(self.visited) >= 128:
            raise DetailUnavailable("detail_chain_limit")
        self.visited.add(reference)
        detail = self.read(reference)
        if detail.get("snapshot_mode") == "prefix_delta":
            base = self.snapshot(detail["base_detail_ref"])
            prefix = detail["common_prefix_messages"]
            if not isinstance(prefix, int) or not 0 <= prefix <= len(base["messages"]):
                raise DetailUnavailable("invalid_delta")
            detail = {**detail, "messages": base["messages"][:prefix] + detail["appended_messages"],
                      "tools": base["tools"] if detail.get("tools_reused") else detail.get("tools", [])}
        if not isinstance(detail.get("messages"), list) or not isinstance(detail.get("tools"), list):
            raise DetailUnavailable("invalid_snapshot")
        return detail


def measure_prompt(messages: list, tools: list) -> dict:
    def content(message):
        value = message.get("content", "")
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    systems, envelopes, updates, users = [], [], [], []
    for message in messages:
        text = content(message)
        if message.get("role") == "system":
            systems.append(text)
        elif text.startswith("[HOST CURRENT-TURN ENVELOPE]"):
            envelopes.append(text)
        elif text.startswith("[HOST TASK CONTRACT UPDATE]"):
            updates.append(text)
        elif (message.get("role") == "user" and not message.get("host_authority")
              and message.get("message_type") in (None, "conversation")):
            users.append(text)
    metrics = {
        "message_count": len(messages), "tool_count": len(tools),
        "system_characters": sum(map(len, systems)),
        "current_turn_envelope_characters": sum(map(len, envelopes)),
        "contract_update_characters": sum(map(len, updates)),
        "contract_update_count": len(updates),
        "tool_schema_characters": len(json.dumps(tools, ensure_ascii=False, separators=(",", ":"))),
    }
    metrics["instruction_characters"] = sum(metrics[k] for k in
        ("system_characters", "current_turn_envelope_characters", "contract_update_characters"))
    if users:
        metrics["latest_user_characters"] = len(users[-1])
        if users[-1]:
            metrics["instruction_to_user_ratio"] = metrics["instruction_characters"] / len(users[-1])
    return metrics


def project(record: dict, cas: Path) -> tuple[dict, dict, str]:
    event = record["event"]
    metrics: dict = {}
    dimensions: dict = {}

    def label(key, value):
        # Metadata labels only. Arbitrary prose, paths and error messages are excluded.
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value):
            dimensions[key] = value

    def number(key, value):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1e15 and math.isfinite(value):
            metrics[key] = value

    for key in ("task_kind", "actor_kind", "process_id", "model", "protocol", "tool_name", "status"):
        label(key, record.get(key))
    number("monotonic_ns", record.get("monotonic_ns"))
    number("step", record.get("step"))
    number("snapshot_bytes", record.get("detail_bytes"))
    detail_status = "not_needed"
    if event in {"provider_request_started", "task_admitted"}:
        try:
            reader = DetailReader(cas)
            if event == "provider_request_started":
                detail = reader.snapshot(record.get("detail_ref", ""))
                metrics.update(measure_prompt(detail["messages"], detail["tools"]))
                for key in ("prompt_schema_hash", "tool_schema_hash"):
                    label(key, (record.get("cache_intent") or {}).get(key))
            else:
                detail = reader.read(record.get("detail_ref", ""))
                instruction = detail.get("instruction")
                if isinstance(instruction, str):
                    number("user_instruction_characters", len(instruction))
                label("terminal", (detail.get("frontend_origin") or {}).get("terminal"))
            detail_status = "ok"
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
            detail_status = str(error) if isinstance(error, DetailUnavailable) else "invalid_detail"
    if event == "provider_usage":
        usage = record.get("usage") or {}
        raw = usage.get("raw")
        known = set(usage)
        if isinstance(raw, dict):
            # Runtime normalization defaults missing fields to zero. That is not
            # evidence the provider actually reported zero cache/reasoning usage.
            input_details = raw.get("input_tokens_details") or raw.get("prompt_tokens_details") or {}
            output_details = raw.get("output_tokens_details") or raw.get("completion_tokens_details") or {}
            known = set()
            if "input_tokens" in raw or "prompt_tokens" in raw: known.add("input_tokens")
            if "output_tokens" in raw or "completion_tokens" in raw: known.add("output_tokens")
            if "total_tokens" in raw or {"input_tokens", "output_tokens"} <= known: known.add("total_tokens")
            if "reasoning_tokens" in output_details: known.add("reasoning_tokens")
            if "cache_read_input_tokens" in raw or "cached_tokens" in input_details: known.add("cache_read_tokens")
            if "cache_creation_input_tokens" in raw or "cache_write_tokens" in input_details: known.add("cache_write_tokens")
        for key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens"):
            if key in known:
                number(key, usage.get(key))
    if event == "agent_complete":
        outcome = record.get("outcome") or {}
        label("status", outcome.get("status"))
        number("duration_ms", outcome.get("duration_ms"))
        number("steps_used", outcome.get("steps_used"))
        response = outcome.get("response")
        if isinstance(response, str):
            number("response_characters", len(response))
        label("error_code", (outcome.get("error") or {}).get("code") if isinstance(outcome.get("error"), dict) else None)
    if event == "tool_result":
        if isinstance(record.get("is_error"), bool):
            number("tool_errors", int(record["is_error"]))
        receipt = record.get("receipt") or {}
        number("duration_ms", receipt.get("duration_ms"))
    if event == "decision_required":
        decision = record.get("decision") or record.get("pending_question") or {}
        label("decision_kind", decision.get("kind"))
    if event == "completion_gate":
        label("verdict", record.get("verdict"))
        if isinstance(record.get("allowed"), bool):
            number("gate_allowed", int(record["allowed"]))
        if isinstance(record.get("accepted"), bool):
            number("gate_accepted", int(record["accepted"]))
    return dimensions, metrics, detail_status
