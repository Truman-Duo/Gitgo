"""File-level facts: frequent modification, co-change, delete-restore patterns."""

from dataclasses import dataclass, field


@dataclass
class Fact:
    fact_id: str
    fact_type: str
    summary: str
    related_events: list[str] = field(default_factory=list)
    derived_at: str = ""
    project_name: str = ""
    severity: str = "medium"


def derive_file_facts(entries: list, project_name: str,
                      derived_at: str) -> list[Fact]:
    """从 scan event 提取文件级 pattern。"""
    from collections import Counter

    facts = []
    scans = [e for e in entries if e.operation == "scan"]
    if len(scans) < 3:
        return facts

    # 统计文件变更频率
    file_counts: Counter = Counter()
    for s in scans[-20:]:
        d = s.detail if isinstance(s.detail, dict) else {}
        changed = d.get("entries_changed", 0)
        if changed:
            pass  # scan events don't have per-file detail; use entry-level

    # 从最近的 policy_check_result 的 matched_lessons 找高频模式
    policies = [e for e in entries if e.operation == "policy_check_result"]
    if len(policies) >= 3:
        consecutive_warnings = all(
            p.status == "warning" for p in policies[-3:]
        )
        if consecutive_warnings:
            facts.append(Fact(
                fact_id=f"fact_consecutive_policy_warnings_{project_name}",
                fact_type="consecutive_policy_warnings",
                summary=f"最近 {len(policies[-3:])} 次 policy check 连续 warning",
                related_events=[p.correlation_id for p in policies[-3:]],
                derived_at=derived_at,
                project_name=project_name,
                severity="high",
            ))

    return facts
