"""发布推理 — 从 push 记录构建发布历史，支持 release note 注释。"""
from __future__ import annotations

from backend.core.history import HistoryManager


def list_releases(project_name: str) -> dict:
    """从 HistoryManager 提取所有 push 记录，按 pushed_at 分组为发布列表。"""
    entries = HistoryManager.load()
    entries = [e for e in entries if e.project_name == project_name and e.operation == "push"]

    releases = []
    for e in entries:
        detail = e.detail or {}
        releases.append({
            "pushed_at": e.timestamp,
            "commits": detail.get("commits", []),
            "reason": detail.get("release_note"),
        })

    # 按时间倒序：最新发布在前
    releases.sort(key=lambda r: r["pushed_at"], reverse=True)
    return {"project": project_name, "releases": releases}


def add_release_note(project_name: str, message: str) -> bool:
    """找到最近一次 push 记录，写入其 detail.release_note。"""
    entries = HistoryManager.load()
    push_pairs = [
        (i, e) for i, e in enumerate(entries)
        if e.project_name == project_name and e.operation == "push"
    ]
    if not push_pairs:
        return False

    # 按 timestamp 排序，找到最新的 push 记录
    push_pairs.sort(key=lambda x: x[1].timestamp, reverse=True)
    idx, entry = push_pairs[0]

    # 更新 detail（add_release_note 的 "note" 操作名将内容写入 release_note 字段）
    detail = dict(entry.detail or {})
    detail["release_note"] = message
    entries[idx] = entry
    entries[idx].detail = detail

    HistoryManager.save(entries)
    return True
