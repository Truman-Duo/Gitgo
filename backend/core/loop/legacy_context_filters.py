"""Retired history-mutation filters kept only for import compatibility.

The production context manager never calls these helpers. New behavior belongs
to append-only epochs and the addressable context store.
"""
from __future__ import annotations

def _retention_priority(message: dict | str, harness: dict) -> float:
    """计算消息的保留优先级（0-1）。

    兼容两种 harness 格式:
    - 新格式: {"signals": [GovernanceSignal, ...], "brief": "..."}
    - 旧格式: {"lesson_triggers": [...], "contract_drift": [...], ...}
    """
    msg = message if isinstance(message, dict) else {"content": str(message or "")}
    if not harness or not msg.get("content"):
        return 0.3

    # 新格式：从 GovernanceSignal 列表计算优先级
    signals = harness.get("signals")
    if signals is not None:
        from backend.core.loop.harness.retention import retention_priority_from_signals
        return retention_priority_from_signals(msg, signals)

    # 旧格式兼容
    score = 0.3

    signal_ids = {str(item) for item in (msg.get("governance_signal_ids", []) or [])}
    referenced_files = {str(item) for item in (msg.get("referenced_files", []) or [])}
    semantic_tags = {str(item) for item in (msg.get("semantic_tags", []) or [])}

    rejection_ids = {
        str(item) for item in (harness.get("rejection_signal_ids", []) or [])
    }
    if signal_ids.intersection(rejection_ids):
        return 1.0

    for lt in harness.get("lesson_triggers", []):
        fname = lt.get("file", "")
        if fname and fname in referenced_files:
            score = max(score, 0.8)

    for d in harness.get("contract_drift", []):
        fname = d.get("file", "")
        if fname and fname in referenced_files:
            score = max(score, 0.7)

    for cf in harness.get("critical_features", []):
        if cf in semantic_tags:
            score = max(score, 0.6)

    return score


class ContextConstants:
    """上下文管理常量。可通过 contract.yaml context.* 覆盖。"""

    TURN_UNIT = "assistant_message"  # 一"轮" = 一次 assistant 响应
    SNIP_AGE_TURNS = 3               # tool_result 超过 N 轮 → snip
    NUDGE_TTL_TURNS = 5              # pending nudge 超过 N 轮 → orphan
    MAX_NUDGE_REPEAT = 3             # 同 nudge 重复注入上限 → 升级 A
    DEP_GRAPH_HOP_WEIGHTS = {0: 0.9, 1: 0.9, 2: 0.6}  # default=0.3
    CONTEXT_BUDGET_RESERVED = 3000


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


def _replace_with_lesson_transcripts(session, harness_data) -> int:
    """用 lesson 的紧凑格式替代原文对话。lesson 远小于原文。

    Returns: 替代的消息数。
    """
    lessons = harness_data.get("lessons", [])
    if not lessons:
        return 0
    replaced = 0

    for msg in session.messages:
        linked_lesson_ids = {
            str(item) for item in (msg.get("lesson_ids", []) or []) if str(item)
        }
        if not linked_lesson_ids:
            continue
        for lesson in lessons:
            lesson_id = str(getattr(lesson, "id", ""))
            if lesson_id and lesson_id in linked_lesson_ids:
                # 替代为紧凑格式
                trigger = getattr(lesson, "trigger", "")
                msg["content"] = (
                    f"[lesson {lesson_id}] "
                    f"trigger={trigger} "
                    f"rule={getattr(lesson, 'rule', '')[:80]}"
                )
                msg["_replaced_by_lesson"] = True
                replaced += 1
                break

    return replaced


def _resolve_retention(msg: dict, harness_data=None) -> float:
    """取各 filter 的 max: 任一 filter 认为重要就保留。"""
    return max(
        msg.get("_retention_override", 0.3),
        msg.get("_retention_priority", 0.0),       # RetentionAdvisor
        msg.get("_nudge_priority", 0.0),            # pending nudge
    )


