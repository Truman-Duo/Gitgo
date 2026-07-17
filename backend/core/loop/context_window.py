"""Context Window — 三档水位线 + harness 智能保留。

参考 Reasonix compact.go 的三档策略 + gitgo 独有的 harness 信息。

水位线:
- SOFT (50%):  仅通知，保护 prompt cache
- PRUNE (80%): 裁剪旧 tool result（免费，不调 LLM）
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
        self._flags: set[str] = set()

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

        if ratio >= self.SOFT_RATIO and self.SOFT_NOTICED not in self._flags:
            self._flags.add(self.SOFT_NOTICED)
            return {"action": "soft_warn", "tokens": tokens,
                    "usage_ratio": round(ratio, 3), "pruned_count": 0}

        return {"action": "none", "tokens": tokens,
                "usage_ratio": round(ratio, 3), "pruned_count": 0}

    def prune(self, session: "AgentSession",
              harness_data: dict | None = None,
              tail_tokens: int = 16000) -> int:
        """裁剪旧消息（免费操作，不调 LLM）。

        保留策略:
        - system prompt: 永远保留
        - tail_tokens 预算内的最近消息: 保留
        - 中间消息: 按 harness 优先级裁剪

        返回释放的估算 token 数。
        """
        messages = session.messages
        if len(messages) <= 4:
            return 0

        harness = harness_data or {}
        pruned = 0
        kept = []

        # 找到 tail 起始位置（从后往前保留 tail_tokens）
        tail_start = len(messages)
        tail_accum = 0
        for i in range(len(messages) - 1, -1, -1):
            msg_tokens = len(messages[i].get("content", "")) // 4
            if tail_accum + msg_tokens > tail_tokens and i > 0:
                tail_start = i + 1
                break
            tail_accum += msg_tokens

        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = msg.get("content", "")

            # system prompt 永远保留
            if role == "system":
                kept.append(msg)
                continue

            # tail 中的消息保留
            if i >= tail_start:
                kept.append(msg)
                continue

            # 按 harness 优先级决定是否保留
            priority = _retention_priority(content, harness)
            if priority >= 0.7:
                kept.append(msg)
                continue

            # 裁剪: 替换为 elided 标记
            msg_tokens = len(content) // 4
            kept.append({
                "role": role,
                "content": f"[elided {role} message — {msg_tokens} tokens freed]"
            })
            pruned += 1

        if pruned > 0:
            session.messages = kept

        return pruned

    def compact(self, session: "AgentSession",
                llm_provider, harness_data: dict | None = None,
                retention_suggestions: list[str] | None = None) -> bool:
        """LLM 摘要压缩（付费操作，仅 >90% 时触发）。

        将中间消息替换为结构化摘要。返回是否成功。
        """
        # v1: 仅在 >90% 时调用，用 LLM 生成摘要替换中间轮次
        messages = session.messages
        if len(messages) <= 6:
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
            summary = llm_provider.chat(
                [{"role": "user", "content": compact_prompt}],
                max_tokens=2048, timeout=60,
            )

            # 保留不可压缩前缀 + 摘要 + 最近 3 条
            prefix = ""
            if rejection_instructions:
                prefix = "## 不可忘记的纠正指令\n" + "\n".join(
                    f"- {r}" for r in rejection_instructions
                ) + "\n\n"

            new_messages = [messages[0]]  # system prompt
            new_messages.append({
                "role": "user",
                "content": f"{prefix}[上下文摘要]\n{summary}",
            })
            new_messages.extend(messages[-3:])  # 最近 3 条

            session.messages = new_messages
            self._flags.discard(self.SOFT_NOTICED)  # 重置软通知
            return True
        except Exception:
            return False

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
        has_action = any(
            "<tool_call>" in m.get("content", "") or
            "TASK_COMPLETE" in m.get("content", "") or
            "分析完成" in m.get("content", "") or
            "任务完成" in m.get("content", "")
            for m in recent
        )

        stagnant = cv < 0.2 and not has_action

        return {
            "stagnant": stagnant,
            "cv": round(cv, 3),
            "avg_tokens": round(avg, 3),
            "rounds": len(recent),
        }


def _retention_priority(content: str, harness: dict) -> float:
    """计算消息的保留优先级（0-1）。

    兼容两种 harness 格式:
    - 新格式: {"signals": [GovernanceSignal, ...], "brief": "..."}
    - 旧格式: {"lesson_triggers": [...], "contract_drift": [...], ...}
    """
    if not harness or not content:
        return 0.3

    # 新格式：从 GovernanceSignal 列表计算优先级
    signals = harness.get("signals")
    if signals is not None:
        from backend.core.loop.harness.retention import retention_priority_from_signals
        return retention_priority_from_signals(content, signals)

    # 旧格式兼容
    score = 0.3

    for instr in harness.get("rejection_instructions", []):
        if instr[:30] in content:
            return 1.0

    for lt in harness.get("lesson_triggers", []):
        fname = lt.get("file", "")
        if fname and fname in content:
            score = max(score, 0.8)

    for d in harness.get("contract_drift", []):
        fname = d.get("file", "")
        if fname and fname in content:
            score = max(score, 0.7)

    for cf in harness.get("critical_features", []):
        if cf in content:
            score = max(score, 0.6)

    return score


# ═══════════════════════════════════════════════════════════════
# v0.36: Context Management — manage() + 压缩优先级链
# ═══════════════════════════════════════════════════════════════


class ContextConstants:
    """上下文管理常量。可通过 contract.yaml context.* 覆盖。"""

    TURN_UNIT = "assistant_message"  # 一"轮" = 一次 assistant 响应
    SNIP_AGE_TURNS = 3               # tool_result 超过 N 轮 → snip
    NUDGE_TTL_TURNS = 5              # pending nudge 超过 N 轮 → orphan
    MAX_NUDGE_REPEAT = 3             # 同 nudge 重复注入上限 → 升级 A
    DEP_GRAPH_HOP_WEIGHTS = {0: 0.9, 1: 0.9, 2: 0.6}  # default=0.3
    CONTEXT_BUDGET_RESERVED = 3000


# ── manage() 统一入口 ─────────────────────────────────────

def manage_context(session, harness_data, llm_provider,
                   dep_graph=None, task_files=None):
    """压缩优先级链：免费算法 → 最后手段 LLM compact。

    替代裸调 check()→prune()→compact()。
    """
    tokens = session.estimate_tokens()
    budget = 128000  # 默认，可从 contract.yaml 取

    # 50%: 隐用户输入回收
    if tokens > budget * 0.5:
        _recycle_governance_nudges(session)

    # 70%: Tool Result Snip
    if tokens > budget * 0.7:
        _snip_old_tool_results(session)

    # 80%: 依赖图过滤（需 dep_graph + task_files）
    if tokens > budget * 0.8 and dep_graph and task_files:
        _dep_graph_filter(session, dep_graph, task_files)

    # 85%: 知识替代
    if tokens > budget * 0.85:
        _replace_with_lesson_transcripts(session, harness_data)

    # 90%: 最后手段 — 返回 True 表示需要 compact
    if tokens > budget * 0.9:
        return True

    return False


# ── 50%: 隐用户输入回收 ───────────────────────────────────

def _recycle_governance_nudges(session) -> int:
    """回收已响应的治理 nudge。pending 不碰，resolved 降优先级，orphan 降+记日志。

    Returns: 回收的 nudge 数量。
    """
    recycled = 0
    for i, msg in enumerate(session.messages):
        if msg.get("message_type") != "governance_nudge":
            continue

        subsequent = session.messages[i+1:]
        has_response = any(m.get("role") == "assistant" for m in subsequent)

        if has_response:
            # resolved: Agent 已响应 → 可以回收
            msg["_nudge_state"] = "resolved"
            msg["_retention_override"] = 0.1
            recycled += 1

        elif len(subsequent) >= ContextConstants.NUDGE_TTL_TURNS:
            # orphan: Agent 未响应但已过 TTL → 强制回收 + 记录
            msg["_nudge_state"] = "orphan"
            msg["_retention_override"] = 0.1
            recycled += 1
            # 写入 HistoryManager warning
            try:
                from backend.core.history import HistoryManager
                HistoryManager.add_operation(
                    "system", "orphan_nudge", "warning",
                    {"nudge_content": msg.get("content", "")[:200],
                     "ttl_exceeded": True},
                )
            except Exception:
                pass

        else:
            # pending: 仍在等待 Agent 响应 → 不碰
            msg["_nudge_state"] = "pending"

    return recycled


# ── 70%: Tool Result Snip ─────────────────────────────────

def _snip_old_tool_results(session) -> int:
    """替换旧的 tool_result 为占位符。幂等保护。

    Returns: snip 数量。
    """
    assistant_indices = [
        i for i, m in enumerate(session.messages)
        if m.get("role") == "assistant"
    ]
    snipped = 0

    for i, msg in enumerate(session.messages):
        if msg.get("message_type") != "tool_result":
            continue
        if msg.get("_snip_state") == "snipped":
            continue  # 幂等

        # 计算 age: 此 tool_result 之后经过了多少轮 assistant 响应
        turns_after = sum(
            1 for aidx in assistant_indices if aidx > i
        )
        if turns_after >= ContextConstants.SNIP_AGE_TURNS:
            tool_name = msg.get("_tool_name", "unknown")
            msg["content"] = (
                f"[tool {tool_name}: output elided after "
                f"{turns_after} turns — use recall to retrieve]"
            )
            msg["_snip_state"] = "snipped"
            snipped += 1

    return snipped


# ── 80%: 依赖图过滤 ──────────────────────────────────────

def _dep_graph_filter(session, dep_graph: dict,
                      task_files: list[str]) -> int:
    """依赖图打分：消息涉及的文件离 task 文件越远，retention 越低。

    白名单文件 (.gitgo/config.yaml, pyproject.toml, conftest.py) 不受限制。

    Returns: 被降级的消息数。
    """
    WHITELIST = {".gitgo/config.yaml", "pyproject.toml", "conftest.py",
                 ".gitignore", "Makefile"}
    filtered = 0

    for msg in session.messages:
        refs = msg.get("referenced_files", [])
        if not refs:
            continue

        # 白名单豁免
        if any(r in WHITELIST for r in refs):
            msg["_retention_override"] = 0.8
            continue

        # 计算最小跳数
        min_hops = 999
        for ref in refs:
            for tf in task_files:
                hops = _graph_distance(dep_graph, ref, tf)
                if hops < min_hops:
                    min_hops = hops

        override = ContextConstants.DEP_GRAPH_HOP_WEIGHTS.get(min_hops, 0.3)
        msg["_retention_override"] = override
        if override < 0.5:
            filtered += 1

    return filtered


def _graph_distance(dep_graph: dict, file_a: str, file_b: str,
                    max_depth: int = 5) -> int:
    """BFS 计算依赖图距离。"""
    if file_a == file_b:
        return 0
    visited = {file_a}
    frontier = [file_a]
    for depth in range(1, max_depth + 1):
        next_frontier = []
        for node in frontier:
            for neighbor in dep_graph.get(node, {}).get("callers", []):
                # callers 格式: "login.py:handle_login" → 提取文件部分
                nb_file = neighbor.split(":")[0] if ":" in neighbor else neighbor
                if nb_file == file_b:
                    return depth
                if nb_file not in visited:
                    visited.add(nb_file)
                    next_frontier.append(nb_file)
        frontier = next_frontier
    return 999


# ── 85%: 知识替代 ────────────────────────────────────────

def _replace_with_lesson_transcripts(session, harness_data) -> int:
    """用 lesson 的紧凑格式替代原文对话。lesson 远小于原文。

    Returns: 替代的消息数。
    """
    lessons = harness_data.get("lessons", [])
    if not lessons:
        return 0
    replaced = 0

    for msg in session.messages:
        content = msg.get("content", "")
        for lesson in lessons:
            trigger = getattr(lesson, "trigger", "")
            if trigger and trigger in content:
                # 替代为紧凑格式
                msg["content"] = (
                    f"[lesson {getattr(lesson, 'id', '?')}] "
                    f"trigger={trigger} "
                    f"rule={getattr(lesson, 'rule', '')[:80]}"
                )
                msg["_replaced_by_lesson"] = True
                replaced += 1
                break

    return replaced


# ── Retention 多源合成 ────────────────────────────────────

def _resolve_retention(msg: dict, harness_data=None) -> float:
    """取各 filter 的 max: 任一 filter 认为重要就保留。"""
    return max(
        msg.get("_retention_override", 0.3),
        msg.get("_retention_priority", 0.0),       # RetentionAdvisor
        msg.get("_nudge_priority", 0.0),            # pending nudge
    )
