"""Workflow-level facts: rejection chains, exploration patterns, burst syncs."""

from backend.core.fact.file_patterns import Fact


def derive_workflow_facts(entries: list, project_name: str,
                          derived_at: str) -> list[Fact]:
    """从 workflow event 提取 pattern。"""
    facts = []

    # Rejection chain: ≥3 consecutive rejections
    rejections = [e for e in entries if e.operation == "rejection"]
    if len(rejections) >= 3:
        recent_3 = rejections[-3:]
        reasons = []
        for r in recent_3:
            d = r.detail if isinstance(r.detail, dict) else {}
            reasons.append(d.get("reason", ""))
        facts.append(Fact(
            fact_id=f"fact_rejection_chain_{project_name}_{len(rejections)}",
            fact_type="rejection_chain",
            summary=f"连续 {len(rejections)} 次 rejection: {'; '.join(reasons[-2:])}",
            related_events=[r.correlation_id for r in recent_3],
            derived_at=derived_at,
            project_name=project_name,
            severity="high",
        ))

    # Burst formalize: ≥5 formalize in a short window
    formalizes = [e for e in entries if e.operation == "formalize"]
    if len(formalizes) >= 5:
        recent_f = formalizes[-5:]
        facts.append(Fact(
            fact_id=f"fact_burst_formalize_{project_name}",
            fact_type="burst_formalize",
            summary=f"短时间内 {len(recent_f)} 次 formalize，可能是试错模式",
            related_events=[f.correlation_id for f in recent_f],
            derived_at=derived_at,
            project_name=project_name,
            severity="medium",
        ))

    return facts
