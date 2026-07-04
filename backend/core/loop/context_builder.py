"""Context Builder — 在 Phase 边界构建治理简报，注入 A 级 Agent context。

四个纯函数，不调 LLM，只做数据读取 + 文本拼接。总计 800-1500 token。
"""

from __future__ import annotations
from pathlib import Path


def build_governance_brief(
    project_name: str,
    workspace_path: str | Path,
    changed_files: list[str] | None = None,
) -> dict:
    """在 Phase 边界构建治理简报。

    返回值是 flat dict，每个 value 是 LLM 可读的浓缩文本。
    """
    return {
        "phase_brief": _phase_brief(project_name),
        "contract_summary": _contract_summary(project_name, workspace_path, changed_files or []),
        "lesson_matches": _lesson_matches(project_name),
        "rejection_history": _rejection_history(project_name),
    }


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

    # 如果有变更文件列表，只展示相关 feature
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


def _lesson_matches(project_name: str) -> str:
    """从 policy_check_result event 读最近一次匹配的 lesson。"""
    from backend.core.history import HistoryManager
    entries = HistoryManager.load()
    checks = [
        e for e in entries
        if e.project_name == project_name
        and e.operation == "policy_check_result"
    ]
    if not checks:
        return ""

    last_check = checks[-1]
    detail = last_check.detail or {}
    triggers = detail.get("lesson_triggers", [])
    if not triggers:
        return ""

    lines = [f"匹配到的 lesson ({len(triggers)} 条):"]
    for t in triggers[:5]:
        severity = t.get("severity", "?")
        rule = t.get("rule", "")[:120]
        lines.append(f"  [{severity}] {rule}")
    return "\n".join(lines)


def _rejection_history(project_name: str) -> str:
    """从 rejection event 读最近 3 条的 reason + instruction。"""
    from backend.core.history import HistoryManager
    entries = HistoryManager.load()
    rejections = [
        e for e in entries
        if e.project_name == project_name
        and e.operation == "rejection"
    ]
    if not rejections:
        return ""

    recent = rejections[-3:]
    lines = ["近期被拒记录:"]
    for i, r in enumerate(recent, 1):
        detail = r.detail or {}
        reason = detail.get("reason", "")[:100]
        instruction = detail.get("instruction", "")[:100]
        lines.append(f"  #{i} 原因: {reason}")
        if instruction:
            lines.append(f"     纠正: {instruction}")
    return "\n".join(lines)
