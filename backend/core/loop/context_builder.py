"""Context Builder — 在 Phase 边界构建治理上下文，注入 A 级 Agent context。

v0.33: 升级为双轨输出
- signals: list[GovernanceSignal] — 结构化信号，供 HarnessPlugin 消费
- brief: str — LLM 可读文本摘要，注入 AgentSession system prompt
"""

from __future__ import annotations
from pathlib import Path


def build_governance_context(
    project_name: str,
    workspace_path: str | Path,
    changed_files: list[str] | None = None,
) -> dict:
    """构建治理上下文：结构化信号 + LLM 文本摘要。

    Returns:
        {"signals": [GovernanceSignal, ...], "brief": str}
    """
    from backend.core.loop.signal_normalizer import SignalNormalizer

    # 收集各来源的原始数据
    policy_results = _get_latest_policy_results(project_name)
    lessons = _get_relevant_lessons(workspace_path)
    rejections = _get_recent_rejections(project_name)
    facts = _get_recent_facts(project_name)

    # 归一化为统一信号
    normalizer = SignalNormalizer()
    signals = normalizer.normalize(
        policy_results=policy_results,
        lessons=lessons,
        rejections=rejections,
        facts=facts,
    )

    # 构建 LLM 文本摘要
    brief = _build_text_brief(signals, project_name, workspace_path, changed_files or [])

    return {"signals": signals, "brief": brief}


def build_governance_brief(
    project_name: str,
    workspace_path: str | Path,
    changed_files: list[str] | None = None,
) -> dict:
    """[兼容] 构建治理简报 — 返回 flat dict，每个 value 是 LLM 可读文本。

    内部调用 build_governance_context()，从中提取文本字段。
    """
    ctx = build_governance_context(project_name, workspace_path, changed_files)
    brief = ctx.get("brief", "")
    signals = ctx.get("signals", [])

    # 从信号中重建历史兼容的四个文本字段
    return {
        "phase_brief": _phase_brief(project_name),
        "contract_summary": _contract_summary(project_name, workspace_path, changed_files or []),
        "lesson_matches": _format_lesson_signals(signals),
        "rejection_history": _format_rejection_signals(signals),
    }


# ── 数据采集 ──────────────────────────────────────────────────

def _get_latest_policy_results(project_name: str) -> dict | None:
    """从 history 读取最近一次 policy_check_result。"""
    from backend.core.history import HistoryManager
    entries = HistoryManager.load()
    checks = [
        e for e in entries
        if e.project_name == project_name
        and e.operation == "policy_check_result"
    ]
    if not checks:
        return None
    return checks[-1].detail or {}


def _get_relevant_lessons(workspace_path: str | Path) -> list:
    """加载与工作区相关的 lesson。"""
    from backend.core.knowledge.lesson import LessonManager
    ws = Path(workspace_path)
    lessons = LessonManager.load_abstract(ws)
    # 尝试加载实例层 lessons（项目名未知时跳过）
    try:
        lessons += LessonManager.load_pending(ws, "")
    except Exception:
        pass
    return lessons


def _get_recent_rejections(project_name: str) -> list[dict]:
    """从 history 读取最近 3 条 rejection。"""
    from backend.core.history import HistoryManager
    entries = HistoryManager.load()
    rejections = [
        e for e in entries
        if e.project_name == project_name
        and e.operation == "rejection"
    ]
    return [
        {"correlation_id": r.correlation_id, "detail": r.detail}
        for r in rejections[-3:]
    ]


def _get_recent_facts(project_name: str) -> list:
    """从 history 读取最近 facts。"""
    from backend.core.history import HistoryManager
    entries = HistoryManager.load()
    fact_events = [
        e for e in entries
        if e.project_name == project_name
        and e.operation == "fact_recorded"
    ]
    facts = []
    for fe in fact_events[-10:]:
        detail = fe.detail or {}
        # 构建轻量 fact 对象供 SignalNormalizer 使用
        fact = type('Fact', (), {
            'fact_id': detail.get('fact_id', ''),
            'fact_type': detail.get('fact_type', ''),
            'severity': detail.get('severity', 'low'),
            'summary': detail.get('summary', ''),
        })()
        facts.append(fact)
    return facts


