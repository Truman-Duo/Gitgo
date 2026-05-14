"""测试 releases.py — 发布推理与 release note"""
from __future__ import annotations

from unittest.mock import patch

from backend.core.governance.releases import add_release_note, list_releases
from backend.core.history import HistoryEntry, HistoryManager


def _make_entry(project: str, operation: str, detail: dict | None = None,
                timestamp: str = "2026-05-13T10:00:00") -> HistoryEntry:
    return HistoryEntry(
        timestamp=timestamp,
        project_name=project,
        operation=operation,
        detail=detail or {},
    )


# ── list_releases ──────────────────────────────────────────────


def test_releases_empty():
    """无 push 记录时返回空列表。"""
    with patch.object(HistoryManager, "load", return_value=[]):
        data = list_releases("Test")
    assert data["project"] == "Test"
    assert data["releases"] == []


def test_releases_single_push():
    """单个 push 记录返回单条 release。"""
    entries = [
        _make_entry("Test", "push", {
            "commits": ["[T-1]", "[T-2]"],
        }, timestamp="2026-05-13T15:00:00"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        data = list_releases("Test")
    assert len(data["releases"]) == 1
    r = data["releases"][0]
    assert r["pushed_at"] == "2026-05-13T15:00:00"
    assert r["commits"] == ["[T-1]", "[T-2]"]
    assert r["reason"] is None


def test_releases_multiple_push():
    """多个 push 记录按时间倒序排列。"""
    entries = [
        _make_entry("Test", "push", {
            "commits": ["[T-1]"],
        }, timestamp="2026-05-10T10:00:00"),
        _make_entry("Test", "push", {
            "commits": ["[T-2]", "[T-3]"],
        }, timestamp="2026-05-13T15:00:00"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        data = list_releases("Test")
    assert len(data["releases"]) == 2
    # 最新在前
    assert data["releases"][0]["pushed_at"] == "2026-05-13T15:00:00"
    assert data["releases"][1]["pushed_at"] == "2026-05-10T10:00:00"


def test_releases_with_release_note():
    """release note 在 detail 中→ reason 字段返回。"""
    entries = [
        _make_entry("Test", "push", {
            "commits": ["[T-1]"],
            "release_note": "安全修复: CVE-2026-0001",
        }, timestamp="2026-05-13T15:00:00"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        data = list_releases("Test")
    assert data["releases"][0]["reason"] == "安全修复: CVE-2026-0001"


def test_releases_filter_by_project():
    """仅返回指定项目的 push 记录。"""
    entries = [
        _make_entry("A", "push", {"commits": ["[A-1]"]}),
        _make_entry("B", "push", {"commits": ["[B-1]"]}),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        data = list_releases("A")
    assert len(data["releases"]) == 1
    assert data["releases"][0]["commits"] == ["[A-1]"]


def test_releases_ignores_non_push():
    """非 push 操作不出现在 releases 中。"""
    entries = [
        _make_entry("Test", "formalize", {"commit": "[T-1] feat"}),
        _make_entry("Test", "scan", {"entries_changed": 3}),
        _make_entry("Test", "push", {"commits": ["[T-1]"]}),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        data = list_releases("Test")
    assert len(data["releases"]) == 1


# ── add_release_note ───────────────────────────────────────────


def test_add_release_note_success():
    """为最新 push 记录添加 release note。"""
    entries = [
        _make_entry("Test", "push", {
            "commits": ["[T-1]"],
        }, timestamp="2026-05-10T10:00:00"),
        _make_entry("Test", "push", {
            "commits": ["[T-2]"],
        }, timestamp="2026-05-13T15:00:00"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries) as mock_load:
        with patch.object(HistoryManager, "save") as mock_save:
            ok = add_release_note("Test", "版本发布: 新功能上线")
    assert ok is True
    # 验证 save 被调用且最新 push 的 detail 含 release_note
    mock_save.assert_called_once()
    saved_entries = mock_save.call_args[0][0]
    push_entries = [e for e in saved_entries if e.operation == "push"]
    # 最新的 push (T-2) 应有 release_note
    latest = [e for e in push_entries if e.timestamp == "2026-05-13T15:00:00"][0]
    assert latest.detail["release_note"] == "版本发布: 新功能上线"
    # 旧的 push (T-1) 不受影响
    older = [e for e in push_entries if e.timestamp == "2026-05-10T10:00:00"][0]
    assert "release_note" not in older.detail


def test_add_release_note_no_push():
    """无 push 记录时返回 False。"""
    with patch.object(HistoryManager, "load", return_value=[]):
        ok = add_release_note("Test", "无意义")
    assert ok is False


def test_add_release_note_updates_existing():
    """覆盖已有的 release note。"""
    entries = [
        _make_entry("Test", "push", {
            "commits": ["[T-1]"],
            "release_note": "旧的说明",
        }, timestamp="2026-05-13T15:00:00"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        with patch.object(HistoryManager, "save") as mock_save:
            ok = add_release_note("Test", "新的说明")
    assert ok is True
    saved_entries = mock_save.call_args[0][0]
    assert saved_entries[0].detail["release_note"] == "新的说明"


def test_add_release_note_filter_by_project():
    """仅更新指定项目的 push 记录。"""
    entries = [
        _make_entry("A", "push", {"commits": ["[A-1]"]}, timestamp="2026-05-13T15:00:00"),
        _make_entry("B", "push", {"commits": ["[B-1]"]}, timestamp="2026-05-13T16:00:00"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        with patch.object(HistoryManager, "save") as mock_save:
            ok = add_release_note("A", "A 的 release note")
    assert ok is True
    saved = mock_save.call_args[0][0]
    a_push = [e for e in saved if e.project_name == "A" and e.operation == "push"][0]
    b_push = [e for e in saved if e.project_name == "B" and e.operation == "push"][0]
    assert a_push.detail["release_note"] == "A 的 release note"
    assert "release_note" not in b_push.detail
