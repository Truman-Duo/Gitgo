"""Fact Engine — Event → Fact derivation layer.

从 HistoryManager 读取原始 event，计算高频 pattern 的 fact，写回 HistoryManager。
fact 通过 correlation_id 精确关联源 event，不做模糊匹配。
"""

from datetime import datetime
from backend.core.fact.file_patterns import Fact, derive_file_facts
from backend.core.fact.workflow_patterns import derive_workflow_facts
from backend.core.fact.contract_patterns import derive_contract_facts

__all__ = ["Fact", "derive_facts"]


def derive_facts(project_name: str) -> list[Fact]:
    """从 HistoryManager 读取最新 event，计算新的 fact。去重，返回新增的 fact 列表。"""
    from backend.core.history import HistoryManager

    entries = HistoryManager.load()
    recent = [e for e in entries if e.project_name == project_name][-50:]
    derived_at = datetime.now().isoformat()

    facts: list[Fact] = []
    facts.extend(derive_file_facts(recent, project_name, derived_at))
    facts.extend(derive_workflow_facts(recent, project_name, derived_at))
    facts.extend(derive_contract_facts(recent, project_name, derived_at))

    # 去重：已存在 fact_derived event 的不再生成
    existing_ids = set()
    for e in entries:
        if e.operation == "fact_derived" and isinstance(e.detail, dict):
            fid = e.detail.get("fact_id", "")
            if fid:
                existing_ids.add(fid)
    new_facts = [f for f in facts if f.fact_id not in existing_ids]

    # 写回 HistoryManager（用 correlation_id 精确关联源 event）
    for f in new_facts:
        HistoryManager.add_operation(
            project_name, "fact_derived", "success",
            {"fact_id": f.fact_id, "fact_type": f.fact_type,
             "summary": f.summary,
             "related_events": f.related_events,
             "severity": f.severity},
            correlation_id=f"fact_{f.fact_id}",
        )

    return new_facts
