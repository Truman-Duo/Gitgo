"""File-level facts: frequent modification, co-change, delete-restore patterns.

v0.33 E1-fix: time-window gating — consecutive_policy_warnings requires
             ≥3 warnings within 1 hour, not just "last 3" regardless of time.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class Fact:
    fact_id: str
    fact_type: str
    summary: str
    related_events: list[str] = field(default_factory=list)
    derived_at: str = ""
    project_name: str = ""
    severity: str = "medium"


def _parse_ts(entry) -> datetime | None:
    """从 HistoryEntry 解析 timestamp，失败返回 None。"""
    ts = getattr(entry, "timestamp", "")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _in_time_window(events: list, window_hours: float, now_ts: datetime) -> bool:
    """检查 events 的时间跨度是否在 window_hours 内。"""
    if len(events) < 2:
        return False
    timestamps = []
    for e in events:
        t = _parse_ts(e)
        if t:
            timestamps.append(t)
    if len(timestamps) < 2:
        return False
    return (now_ts - min(timestamps)) <= timedelta(hours=window_hours)


def derive_file_facts(entries: list, project_name: str,
                      derived_at: str) -> list[Fact]:
    """从 scan event 提取文件级 pattern。"""
    facts = []

    try:
        now = datetime.fromisoformat(derived_at)
    except (ValueError, TypeError):
        now = datetime.now()

    # ── Consecutive policy warnings: ≥3 within 1 hour ──
    policies = [e for e in entries if e.operation == "policy_check_result"]
    if len(policies) >= 3:
        recent = policies[-3:]
        if all(p.status == "warning" for p in recent):
            if _in_time_window(recent, 1.0, now):
                facts.append(Fact(
                    fact_id=f"fact_consecutive_policy_warnings_{project_name}",
                    fact_type="consecutive_policy_warnings",
                    summary=(
                        f"最近 {len(recent)} 次 policy check 连续 warning"
                        f"（1 小时内）"
                    ),
                    related_events=[p.correlation_id for p in recent],
                    derived_at=derived_at,
                    project_name=project_name,
                    severity="high",
                ))

    return facts
