"""变更模式检测 — 从 Operation History 检测共变模块、commit 类型聚类、trial 后续影响。"""
from __future__ import annotations

from collections import Counter

from backend.core.history import HistoryManager


def detect_co_changing(project_name: str) -> list[dict]:
    """检测共变模块：哪些目录倾向于在同一个 formal commit 中一起变更。

    从 formalize detail 的 files_changed 中提取，按顶层目录聚合。
    返回按 co_occurrence 降序排列的配对列表。
    """
    entries = HistoryManager.load()
    formalize_entries = [
        e for e in entries
        if e.project_name == project_name and e.operation == "formalize"
    ]

    total_formal = len(formalize_entries)
    if total_formal == 0:
        return []

    # 提取每个 formal commit 的顶层目录集合
    dir_sets: list[set[str]] = []
    for fe in formalize_entries:
        files = (fe.detail or {}).get("files_changed", [])
        dirs = set()
        for f in files:
            p = f.get("path", "")
            d = p.split("/")[0] if "/" in p else "(root)"
            dirs.add(d)
        if len(dirs) >= 2:
            dir_sets.append(dirs)

    # 统计每对目录的共现次数
    pair_counts: Counter[tuple[str, str]] = Counter()
    for ds in dir_sets:
        dirs = sorted(ds)
        for i in range(len(dirs)):
            for j in range(i + 1, len(dirs)):
                pair_counts[(dirs[i], dirs[j])] += 1

    result = []
    for (a, b), count in pair_counts.most_common(20):
        result.append({
            "modules": [a, b],
            "co_occurrence": count,
            "total_formal": total_formal,
        })
    return result


def detect_type_clusters(project_name: str) -> list[dict]:
    """检测 commit 类型聚类：formal commit 的类型分布及多源合并模式。

    从 formalize detail 的 commit 字段提取类型，
    统计每种类型的 formalize 次数和平均 source_indices 数量。
    """
    entries = HistoryManager.load()
    formalize_entries = [
        e for e in entries
        if e.project_name == project_name and e.operation == "formalize"
    ]

    if not formalize_entries:
        return []

    type_stats: dict[str, dict] = {}
    for fe in formalize_entries:
        detail = fe.detail or {}
        commit_tag = detail.get("commit", "")
        ct = _parse_commit_type(commit_tag)
        sources = detail.get("source_indices", [])
        source_count = len(sources) if sources else 1

        if ct not in type_stats:
            type_stats[ct] = {"total": 0, "source_counts": [], "multi_source": 0}
        type_stats[ct]["total"] += 1
        type_stats[ct]["source_counts"].append(source_count)
        if source_count > 1:
            type_stats[ct]["multi_source"] += 1

    result = []
    for ct, stats in sorted(type_stats.items()):
        sc = stats["source_counts"]
        avg_sources = round(sum(sc) / len(sc), 2) if sc else 0
        result.append({
            "type": ct,
            "count": stats["total"],
            "avg_sources": avg_sources,
            "multi_source_ratio": round(stats["multi_source"] / stats["total"], 2),
        })
    return result


def detect_trial_impact(project_name: str) -> dict:
    """检测 Trial 后续影响：accept 后触发 workspace 变更的概率。

    按 correlation_id 关联：triage_accept 后同一 session 内的 scan
    若检测到 entries_changed > 0，计为"触发了 workspace 变更"。
    """
    entries = HistoryManager.load()
    entries = [e for e in entries if e.project_name == project_name]

    # 收集所有 triage_accept 的 correlation_id
    accept_cids = {
        e.correlation_id
        for e in entries
        if e.operation == "triage_accept" and e.correlation_id
    }
    total_accepted = len(accept_cids)
    if total_accepted == 0:
        return {"total_accepted": 0, "triggered_workspace_change": 0,
                "avg_trigger_rate": 0.0}

    # 检查每个 accept 后的 scan 是否检测到变更
    triggered = 0
    for cid in accept_cids:
        # 同 correlation_id 下，在 triage_accept 之后的 scan
        session_entries = sorted(
            [e for e in entries if e.correlation_id == cid],
            key=lambda e: e.timestamp,
        )
        accept_ts = None
        for se in session_entries:
            if se.operation == "triage_accept":
                accept_ts = se.timestamp
            if accept_ts and se.operation == "scan" and se.timestamp >= accept_ts:
                changed = (se.detail or {}).get("entries_changed", 0)
                if changed > 0:
                    triggered += 1
                    break

    return {
        "total_accepted": total_accepted,
        "triggered_workspace_change": triggered,
        "avg_trigger_rate": round(triggered / total_accepted, 2),
    }


def build_patterns_report(project_name: str) -> dict:
    """聚合三种模式检测，返回完整报告。"""
    return {
        "project": project_name,
        "co_changing_modules": detect_co_changing(project_name),
        "commit_type_clusters": detect_type_clusters(project_name),
        "trial_impact": detect_trial_impact(project_name),
    }


def _parse_commit_type(commit_tag: str) -> str:
    """从 '[PREFIX-123] type: subject' 中提取 commit type。"""
    rest = commit_tag.split("] ", 1)[-1] if "] " in commit_tag else commit_tag
    if ":" in rest:
        return rest.split(":")[0].strip()
    return "unknown"
