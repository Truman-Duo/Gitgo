"""建议质量度量 — 分析 AI 建议（ai_proposal）与人类决策（human_decision）的差异。

仅使用 indices Jaccard 重叠度判断采纳/修改/拒绝，不做 message 文本比较。
"""
from __future__ import annotations

from backend.core.history import HistoryManager


def load_suggestion_pairs(project_name: str) -> list[dict]:
    """提取所有 suggest_* 条目及其对应的执行记录，按 correlation_id 配对。

    返回: [{"suggest_type": "formalize", "ai_proposal": {...},
            "human_decision": {...}, "correlation_id": "..."}]
    """
    entries = HistoryManager.load()
    entries = [e for e in entries if e.project_name == project_name]

    suggests = [e for e in entries if e.operation.startswith("suggest_")]
    executions = {
        e.correlation_id: e
        for e in entries
        if e.correlation_id
        and e.operation in (
            "formalize", "triage_accept", "triage_promote", "triage_discard",
        )
    }

    pairs = []
    for s in suggests:
        suggest_type = s.operation.replace("suggest_", "")
        ai_proposal = (s.detail or {}).get("ai_proposal")
        if not ai_proposal:
            continue

        # 优先从同一条记录的 human_decision 获取（add_suggestion 直存模式）
        human_decision = (s.detail or {}).get("human_decision")

        # 否则按 correlation_id 匹配执行记录
        if not human_decision and s.correlation_id and s.correlation_id in executions:
            exec_entry = executions[s.correlation_id]
            human_decision = _extract_decision(exec_entry, suggest_type)

        if human_decision:
            pairs.append({
                "suggest_type": suggest_type,
                "ai_proposal": ai_proposal,
                "human_decision": human_decision,
                "correlation_id": s.correlation_id,
            })

    return pairs


def _extract_decision(entry, suggest_type: str) -> dict | None:
    """从执行记录的 detail 中提取 human_decision 结构。"""
    detail = entry.detail or {}
    if suggest_type == "formalize" and entry.operation == "formalize":
        return {
            "indices": detail.get("source_indices", []),
            "commit": detail.get("commit", ""),
        }
    if suggest_type == "triage" and entry.operation.startswith("triage_"):
        action = entry.operation.replace("triage_", "")
        return {"index": detail.get("trial_hash", ""), "action": action}
    return None


def compute_quality_metrics(pairs: list[dict]) -> dict:
    """聚合计算采纳率/修改率/拒绝率。仅用 indices Jaccard 重叠度。"""
    if not pairs:
        return _empty_report()

    formalize_pairs = [p for p in pairs if p["suggest_type"] == "formalize"]
    triage_pairs = [p for p in pairs if p["suggest_type"] == "triage"]

    result = {
        "suggestion_count": len(pairs),
        "by_type": {},
    }

    if formalize_pairs:
        verdicts = [_judge_formalize(p) for p in formalize_pairs]
        result["by_type"]["formalize"] = _aggregate_verdicts(verdicts)
        result["by_type"]["formalize"]["total"] = len(formalize_pairs)

    if triage_pairs:
        verdicts = [_judge_triage(p) for p in triage_pairs]
        result["by_type"]["triage"] = _aggregate_verdicts(verdicts)
        result["by_type"]["triage"]["total"] = len(triage_pairs)

    # 按 commit type 切片 — 从 formalize pairs 的 human_decision.commit 解析
    by_commit_type = {}
    for p in formalize_pairs:
        commit = p["human_decision"].get("commit", "")
        ct = _parse_commit_type(commit)
        by_commit_type.setdefault(ct, []).append(_judge_formalize(p))
    result["by_commit_type"] = {
        ct: {"total": len(vs), "acceptance_rate": _acceptance_rate(vs)}
        for ct, vs in by_commit_type.items()
    }

    # 按模块切片 — 从 formalize 记录关联的 files_changed
    by_module = {}
    for p in formalize_pairs:
        files = p["human_decision"].get("files_changed", [])
        for f in files:
            mod = f["path"].split("/")[0] if "/" in f["path"] else "(root)"
            by_module.setdefault(mod, []).append(_judge_formalize(p))
    result["by_module"] = {
        mod: {"total": len(vs), "acceptance_rate": _acceptance_rate(vs)}
        for mod, vs in by_module.items()
    }

    return result


