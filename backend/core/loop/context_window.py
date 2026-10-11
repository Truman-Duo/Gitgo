"""Context Window — 三档水位线 + harness 智能保留。

Sent history remains immutable; only explicit compaction creates a new epoch.

水位线:
- SOFT (50%):  仅通知，保护 prompt cache
- PRUNE (80%): pressure notice; no history mutation
- FORCE (90%): LLM 摘要压缩（付费，最后手段）

Harness 智能保留: 利用 contract / lesson / rejection 信息决定裁剪优先级。
四个参考工具都没有这个能力——它们的压缩是"盲"的。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.loop.session import AgentSession


class ContextWindow:
    """上下文窗口管理器。"""

    SOFT_RATIO = 0.5
    PRUNE_RATIO = 0.8
    FORCE_RATIO = 0.9
    SOFT_NOTICED = "soft_noticed"

    def __init__(self, model_token_limit: int = 128000):
        self._limit = model_token_limit
        self.last_compaction_error = ""

    # ── Public API ──────────────────────────────────────────

    def check(self, session: "AgentSession") -> dict:
        """检查水位线，返回建议动作。

        Returns:
            {"action": "none"|"soft_warn"|"prune"|"force_compact",
             "tokens": int, "usage_ratio": float, "pruned_count": 0}
        """
        tokens = session.estimate_tokens()
        ratio = tokens / self._limit if self._limit > 0 else 0

        if ratio >= self.FORCE_RATIO:
            return {"action": "force_compact", "tokens": tokens,
                    "usage_ratio": round(ratio, 3), "pruned_count": 0}

        if ratio >= self.PRUNE_RATIO:
            return {"action": "prune", "tokens": tokens,
                    "usage_ratio": round(ratio, 3), "pruned_count": 0}

        flags = getattr(session, "context_flags", set())
        if ratio >= self.SOFT_RATIO and self.SOFT_NOTICED not in flags:
            flags.add(self.SOFT_NOTICED)
            return {"action": "soft_warn", "tokens": tokens,
                    "usage_ratio": round(ratio, 3), "pruned_count": 0}

        return {"action": "none", "tokens": tokens,
                "usage_ratio": round(ratio, 3), "pruned_count": 0}


    def compact(self, session: "AgentSession",
                llm_provider, harness_data: dict | None = None,
                retention_suggestions: list[str] | None = None,
                task_budget=None, cancel_event=None,
                budget_process_id: str = "", on_provider_notice=None) -> bool:
        """LLM 摘要压缩（付费操作，仅 >90% 时触发）。

        将中间消息替换为结构化摘要。返回是否成功。
        """
        # v1: 仅在 >90% 时调用，用 LLM 生成摘要替换中间轮次
        self.last_compaction_error = ""
        messages = session.messages
        if len(messages) <= 6:
            self.last_compaction_error = "not_enough_foldable_history"
            return False

        harness = harness_data or {}

        # 提取 rejection 指令（兼容新旧格式）
        signals = harness.get("signals")
        if signals is not None:
            rejection_instructions = [
                s.rule for s in signals
                if s.source == "rejection" and s.rule
            ]
        else:
            rejection_instructions = harness.get("rejection_instructions", [])

        # 获取 RetentionAdvisor 保留建议（由调用方通过 SignalBus.dispatch 传入）
        retention_prefix = ""
        if retention_suggestions:
            retention_prefix = "## 高优先级保留策略\n" + "\n".join(
                f"- {s}" for s in retention_suggestions
            ) + "\n\n"

        foldable = messages[1:-3]  # 保留 system + 最近 3 条
        if not foldable:
            self.last_compaction_error = "not_enough_foldable_history"
            return False

        text_parts = []
        for m in foldable:
            text_parts.append(f"[{m.get('role', '?')}]: {m.get('content', '')[:300]}")
        foldable_text = "\n".join(text_parts)

        compact_prompt = (
            f"{retention_prefix}"
            "将以下 Agent 对话历史压缩为结构化摘要。保留:\n"
            "- 关键决策和原因\n"
            "- 涉及的文件路径\n"
            "- 错误和修复方法\n"
            "- 未完成的待办事项\n\n"
            f"对话历史:\n{foldable_text}\n\n"
            "摘要:"
        )

        try:
            routed = hasattr(llm_provider, "stream_session_events")
            if task_budget is not None and not routed:
                task_budget.begin_provider_call(budget_process_id)
            def before_attempt(_index, _client):
                if task_budget is not None:
                    task_budget.begin_provider_call(budget_process_id)
                return 60
            def notify(notice):
                if on_provider_notice is not None:
                    on_provider_notice(notice)
                else:
                    session.host_ledger.append(notice)
            summary = llm_provider.chat(
                [{"role": "user", "content": compact_prompt}],
                max_tokens=min(2048, int(getattr(
                    llm_provider, "max_output_tokens", 2048,
                ))),
                timeout=60, preserve_provider_state=True,
                cancel_event=cancel_event,
                max_retries=0,
                **({"before_attempt": before_attempt, "on_notice": notify}
                   if routed else {}),
            )
            if isinstance(summary, dict):
                provider_state = {
                    key: summary[key] for key in (
                        "response_id", "reasoning_content", "reasoning_details",
                        "provider_artifacts",
                    ) if summary.get(key) is not None
                }
                if provider_state:
                    session.host_ledger.append({
                        "event": "context_compaction_provider_state",
                        "context_epoch": session.context_epoch,
                        "provider_state": provider_state,
                    })
                if summary.get("usage"):
                    if task_budget is not None:
                        task_budget.record_provider_usage(summary["usage"])
                    session.record_provider_usage(
                        summary["usage"],
                        protocol=str(getattr(
                            getattr(llm_provider, "protocol", ""), "value",
                            getattr(llm_provider, "protocol", ""),
                        )),
                    )
                summary = str(summary.get("content", ""))
            if not isinstance(summary, str) or not summary.strip():
                self.last_compaction_error = "empty_compaction_summary"
                return False
            if task_budget is not None:
                task_budget.consume_output(str(summary), budget_process_id)

            # 保留不可压缩前缀 + 摘要 + 最近 3 条
            prefix = ""
            if rejection_instructions:
                prefix = "## 不可忘记的纠正指令\n" + "\n".join(
                    f"- {r}" for r in rejection_instructions
                ) + "\n\n"

            session.begin_context_epoch(
                summary=f"{prefix}[上下文摘要]\n{summary}",
                retained_messages=list(messages[-3:]),
                reason="force_compact_watermark",
            )
            return True
        except Exception as exc:
            from backend.core.loop.budget import TaskBudgetExceeded
            from backend.core.loop.llm import StreamCancelledError
            if isinstance(exc, (TaskBudgetExceeded, StreamCancelledError)):
                raise
            self.last_compaction_error = f"{type(exc).__name__}: {exc}"[:500]
            return False

    def force_compact(self, session: "AgentSession", *, reason: str) -> bool:
        """Start a bounded epoch after explicit user approval.

        This path deliberately does not call a model. The full prior epoch stays
        in the local audit archive, while provider-visible history keeps the
        immutable ROM/task prefix plus a small host-authored checkpoint. It is
        destructive only from the provider context's point of view.
        """
        messages = list(session.messages)
        if not messages:
            self.last_compaction_error = "empty_session"
            return False
        prefix = [item for item in messages if item.get("message_type") in {
            "compiled_system_prompt", "compiled_task_contract",
        }][:2]
        prefix_chars = sum(len(str(item.get("content") or "")) for item in prefix)
        checkpoint_chars = min(16000, max(0, int(self._limit * 4 * 0.5) - prefix_chars - 1000))
        if checkpoint_chars < 256:
            self.last_compaction_error = "immutable_prefix_exceeds_context_budget"
            return False
        latest_user = next((
            str(item.get("content", "")) for item in reversed(messages)
            if item.get("role") == "user" and item.get("message_type", "") in {"", "conversation"}
        ), "")
        latest_assistant = next((
            str(item.get("content", "")) for item in reversed(messages)
            if item.get("role") == "assistant" and item.get("content")
        ), "")
        summary = (
            "[HOST FORCED COMPACTION — USER APPROVED]\n"
            f"Reason: {reason}\n"
            f"Removed provider-visible messages: {len(messages)}. "
            "The complete prior epoch remains in the local audit archive.\n"
            f"Latest user direction: {latest_user[:checkpoint_chars // 2]}\n"
            f"Latest assistant state: {latest_assistant[:checkpoint_chars // 2]}"
        )
        session.begin_context_epoch(
            summary=summary,
            retained_messages=[],
            reason="user_approved_force_compact",
        )
        self.last_compaction_error = ""
        return True

    def check_budget_continuity(self, session: "AgentSession",
                                window_size: int = 5) -> dict:
        """检测最近 N 轮 LLM 响应是否存在 token budget 延续浪费。

        判断标准:
        - 最近 N 条 assistant 消息长度变异系数 (CV) < 0.2
        - 且无 <tool_call> 或 TASK_COMPLETE 标记
        → 视为停滞循环，建议提前终止或 compact。

        Returns:
            {"stagnant": bool, "cv": float, "avg_tokens": float, "rounds": int}
        """
        assistant_msgs = [
            m for m in session.messages
            if m.get("role") == "assistant"
        ]
        if len(assistant_msgs) < window_size:
            return {"stagnant": False, "cv": 0.0, "avg_tokens": 0.0, "rounds": 0}

        recent = assistant_msgs[-window_size:]
        lengths = [len(m.get("content", "")) for m in recent]

        avg = sum(lengths) / len(lengths)
        if avg < 50:
            return {"stagnant": False, "cv": 0.0, "avg_tokens": 0.0, "rounds": 0}

        variance = sum((l - avg) ** 2 for l in lengths) / len(lengths)
        std = variance ** 0.5
        cv = std / avg if avg > 0 else 0

        # Check for tool calls or completion in the window
        from backend.core.loop.loop_guard import _is_completion
        has_action = any(
            bool((session.provider_state.get(m.get("provider_state_id"), {}) or {}).get("tool_calls"))
            or _is_completion(m.get("content", ""))
            for m in recent
        )

        stagnant = cv < 0.2 and not has_action

        return {
            "stagnant": stagnant,
            "cv": round(cv, 3),
            "avg_tokens": round(avg, 3),
            "rounds": len(recent),
        }




# ═══════════════════════════════════════════════════════════════
# v0.36: Context Management — manage() + 压缩优先级链
# ═══════════════════════════════════════════════════════════════




# ── manage() 统一入口 ─────────────────────────────────────

def manage_context(session, harness_data, llm_provider,
                   dep_graph=None, task_files=None,
                   model_token_limit: int | None = None):
    """Decide whether to start a low-frequency context GC epoch.

    Phase 2 stops mutating sent history at intermediate watermarks.  Routing,
    addressable context and memo receipts keep the active log small; only the
    force watermark creates an auditable new epoch.
    """
    tokens = session.estimate_tokens()
    budget = int(model_token_limit or getattr(
        session, "model_context_limit", 128000,
    ))

    if tokens > budget * 0.9:
        return True

    return False


# ── 50%: 隐用户输入回收 ───────────────────────────────────



# ── 70%: Tool Result Snip ─────────────────────────────────



# ── 80%: 依赖图过滤 ──────────────────────────────────────





# ── 85%: 知识替代 ────────────────────────────────────────



# ── Retention 多源合成 ────────────────────────────────────


# Import compatibility only; never used by the active context path.
from .legacy_context_filters import (
    ContextConstants, _dep_graph_filter, _graph_distance, _recycle_governance_nudges, _replace_with_lesson_transcripts, _resolve_retention, _retention_priority, _snip_old_tool_results
)
