"""语义变更图 — 从 formal commit 记录构建关联图。

节点: formal commit（来自 formalize + triage_accept 记录）
边:
  - file_overlap: 两个 formal commit 修改了相同文件 (Jaccard ≥ 0.3)
  - same_push: 两个 formal commit 被同一次 push 发布
  - trial_source: formal commit 通过 accept trial incoming 产生
"""
from __future__ import annotations

from backend.core.history import HistoryManager


def _parse_commit_id(commit_field: str) -> str:
    """从 '[PREFIX-N] type: subject' 中提取节点 ID '[PREFIX-N]'。"""
    if "] " in commit_field:
        return commit_field.split("] ")[0] + "]"
    return commit_field


def build_graph(project_name: str) -> dict:
    """从 HistoryManager 读取所有 formalize/triage_accept/push 记录，构建 nodes + edges。"""
    entries = HistoryManager.load()
    entries = [e for e in entries if e.project_name == project_name]

    # 收集节点
    nodes: list[dict] = []
    node_ids: set[str] = set()

    # formalize 条目 → formal 节点
    formalize_entries = [e for e in entries if e.operation == "formalize"]
    for fe in formalize_entries:
        detail = fe.detail or {}
        commit_id = _parse_commit_id(detail.get("commit", ""))
        if not commit_id or commit_id in node_ids:
            continue
        node_ids.add(commit_id)
        nodes.append({
            "id": commit_id,
            "type": "formal",
            "files_changed": [f["path"] for f in detail.get("files_changed", [])],
            "source_commits": len(detail.get("source_indices", [])),
            "created_at": fe.timestamp,
            "correlation_id": fe.correlation_id,
        })

    # triage_accept 条目 → incoming 节点（accept 产生 is_incoming formal commit）
    accept_entries = [e for e in entries if e.operation == "triage_accept"]
    accept_nodes: list[dict] = []
    for ae in accept_entries:
        detail = ae.detail or {}
        trial_hash = detail.get("trial_hash", "")
        node_id = f"incoming:{trial_hash[:12]}"
        if node_id in node_ids:
            continue
        node_ids.add(node_id)
        node = {
            "id": node_id,
            "type": "incoming",
            "trial_hash": trial_hash,
            "message": detail.get("trial_message", ""),
            "created_at": ae.timestamp,
            "correlation_id": ae.correlation_id,
        }
        accept_nodes.append(node)
        nodes.append(node)

    # 构建边
    edges: list[dict] = []

    # 1. file_overlap 边：formal 节点间的 Jaccard 文件重叠
    formal_nodes = [n for n in nodes if n["type"] == "formal"]
    for i in range(len(formal_nodes)):
        for j in range(i + 1, len(formal_nodes)):
            a_files = set(formal_nodes[i].get("files_changed", []))
            b_files = set(formal_nodes[j].get("files_changed", []))
            if not a_files or not b_files:
                continue
            intersection = a_files & b_files
            if not intersection:
                continue
            union = a_files | b_files
            jaccard = len(intersection) / len(union)
            if jaccard >= 0.3:
                edges.append({
                    "from": formal_nodes[i]["id"],
                    "to": formal_nodes[j]["id"],
                    "type": "file_overlap",
                    "overlap_files": sorted(intersection),
                    "overlap_ratio": round(jaccard, 3),
                })

    # 2. same_push 边：批量 push 的 commits 列表中互相关联
    push_entries = [e for e in entries if e.operation == "push"]
    for pe in push_entries:
        commits = (pe.detail or {}).get("commits", [])
        if len(commits) < 2:
            continue
        for i in range(len(commits)):
            for j in range(i + 1, len(commits)):
                edges.append({
                    "from": commits[i],
                    "to": commits[j],
                    "type": "same_push",
                    "pushed_at": pe.timestamp,
                })

    # 3. trial_source 边：triage_accept → formal（同 correlation_id）
    accept_cids = {n["correlation_id"] for n in accept_nodes if n["correlation_id"]}
    for an in accept_nodes:
        cid = an.get("correlation_id", "")
        if not cid:
            continue
        for fn in formal_nodes:
            if fn.get("correlation_id") == cid:
                edges.append({
                    "from": an["id"],
                    "to": fn["id"],
                    "type": "trial_source",
                })

    return {
        "project": project_name,
        "nodes": nodes,
        "edges": edges,
    }
