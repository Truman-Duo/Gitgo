"""Contract-level facts: repeated drift, architecture violations, dependency breaks."""

from backend.core.fact.file_patterns import Fact


def derive_contract_facts(entries: list, project_name: str,
                          derived_at: str) -> list[Fact]:
    """从 contract/drift event 提取合约级 pattern。"""
    facts = []

    drifts = [e for e in entries if e.operation == "governance_drift"]
    if len(drifts) >= 5:
        rules = []
        for d in drifts[-5:]:
            det = d.detail if isinstance(d.detail, dict) else {}
            rules.append(det.get("rule", "?"))
        facts.append(Fact(
            fact_id=f"fact_repeated_drift_{project_name}",
            fact_type="repeated_contract_drift",
            summary=f"最近 {len(drifts)} 次 contract drift，主要规则: {', '.join(set(rules[-3:]))}",
            related_events=[d.correlation_id for d in drifts[-5:]],
            derived_at=derived_at,
            project_name=project_name,
            severity="high",
        ))

    return facts