# ── 文本摘要构建 ──────────────────────────────────────────────

def _build_text_brief(
    signals: list,
    project_name: str,
    workspace_path: str | Path,
    changed_files: list[str],
) -> str:
    """从 GovernanceSignal 列表构建 LLM 可读文本摘要（800-1500 token）。"""
    parts = []

    # Phase brief
    pb = _phase_brief(project_name)
    if pb:
        parts.append(pb)

    # Contract summary
    cs = _contract_summary(project_name, workspace_path, changed_files)
    if cs:
        parts.append(cs)

    # Lesson signals
    ls = _format_lesson_signals(signals)
    if ls:
        parts.append(ls)

    # Rejection signals
    rs = _format_rejection_signals(signals)
    if rs:
        parts.append(rs)

    # Block-level signals summary
    block_signals = [s for s in signals if s.category.value == "block"]
    if block_signals:
        lines = [f"## 阻断级信号 ({len(block_signals)} 条)"]
        for s in block_signals[:5]:
            lines.append(f"- [{s.severity.value}] {s.rule[:120]}")
            if s.suggestion:
                lines.append(f"  建议: {s.suggestion[:120]}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _format_lesson_signals(signals: list) -> str:
    """格式化 lesson_trigger 信号为文本。"""
    lesson_sigs = [s for s in signals if s.source == "lesson_trigger"]
    if not lesson_sigs:
        return ""
    lines = [f"匹配到的 lesson ({len(lesson_sigs)} 条):"]
    for s in lesson_sigs[:5]:
        lines.append(f"  [{s.severity.value}] {s.rule[:120]}")
    return "\n".join(lines)


def _format_rejection_signals(signals: list) -> str:
    """格式化 rejection 信号为文本。"""
    rejection_sigs = [s for s in signals if s.source == "rejection"]
    if not rejection_sigs:
        return ""
    lines = ["近期被拒记录:"]
    for i, s in enumerate(rejection_sigs[:3], 1):
        lines.append(f"  #{i} {s.rule[:120]}")
        if s.suggestion:
            lines.append(f"     纠正: {s.suggestion[:100]}")
    return "\n".join(lines)


# ── 历史兼容：从 history 读文本 ────────────────────────────────

def _phase_brief(project_name: str) -> str:
    """从 tool_executed event 读本 cycle 内 B 级 Agent 产出摘要。"""
    from backend.core.history import HistoryManager
    entries = HistoryManager.load()
    tool_events = [
        e for e in entries
        if e.operation == "tool_executed"
        and e.detail.get("project_name", "") == project_name
    ]
    if not tool_events:
        tool_events = [
            e for e in entries
            if e.operation == "tool_executed"
        ]

    if not tool_events:
        return ""

    recent = tool_events[-20:]
    lines = ["近期工具调用:"]
    for e in recent:
        d = e.detail
        status = "OK" if d.get("allowed") else "DENIED"
        role = d.get("role", "?")
        tool = d.get("tool_name", "?")
        duration = d.get("duration_ms", 0)
        lines.append(f"  [{status}] {role}/{tool} ({duration:.0f}ms)")
    return "\n".join(lines)


def _contract_summary(project_name: str, workspace_path: str | Path,
                      changed_files: list[str]) -> str:
    """从 contract.yaml 读 decided_features，只保留本次变更相关的条目。"""
    from backend.core.contract import ContractManager
    contract = ContractManager.load(Path(workspace_path))
    if contract is None:
        return ""

    features = contract.decided_features
    if not features:
        return ""

    if changed_files:
        related = [f for f in features
                   if any(f.location and cf.startswith(f.location) for cf in changed_files)]
        if related:
            features = related

    lines = [f"已确认特性 ({len(features)} 个):"]
    for f in features[:8]:
        loc = f" @{f.location}" if f.location else ""
        confirmed = f"confirmed x{f.confirmed_count}" if f.confirmed_count else ""
        lines.append(f"  {f.name}{loc} {confirmed}".strip())
    return "\n".join(lines)
