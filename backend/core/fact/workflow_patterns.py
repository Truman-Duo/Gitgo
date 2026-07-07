"""Workflow-level facts: rejection chains, exploration patterns, burst syncs.

v0.33 E1-fix: time-window gating.
    - rejection_chain: ≥3 consecutive rejections within 24 hours
    - burst_formalize: ≥5 formalize in ≤1 hour
"""

from datetime import datetime, timedelta

from backend.core.fact.file_patterns import Fact, _parse_ts, _in_time_window


def derive_workflow_facts(entries: list, project_name: str,
                          derived_at: str) -> list[Fact]:
    """从 workflow event 提取 pattern。"""
    facts = []

    try:
        now = datetime.fromisoformat(derived_at)
    except (ValueError, TypeError):
        now = datetime.now()

    # ── Rejection chain: ≥3 consecutive within 24 hours ──
    rejections = [e for e in entries if e.operation == "rejection"]
    if len(rejections) >= 3:
        recent_3 = rejections[-3:]
        if _in_time_window(recent_3, 24.0, now):
            reasons = []
            for r in recent_3:
                d = r.detail if isinstance(r.detail, dict) else {}
                reasons.append(d.get("reason", ""))
            facts.append(Fact(
                fact_id=f"fact_rejection_chain_{project_name}",
                fact_type="rejection_chain",
                summary=(
                    f"24 小时内连续 {len(recent_3)} 次 rejection:"
                    f" {'; '.join(reasons[-2:])}"
                ),
                related_events=[r.correlation_id for r in recent_3],
                derived_at=derived_at,
                project_name=project_name,
                severity="high",
            ))

    # ── Burst formalize: ≥5 within 1 hour ──
    formalizes = [e for e in entries if e.operation == "formalize"]
    if len(formalizes) >= 5:
        recent_f = formalizes[-5:]
        if _in_time_window(recent_f, 1.0, now):
            facts.append(Fact(
                fact_id=f"fact_burst_formalize_{project_name}",
                fact_type="burst_formalize",
                summary=f"1 小时内 {len(recent_f)} 次 formalize，可能是试错模式",
                related_events=[f.correlation_id for f in recent_f],
                derived_at=derived_at,
                project_name=project_name,
                severity="medium",
            ))

    return facts