def _judge_formalize(pair: dict) -> dict:
    """判断单个 formalize 建议的采纳程度。"""
    ai = pair["ai_proposal"]
    human = pair["human_decision"]

    ai_indices = set()
    for g in ai.get("groups", []):
        ai_indices.update(g.get("indices", []))

    human_indices = set(human.get("indices", []))
    union = ai_indices | human_indices
    jaccard = len(ai_indices & human_indices) / max(len(union), 1)

    if jaccard >= 0.8:
        verdict = "accepted"
    elif jaccard >= 0.3:
        verdict = "modified"
    else:
        verdict = "rejected"

    return {"verdict": verdict, "index_jaccard": round(jaccard, 3)}


def _judge_triage(pair: dict) -> dict:
    """判断单个 triage 建议的采纳程度。"""
    ai = pair["ai_proposal"]
    human = pair["human_decision"]

    ai_recs = ai.get("recommendations", [])
    ai_actions = {r["index"]: r["action"] for r in ai_recs}

    human_idx = human.get("index", "")
    human_action = human.get("action", "")

    # triage 按单条 hash 关联——human_decision.index 是 trial_hash
    # 遍历 AI 推荐找到匹配的
    for ai_idx, ai_act in ai_actions.items():
        if human_action == ai_act:
            return {"verdict": "accepted"}
        return {"verdict": "modified",
                "ai_action": ai_act, "human_action": human_action}

    return {"verdict": "rejected"}


def _aggregate_verdicts(verdicts: list[dict]) -> dict:
    total = max(len(verdicts), 1)
    accepted = sum(1 for v in verdicts if v["verdict"] == "accepted")
    modified = sum(1 for v in verdicts if v["verdict"] == "modified")
    rejected = sum(1 for v in verdicts if v["verdict"] == "rejected")
    avg_jaccard = (
        sum(v.get("index_jaccard", 0) for v in verdicts) / total
        if verdicts and "index_jaccard" in verdicts[0]
        else None
    )
    result = {
        "accepted": accepted,
        "modified": modified,
        "rejected": rejected,
        "acceptance_rate": round(accepted / total, 2),
        "modification_rate": round(modified / total, 2),
        "rejection_rate": round(rejected / total, 2),
    }
    if avg_jaccard is not None:
        result["avg_index_jaccard"] = round(avg_jaccard, 3)
    return result


def _acceptance_rate(verdicts: list[dict]) -> float:
    if not verdicts:
        return 1.0
    return round(sum(1 for v in verdicts if v["verdict"] == "accepted") / len(verdicts), 2)


def _parse_commit_type(commit_tag: str) -> str:
    """从 '[PREFIX-123] type: subject' 中提取 commit type。"""
    rest = commit_tag.split("] ", 1)[-1] if "] " in commit_tag else commit_tag
    if ":" in rest:
        return rest.split(":")[0].strip()
    return "unknown"


def _empty_report() -> dict:
    return {
        "suggestion_count": 0,
        "by_type": {},
        "by_commit_type": {},
        "by_module": {},
    }


def group_by_commit_type(pairs: list[dict]) -> dict:
    """从 formalize pairs 按 commit type 分组（供外部切片查询）。"""
    result = {}
    for p in pairs:
        if p["suggest_type"] != "formalize":
            continue
        commit = p["human_decision"].get("commit", "")
        ct = _parse_commit_type(commit)
        result.setdefault(ct, []).append(p)
    return {ct: len(ps) for ct, ps in result.items()}


def group_by_module(pairs: list[dict]) -> dict:
    """从 formalize pairs 按变更模块分组（供外部切片查询）。"""
    result = {}
    for p in pairs:
        if p["suggest_type"] != "formalize":
            continue
        files = p["human_decision"].get("files_changed", [])
        for f in files:
            mod = f["path"].split("/")[0] if "/" in f["path"] else "(root)"
            result[mod] = result.get(mod, 0) + 1
    return result
