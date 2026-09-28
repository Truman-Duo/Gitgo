"""AgentSession — Agent 的独立持久会话。

每个 B Agent 拥有独立的 message history，不与其他 Agent 共享。
A Agent 通过 ContextBuilder 注入治理简报作为 system prompt。
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field

from backend.core.loop.provider_protocol import CacheIntent, deterministic_hash


@dataclass
class AgentSession:
    """B-level Agent 独立会话。"""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    display_name: str = ""
    worker_sequence: int = 0
    messages: list[dict] = field(default_factory=list)
    # Provider state may include raw reasoning_content. By product decision it
    # is persisted as plaintext session data, but remains a low-authority record
    # and is never compiled into system/governance instructions.
    provider_state: dict[str, dict] = field(default_factory=dict, repr=False)
    # Provider-native continuation artifacts are only valid for the exact
    # protocol/endpoint/model route that produced them.  Public conversation
    # remains portable when a user switches providers; native reasoning and
    # function-call state does not.
    active_provider_route: str = ""
    context_epoch: int = 0
    model_context_limit: int = 128000
    context_flags: set[str] = field(default_factory=set, repr=False)
    context_memo: dict[str, dict] = field(default_factory=dict, repr=False)
    _context_memo_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False,
    )
    host_ledger: list[dict] = field(default_factory=list, repr=False)
    provider_usage: list[dict] = field(default_factory=list, repr=False)
    cache_telemetry: list[dict] = field(default_factory=list, repr=False)
    context_inventory: dict = field(default_factory=dict, repr=False)
    epoch_archive: list[dict] = field(default_factory=list, repr=False)
    steering_sequence: int = 0
    compiled_prompt_hash: str = ""
    compiled_prompt_sections: dict[str, str] = field(default_factory=dict, repr=False)
    stable_prefix_hash: str = ""
    last_cache_intent_key: str = ""
    last_cache_context_epoch: int = -1
    manual_compact_requested: bool = False
    force_compact_requested: bool = False
    context_abort_requested: bool = False
    compaction_failure_count: int = 0
    last_compaction_error: str = ""
    pending_compaction_decision: dict | None = None

    @classmethod
    def from_durable_state(cls, state: dict) -> "AgentSession":
        """Rebuild a provider-valid session from the authoritative checkpoint."""
        session = cls(session_id=str(state.get("session_id") or uuid.uuid4()))
        session.messages = [dict(item) for item in (state.get("messages") or [])]
        session.provider_state = {
            str(key): dict(value)
            for key, value in dict(state.get("provider_state") or {}).items()
            if isinstance(value, dict)
        }
        for name in (
            "context_epoch", "model_context_limit", "steering_sequence",
            "last_cache_context_epoch", "worker_sequence",
        ):
            if name in state:
                setattr(session, name, int(state[name] or 0))
        for name in (
            "compiled_prompt_hash", "stable_prefix_hash", "last_cache_intent_key", "display_name",
            "active_provider_route",
        ):
            if name in state:
                setattr(session, name, str(state[name] or ""))
        session.compiled_prompt_sections = {
            str(key): str(value)
            for key, value in dict(state.get("compiled_prompt_sections") or {}).items()
        }
        session.manual_compact_requested = bool(
            state.get("manual_compact_requested", False)
        )
        session.force_compact_requested = bool(
            state.get("force_compact_requested", False)
        )
        session.context_abort_requested = bool(
            state.get("context_abort_requested", False)
        )
        session.compaction_failure_count = int(
            state.get("compaction_failure_count", 0) or 0
        )
        session.last_compaction_error = str(
            state.get("last_compaction_error", "") or ""
        )
        session.pending_compaction_decision = (
            dict(state["pending_compaction_decision"])
            if isinstance(state.get("pending_compaction_decision"), dict) else None
        )
        for name in (
            "context_memo", "host_ledger", "provider_usage",
            "cache_telemetry", "context_inventory", "epoch_archive",
        ):
            if name not in state:
                continue
            value = state[name]
            setattr(
                session, name,
                dict(value) if name in {"context_memo", "context_inventory"}
                else list(value or []),
            )
        return session

    @staticmethod
    def _provider_call_ids(state: dict) -> set[str]:
        """Return every native call id owned by one continuation state."""
        call_ids = {
            str(call.get("id", ""))
            for call in (state.get("tool_calls", []) or [])
            if isinstance(call, dict) and call.get("id")
        }
        call_ids.update(
            str(item.get("call_id") or item.get("id") or "")
            for item in (state.get("response_output_items", []) or [])
            if isinstance(item, dict)
            and item.get("type") == "function_call"
            and (item.get("call_id") or item.get("id"))
        )
        call_ids.update(
            str(item.get("id") or "")
            for item in (state.get("anthropic_content_blocks", []) or [])
            if isinstance(item, dict)
            and item.get("type") == "tool_use"
            and item.get("id")
        )
        return call_ids

    def bind_provider_route(self, route_key: str) -> bool:
        """Bind this live turn to a provider route without losing dialogue.

        Older checkpoints did not label provider-native state.  Such state is
        deliberately quarantined on first bind instead of guessing that it
        belongs to the newly selected provider.  This is a safe one-time
        compatibility downgrade: public assistant text remains visible while
        ambiguous reasoning/tool protocol objects are not replayed.
        """
        route_key = str(route_key or "").strip()
        if not route_key:
            return False
        previous = self.active_provider_route
        if not previous:
            legacy_route = "legacy:unscoped"
            for state in self.provider_state.values():
                state.setdefault("provider_route", legacy_route)
        elif previous != route_key:
            for state in self.provider_state.values():
                state.setdefault("provider_route", previous)
        self.active_provider_route = route_key
        return bool(previous and previous != route_key) or (
            not previous and bool(self.provider_state)
        )

    def seal_dangling_tool_calls(
        self, *, recovery_code: str, provider_route: str = "",
    ) -> list[str]:
        """Close provider-native calls whose durable result was never recorded.

        A daemon can die after the assistant call is checkpointed but before a
        tool result reaches SQLite.  The next request must contain a matching
        result item, but that result must never pretend the tool did or did not
        run.  A host-authored unknown-result record keeps all three provider
        protocols valid without replaying the side effect.
        """
        completed = {
            str(item.get("tool_call_id", ""))
            for item in self.messages
            if item.get("role") == "tool" and item.get("tool_call_id")
        }
        sealed: list[str] = []
        for message in list(self.messages):
            state_id = str(message.get("provider_state_id", ""))
            state = self.provider_state.get(state_id, {})
            if provider_route and state.get("provider_route") != provider_route:
                continue
            generated_results: list[dict] = []
            calls: list[dict] = []
            calls.extend(
                dict(call) for call in (state.get("tool_calls", []) or [])
                if isinstance(call, dict)
            )
            calls.extend({
                "id": item.get("call_id") or item.get("id"),
                "name": item.get("name"),
            } for item in (state.get("response_output_items", []) or [])
                if isinstance(item, dict) and item.get("type") == "function_call")
            calls.extend({
                "id": item.get("id"),
                "name": item.get("name"),
            } for item in (state.get("anthropic_content_blocks", []) or [])
                if isinstance(item, dict) and item.get("type") == "tool_use")
            for call in calls:
                call_id = str(call.get("id", ""))
                if not call_id or call_id in completed:
                    continue
                function = call.get("function", call) or {}
                tool_name = str(function.get("name", "unknown"))
                generated_results.append({
                    "role": "tool",
                    "content": (
                        "[HOST RECOVERY] The prior provider turn ended before a "
                        "durable tool result was recorded. Execution state is unknown; "
                        "do not repeat this operation automatically. Inspect current "
                        "state first."
                    ),
                    "message_type": "tool_result",
                    "_tool_name": tool_name,
                    "is_error": True,
                    "data": {
                        "code": recovery_code,
                        "execution_state": "unknown",
                    },
                    "receipt": {
                        "effect_state": "ambiguous",
                        "recovered": True,
                    },
                    "tool_call_id": call_id,
                })
                completed.add(call_id)
                sealed.append(call_id)
            if generated_results:
                # Provider protocols require results directly after their
                # assistant tool-call turn. A new user message may already have
                # been appended before live repair discovers an old checkpoint,
                # so appending at the tail is not sufficient.
                message_index = next(
                    index for index, current in enumerate(self.messages)
                    if current is message
                )
                self.messages[message_index + 1:message_index + 1] = generated_results
        return sealed

    def append(self, role: str, content: str,
               referenced_files: list[str] | None = None,
               message_type: str = "") -> None:
        """追加消息。v0.36: 支持结构化元数据。

        referenced_files: 消息涉及的文件路径列表（产生时打，不消费时反解）。
        message_type: "conversation" | "tool_result" | "governance_nudge" | "compact_transcript"
        """
        msg = {"role": role, "content": content}
        if referenced_files:
            msg["referenced_files"] = referenced_files
        if message_type:
            msg["message_type"] = message_type
        self.messages.append(msg)

    def append_system(self, content: str, **kwargs) -> None:
        self.messages.append({"role": "system", "content": content, **kwargs})

    def append_user(self, content: str,
                    referenced_files: list[str] | None = None,
                    message_type: str = "") -> None:
        msg = {"role": "user", "content": content}
        if referenced_files:
            msg["referenced_files"] = referenced_files
        if message_type:
            msg["message_type"] = message_type
        self.messages.append(msg)

    def append_assistant(self, content: str, **kwargs) -> None:
        self.messages.append({"role": "assistant", "content": content, **kwargs})

    def append_assistant_provider(
        self,
        content: str,
        *,
        tool_calls: list[dict] | None = None,
        continuation_state: dict | None = None,
    ) -> None:
        """Append an assistant turn and retain plaintext provider state."""
        state_id = str(uuid.uuid4())
        message = {
            "role": "assistant",
            "content": content,
            "provider_state_id": state_id,
        }
        self.messages.append(message)
        state: dict = {}
        if tool_calls:
            state["tool_calls"] = tool_calls
        if continuation_state:
            state.update(continuation_state)
        if state:
            if self.active_provider_route:
                state["provider_route"] = self.active_provider_route
            self.provider_state[state_id] = state

    def append_host_steering(
        self, content: str, *, steering_type: str, version: str = "",
    ) -> dict:
        """Append a host-labelled, model-visible delta without rewriting history."""
        self.steering_sequence += 1
        message = {
            "role": "user",
            "content": content,
            "message_type": "host_steering_delta",
            "host_authority": True,
            "steering_type": steering_type,
            "steering_version": version,
            "steering_sequence": self.steering_sequence,
        }
        self.messages.append(message)
        return message

    def append_tool_result(
        self,
        content: str,
        *,
        tool_name: str,
        tool_call_id: str = "",
        is_error: bool = False,
        data: dict | None = None,
        receipt: dict | None = None,
        referenced_files: list[str] | None = None,
    ) -> None:
        """Append a provider-native tool result plus host-verifiable metadata."""
        message = {
            "role": "tool" if tool_call_id else "user",
            "content": content,
            "message_type": "tool_result",
            "_tool_name": tool_name,
            "is_error": bool(is_error),
            "data": data,
            "receipt": receipt or {},
        }
        if tool_call_id:
            message["tool_call_id"] = tool_call_id
        if referenced_files:
            message["referenced_files"] = referenced_files
        self.messages.append(message)

    def estimate_tokens(self) -> int:
        """字符数 / 4 估算 token 数（保守估计，覆盖中英文混合）。"""
        total = sum(len(m.get("content", "")) for m in self.messages)
        # Provider-native continuation artifacts are replayed even though they
        # are intentionally kept out of model-visible governance text.
        total += sum(len(json.dumps(
            state, ensure_ascii=False, sort_keys=True, default=str,
        )) for state in self.provider_state.values())
        return max(1, total // 4)

    def context_breakdown(self) -> dict:
        """Return the canonical approximate token ownership by prompt/history area."""
        sections: dict[str, int] = {}

        def add(name: str, value: int) -> None:
            amount = max(0, int(value))
            if amount:
                sections[name] = sections.get(name, 0) + amount

        for message in self.messages:
            content_tokens = max(0, len(str(message.get("content") or "")) // 4)
            prompt_sections = list(message.get("prompt_sections") or [])
            if prompt_sections:
                measured = 0
                for item in prompt_sections:
                    if not isinstance(item, dict):
                        continue
                    value = max(0, int(item.get("estimated_tokens", 0) or 0))
                    measured += value
                    add(str(item.get("name") or "Prompt"), value)
                if content_tokens > measured:
                    add("Prompt framing", content_tokens - measured)
                continue
            message_type = str(message.get("message_type") or "")
            role = str(message.get("role") or "")
            if message_type == "tool_result" or role == "tool":
                area = "Tool results"
            elif message_type in {"context_checkpoint", "context_summary"}:
                area = "Context checkpoints"
            elif message_type.startswith("host_") or message_type.endswith("steering"):
                area = "Host steering"
            elif role == "user":
                area = "User conversation"
            elif role == "assistant":
                area = "Assistant conversation"
            else:
                area = "Other history"
            add(area, content_tokens)
        provider_tokens = sum(len(json.dumps(
            state, ensure_ascii=False, sort_keys=True, default=str,
        )) for state in self.provider_state.values()) // 4
        add("Provider continuation", provider_tokens)
        add("Tool schemas", int(self.context_inventory.get("tool_schema_tokens", 0) or 0))
        total = max(1, sum(sections.values()))
        return {
            "estimated_tokens": total,
            "sections": [
                {"name": name, "estimated_tokens": value,
                 "ratio": round(value / total, 6)}
                for name, value in sorted(sections.items(), key=lambda item: (-item[1], item[0]))
            ],
        }

    def context_view(self, *, auto_compact: bool = True) -> dict:
        """Return the provider-visible context bill used by Dashboard.

        Provider input usage is the best headline measurement after a call;
        section ownership remains an explicit estimate because APIs do not
        report per-section token counts. Anthropic reports cached input beside
        uncached input, while OpenAI-style protocols include it in input_tokens.
        """
        breakdown = self.context_breakdown()
        estimated = int(breakdown.get("estimated_tokens", 0) or 0)
        reported = 0
        if self.provider_usage:
            latest = dict(self.provider_usage[-1] or {})
            reported = int(latest.get("input_tokens", 0) or 0)
            if str(latest.get("protocol") or "") == "anthropic_messages":
                reported += int(latest.get("cache_read_tokens", 0) or 0)
                reported += int(latest.get("cache_write_tokens", 0) or 0)
        # The latest assistant/tool result has not necessarily appeared in a
        # subsequent API usage record yet. Never let that lag make the current
        # context meter move backwards.
        used = max(reported, estimated)
        limit = max(0, int(self.model_context_limit or 0))
        compact_reserve = int(limit * 0.10) if auto_compact and limit else 0
        free = max(0, limit - used - compact_reserve)
        sections = [dict(item) for item in breakdown.get("sections", [])]
        section_total = sum(int(item.get("estimated_tokens", 0) or 0) for item in sections)
        if used > section_total:
            sections.append({
                "name": "Provider overhead",
                "estimated_tokens": used - section_total,
                "ratio": round((used - section_total) / max(1, limit), 6),
            })
        for item in sections:
            item["ratio"] = round(
                int(item.get("estimated_tokens", 0) or 0) / max(1, limit), 6,
            )
        return {
            "used_tokens": used,
            "estimated_tokens": estimated,
            "provider_reported_tokens": reported,
            "measurement": (
                "provider+increment_estimate" if reported > 0 and estimated > reported
                else "provider" if reported > 0 else "estimated"
            ),
            "limit": limit,
            "free_tokens": free,
            "auto_compact_tokens": compact_reserve,
            "auto_compact_enabled": bool(auto_compact),
            "epoch": int(self.context_epoch or 0),
            "tool_count": int(self.context_inventory.get("tool_count", 0) or 0),
            "breakdown": {"estimated_tokens": estimated, "sections": sections},
        }

    def _excluded_provider_call_ids(self, provider_route: str) -> set[str]:
        if not provider_route:
            return set()
        excluded: set[str] = set()
        for state in self.provider_state.values():
            if state.get("provider_route") != provider_route:
                excluded.update(self._provider_call_ids(state))
        return excluded

    def to_provider_messages(
        self, *, dynamic_envelope: str = "", provider_route: str = "",
    ) -> list[dict]:
        """Return canonical history plus an optional ephemeral tail envelope.

        The envelope is deliberately not appended to ``messages``.  It belongs
        after the append-only history so changing current-turn operational
        guidance does not invalidate the reusable prompt/history prefix or
        pollute later conversation recall.
        """
        result = []
        excluded_call_ids = self._excluded_provider_call_ids(provider_route)
        for message in self.messages:
            if (
                message.get("role") == "tool"
                and str(message.get("tool_call_id") or "") in excluded_call_ids
            ):
                continue
            wire = dict(message)
            state_id = message.get("provider_state_id", "")
            state = self.provider_state.get(state_id, {})
            state_matches = bool(state) and (
                not provider_route or state.get("provider_route") == provider_route
            )
            if state_matches:
                wire["provider_state"] = dict(state)
            elif state:
                wire.pop("provider_state_id", None)
                if not str(wire.get("content") or "").strip():
                    continue
            result.append(wire)
        if dynamic_envelope.strip():
            result.append({
                "role": "user",
                "content": dynamic_envelope.strip(),
                "message_type": "host_dynamic_envelope",
                "host_authority": True,
            })
        return result

    def cache_intent(self, tools: list[dict] | None = None, *, mode: str) -> CacheIntent:
        stable_messages = [
            {"role": item.get("role"), "content": item.get("content", "")}
            for item in self.messages
            if item.get("message_type") == "compiled_system_prompt"
        ]
        stable_hash = deterministic_hash(stable_messages)
        tool_hash = deterministic_hash(tools or [])
        self.stable_prefix_hash = stable_hash
        return CacheIntent(
            mode=mode,
            stable_prefix_key=deterministic_hash({
                "stable": stable_hash,
                "tools": tool_hash,
            }),
            context_epoch=self.context_epoch,
            stable_prefix_hash=stable_hash,
            tool_schema_hash=tool_hash,
            prompt_schema_hash=self.compiled_prompt_hash,
        )

    def record_provider_usage(
        self, usage: dict, *, protocol: str = "", cache_intent: CacheIntent | None = None,
    ) -> None:
        record = {"protocol": protocol, **dict(usage or {})}
        if cache_intent is not None:
            record["cache_intent"] = cache_intent.to_dict()
        self.provider_usage.append(record)
        cache_read = int(record.get("cache_read_tokens", 0) or 0)
        input_tokens = int(record.get("input_tokens", 0) or 0)
        intent_key = cache_intent.stable_prefix_key if cache_intent is not None else ""
        if cache_intent is None or cache_intent.mode == "unsupported":
            miss_reason = "unsupported_or_untracked"
            eligible = False
        elif not self.last_cache_intent_key:
            miss_reason = "cold_start"
            eligible = False
        elif self.last_cache_intent_key != intent_key:
            miss_reason = "route_or_tool_schema_changed"
            eligible = False
        elif self.last_cache_context_epoch != self.context_epoch:
            miss_reason = "context_epoch_reset"
            eligible = False
        elif cache_read > 0:
            miss_reason = ""
            eligible = True
        else:
            miss_reason = "provider_miss_or_unreported"
            eligible = True
        self.cache_telemetry.append({
            "context_epoch": self.context_epoch,
            "stable_prefix_hash": self.stable_prefix_hash,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": int(record.get("cache_write_tokens", 0) or 0),
            "input_tokens": input_tokens,
            "eligible_for_reuse": eligible,
            "miss_reason": miss_reason,
        })
        if cache_intent is not None:
            self.last_cache_intent_key = intent_key
            self.last_cache_context_epoch = self.context_epoch

    def cache_summary(self, telemetry: list[dict] | None = None) -> dict:
        """Return raw and reuse-eligible cache ratios without hiding cold starts."""
        records = self.cache_telemetry if telemetry is None else telemetry
        total_input = sum(int(item.get("input_tokens", 0) or 0)
                          for item in records)
        total_read = sum(int(item.get("cache_read_tokens", 0) or 0)
                         for item in records)
        eligible_input = sum(
            int(item.get("input_tokens", 0) or 0)
            for item in records
            if item.get("eligible_for_reuse")
        )
        eligible_read = sum(
            int(item.get("cache_read_tokens", 0) or 0)
            for item in records
            if item.get("eligible_for_reuse")
        )
        return {
            "input_tokens": total_input,
            "cache_read_tokens": total_read,
            "raw_hit_ratio": round(total_read / total_input, 4) if total_input else 0.0,
            "eligible_input_tokens": eligible_input,
            "eligible_cache_read_tokens": eligible_read,
            "eligible_hit_ratio": (
                round(eligible_read / eligible_input, 4) if eligible_input else 0.0
            ),
            "miss_reasons": list(dict.fromkeys(
                item.get("miss_reason", "") for item in records
                if item.get("miss_reason")
            )),
        }

    def begin_context_epoch(
        self, *, summary: str, retained_messages: list[dict], reason: str,
        checkpoint_after_retained: bool = False,
    ) -> None:
        """Perform one auditable low-frequency GC and start a new append-only epoch."""
        self.epoch_archive.append({
            "epoch": self.context_epoch,
            "reason": reason,
            "messages": list(self.messages),
            "provider_state": dict(self.provider_state),
        })
        self.context_epoch += 1
        prefix = [
            item for item in self.messages
            if item.get("message_type") in {
                "compiled_system_prompt", "compiled_task_contract",
            }
        ][:2]
        checkpoint = {
            "role": "user",
            "content": f"[CONTEXT EPOCH {self.context_epoch} CHECKPOINT]\n{summary}",
            "message_type": "compact_transcript",
            "context_epoch": self.context_epoch,
        }
        self.messages = (
            prefix + list(retained_messages) + [checkpoint]
            if checkpoint_after_retained
            else prefix + [checkpoint] + list(retained_messages)
        )
        active_state_ids = {
            item.get("provider_state_id") for item in self.messages
            if item.get("provider_state_id")
        }
        self.provider_state = {
            key: value for key, value in self.provider_state.items()
            if key in active_state_ids
        }
        self.context_flags.clear()

    def compact_completed_task_boundary(
        self,
        *,
        checkpoint: dict,
        token_threshold: int = 64000,
        retained_public_messages: int = 8,
    ) -> bool:
        """Seal old execution mechanics before an unrelated root task starts.

        A project session intentionally survives several user turns, but
        provider-native reasoning/function-call state is useful only while its
        task is active.  Replaying many completed tasks teaches the model stale
        capability surfaces and makes a small new request pay for old internal
        work.  At a terminal task boundary, retain a bounded public dialogue
        tail plus a Host-authored outcome checkpoint; the complete epoch is
        still kept in the local audit archive by ``begin_context_epoch``.

        This is deterministic and incurs no model call.  It is deliberately
        gated by an absolute live-context threshold so short iterative work
        keeps its natural conversational continuity and cache prefix.
        """
        stable_allowed = {
            "Behavior and delivery standard", "User collaboration protocol",
        }
        prompt_migration_required = False
        for message in self.messages:
            if message.get("message_type") != "compiled_system_prompt":
                continue
            names = {
                str(item.get("name") or "")
                for item in (message.get("prompt_sections") or [])
                if isinstance(item, dict)
            }
            if (
                int(message.get("prompt_schema_version") or 0) < 6
                or (names and not names.issubset(stable_allowed))
            ):
                prompt_migration_required = True
                break
        if (
            self.estimate_tokens() <= max(1024, int(token_threshold))
            and not prompt_migration_required
        ):
            return False

        public: list[dict] = []
        for message in self.messages:
            if str(message.get("role") or "") not in {"user", "assistant"}:
                continue
            if str(message.get("message_type") or "") not in {"", "conversation"}:
                continue
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            # Public dialogue preserves intent and user-facing conclusions, not
            # provider continuation ids, reasoning artifacts or tool calls.
            public.append({
                "role": str(message.get("role")),
                "content": content[:8000],
                "message_type": "conversation",
            })
        retained = public[-max(0, int(retained_public_messages)):]
        safe_checkpoint = {
            "previous_task_status": str(checkpoint.get("status") or "unknown"),
            "previous_result": str(checkpoint.get("response") or "")[:8000],
            "deliverables": list(checkpoint.get("deliverables") or [])[:16],
            "child_outcomes": list(checkpoint.get("child_outcomes") or [])[:16],
            "instruction": (
                "The prior task is terminal. Historical tool errors and capability "
                "lists are audit evidence, not current runtime facts. Inspect the "
                "workspace or current Host tools before relying on them."
            ),
        }
        self.begin_context_epoch(
            summary=(
                "[HOST COMPLETED TASK BOUNDARY]\n"
                + json.dumps(safe_checkpoint, ensure_ascii=False, separators=(",", ":"))
            ),
            retained_messages=retained,
            reason="completed_task_boundary",
            checkpoint_after_retained=True,
        )
        # A new root task may reuse the conversation but must not reuse the
        # previous task's capability/contract prefix.  Re-seeding at this
        # already-cache-breaking epoch boundary is both cheaper and safer than
        # replaying a large chain of ROM/task deltas.  During migration, old
        # "stable" prefixes that contained dynamic capability sections are
        # dropped as well.
        migrated: list[dict] = []
        for message in self.messages:
            message_type = message.get("message_type")
            if message_type == "compiled_task_contract":
                continue
            if message_type == "compiled_system_prompt":
                names = {
                    str(item.get("name") or "")
                    for item in (message.get("prompt_sections") or [])
                    if isinstance(item, dict)
                }
                if names and not names.issubset(stable_allowed):
                    continue
            migrated.append(message)
        self.messages = migrated
        self.compiled_prompt_hash = ""
        self.compiled_prompt_sections = {}
        return True

    def last_assistant_at(self) -> float:
        """最近一条 assistant 消息的插入时间（Unix timestamp），用于 cache TTL 判断。"""
        import time
        return time.time()

    def inject_governance_brief(self, brief: dict) -> None:
        """注入治理上下文作为 system prompt（在 session 创建时调用一次）。

        兼容新旧两种格式:
        - 新格式: {"signals": [GovernanceSignal, ...], "brief": "..."}
        - 旧格式: {"phase_brief": "...", "contract_summary": "...", ...}
        """
        parts = [f"你是项目治理执行 Agent (ring 3)。"]

        # 新格式：直接使用 brief 文本
        brief_text = brief.get("brief")
        if brief_text is not None:
            parts.append(brief_text)
        else:
            # 旧格式兼容
            if brief.get("phase_brief"):
                parts.append(f"## 近期工具调用\n{brief['phase_brief']}")
            if brief.get("contract_summary"):
                parts.append(f"## 合约摘要\n{brief['contract_summary']}")
            if brief.get("lesson_matches"):
                parts.append(f"## 匹配的治理规则\n{brief['lesson_matches']}")
            if brief.get("rejection_history"):
                parts.append(f"## 近期被拒记录（请避免重复以下错误）\n{brief['rejection_history']}")

        self.append_system(
            "\n\n".join(parts), message_type="governance_context_seed",
        )

    def to_openai_messages(
        self, *, dynamic_envelope: str = "", provider_route: str = "",
    ) -> list[dict]:
        """转换为 OpenAI API 兼容的 messages 格式。"""
        result = []
        excluded_call_ids = self._excluded_provider_call_ids(provider_route)
        for message in self.messages:
            if (
                message.get("role") == "tool"
                and str(message.get("tool_call_id") or "") in excluded_call_ids
            ):
                continue
            wire = {"role": message["role"], "content": message.get("content", "")}
            state_id = message.get("provider_state_id", "")
            state = self.provider_state.get(state_id, {})
            if state and provider_route and state.get("provider_route") != provider_route:
                if not str(wire.get("content") or "").strip():
                    continue
                result.append(wire)
                continue
            # Only adapter-recognized continuation fields cross the boundary.
            for key in ("tool_calls", "reasoning_content", "reasoning_details"):
                if key in state:
                    wire[key] = state[key]
            if message.get("role") == "tool" and message.get("tool_call_id"):
                wire["tool_call_id"] = message["tool_call_id"]
            result.append(wire)
        if dynamic_envelope.strip():
            result.append({"role": "user", "content": dynamic_envelope.strip()})
        return result
