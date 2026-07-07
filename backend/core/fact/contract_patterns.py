"""Contract-level facts: repeated drift, architecture violations, dependency breaks.

v0.33 E1-fix: time-window gating — repeated_contract_drift requires
             ≥5 governance_drift events within 24 hours.
"""

from datetime import datetime

from backend.core.fact.file_patterns import Fact, _in_time_window


def derive_contract_facts(entries: list, project_name: str,
                          derived_at: str) -> list[Fact]:
    """从 contract/drift event 提取合约级 pattern。"""
    facts = []

    try:
        now = datetime.fromisoformat(derived_at)
    except (ValueError, TypeError):
        now = datetime.now()

    # ── Repeated contract drift: ≥5 within 24 hours ──
    drifts = [e for e in entries if e.operation == "governance_drift"]
    if len(drifts) >= 5:
        recent = drifts[-5:]
        if _in_time_window(recent, 24.0, now):
            rules = []
            for d in recent:
                det = d.detail if isinstance(d.detail, dict) else {}
                rules.append(det.get("rule", "?"))
            facts.append(Fact(
                fact_id=f"fact_repeated_drift_{project_name}",
                fact_type="repeated_contract_drift",
                summary=(
                    f"24 小时内 {len(recent)} 次 contract drift，"
                    f"主要规则: {', '.join(set(rules[-3:]))}"
                ),
                related_events=[d.correlation_id for d in recent],
                derived_at=derived_at,
                project_name=project_name,
                severity="high",
            ))

    return facts
