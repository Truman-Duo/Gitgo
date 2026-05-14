"""测试 patterns.py — 变更模式检测"""
from __future__ import annotations

from unittest.mock import patch

from backend.core.governance.patterns import (
    build_patterns_report,
    detect_co_changing,
    detect_trial_impact,
    detect_type_clusters,
)
from backend.core.history import HistoryEntry, HistoryManager


def _make_entry(project: str, operation: str, detail: dict | None = None,
                correlation_id: str = "", timestamp: str = "2026-05-13T10:00:00") -> HistoryEntry:
    return HistoryEntry(
        timestamp=timestamp,
        project_name=project,
        operation=operation,
        detail=detail or {},
        correlation_id=correlation_id,
    )


# ── detect_co_changing ───────────────────────────────────────


def test_co_changing_empty():
    """无 formalize 记录时返回空列表。"""
    with patch.object(HistoryManager, "load", return_value=[]):
        assert detect_co_changing("Test") == []


def test_co_changing_single_dir():
    """formal commit 只含一个目录时不计入共变。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat: x",
            "files_changed": [
                {"path": "adapters/a.py", "status": "new"},
                {"path": "adapters/b.py", "status": "modified"},
            ],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        result = detect_co_changing("Test")
    assert result == []  # 所有文件在同一目录，无跨目录配对


def test_co_changing_cross_dir():
    """跨目录变更：adapters/ 和 tests/ 同时变更。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat: ssh adapter",
            "files_changed": [
                {"path": "adapters/ssh.py", "status": "new"},
                {"path": "tests/test_ssh.py", "status": "new"},
            ],
        }),
        _make_entry("Test", "formalize", {
            "commit": "[T-2] feat: more adapters",
            "files_changed": [
                {"path": "adapters/factory.py", "status": "modified"},
                {"path": "tests/test_factory.py", "status": "new"},
            ],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        result = detect_co_changing("Test")
    assert len(result) == 1
    assert set(result[0]["modules"]) == {"adapters", "tests"}
    assert result[0]["co_occurrence"] == 2
    assert result[0]["total_formal"] == 2


def test_co_changing_root_files():
    """根目录文件归入 (root)。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat",
            "files_changed": [
                {"path": "README.md", "status": "modified"},
                {"path": "adapters/ssh.py", "status": "new"},
            ],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        result = detect_co_changing("Test")
    assert len(result) == 1
    assert "(root)" in result[0]["modules"]


def test_co_changing_filters_by_project():
    """仅检测指定项目的共变。"""
    entries = [
        _make_entry("A", "formalize", {
            "commit": "[A-1] feat",
            "files_changed": [
                {"path": "src/a.py", "status": "new"},
                {"path": "tests/a_test.py", "status": "new"},
            ],
        }),
        _make_entry("B", "formalize", {
            "commit": "[B-1] feat",
            "files_changed": [
                {"path": "lib/b.py", "status": "new"},
                {"path": "lib/c.py", "status": "new"},
            ],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        result = detect_co_changing("A")
    assert len(result) == 1
    assert set(result[0]["modules"]) == {"src", "tests"}


# ── detect_type_clusters ─────────────────────────────────────


def test_type_clusters_empty():
    with patch.object(HistoryManager, "load", return_value=[]):
        assert detect_type_clusters("Test") == []


def test_type_clusters_basic():
    """基本类型分布：feat/fix/docs + 单源/多源。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat: add X",
            "source_indices": [0],
        }),
        _make_entry("Test", "formalize", {
            "commit": "[T-2] feat: add Y and Z",
            "source_indices": [1, 2],
        }),
        _make_entry("Test", "formalize", {
            "commit": "[T-3] fix: patch",
            "source_indices": [3],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        result = detect_type_clusters("Test")
    assert len(result) >= 2
    feat = [c for c in result if c["type"] == "feat"][0]
    assert feat["count"] == 2
    assert feat["avg_sources"] == 1.5  # (1 + 2) / 2
    assert feat["multi_source_ratio"] == 0.5

    fix = [c for c in result if c["type"] == "fix"][0]
    assert fix["count"] == 1
    assert fix["avg_sources"] == 1.0
    assert fix["multi_source_ratio"] == 0.0


def test_type_clusters_no_colon():
    """commit tag 不含冒号时 type 为 unknown。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "just a message",
            "source_indices": [0],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        result = detect_type_clusters("Test")
    assert result[0]["type"] == "unknown"


def test_type_clusters_no_tag():
    """commit tag 不含 ] 前缀时正确提取。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "feat: simple message",
            "source_indices": [0, 1],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        result = detect_type_clusters("Test")
    assert result[0]["type"] == "feat"
    assert result[0]["multi_source_ratio"] == 1.0


# ── detect_trial_impact ──────────────────────────────────────


def test_trial_impact_empty():
    with patch.object(HistoryManager, "load", return_value=[]):
        result = detect_trial_impact("Test")
    assert result["total_accepted"] == 0
    assert result["avg_trigger_rate"] == 0.0


def test_trial_impact_no_accepts():
    """仅有 scan 和 formalize，无 triage_accept。"""
    entries = [
        _make_entry("Test", "scan", {"entries_changed": 5},
                     correlation_id="uuid-1"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        result = detect_trial_impact("Test")
    assert result["total_accepted"] == 0


def test_trial_impact_triggered():
    """triage_accept 后 scan 检测到变更 → 计入触发。"""
    entries = [
        _make_entry("Test", "triage_accept", {"trial_hash": "abc"},
                     correlation_id="uuid-1", timestamp="2026-05-13T10:00:00"),
        _make_entry("Test", "scan", {"entries_changed": 3},
                     correlation_id="uuid-1", timestamp="2026-05-13T10:01:00"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        result = detect_trial_impact("Test")
    assert result["total_accepted"] == 1
    assert result["triggered_workspace_change"] == 1
    assert result["avg_trigger_rate"] == 1.0


def test_trial_impact_not_triggered():
    """triage_accept 后 scan 无变更 → 不计入触发。"""
    entries = [
        _make_entry("Test", "triage_accept", {"trial_hash": "abc"},
                     correlation_id="uuid-1", timestamp="2026-05-13T10:00:00"),
        _make_entry("Test", "scan", {"entries_changed": 0},
                     correlation_id="uuid-1", timestamp="2026-05-13T10:01:00"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        result = detect_trial_impact("Test")
    assert result["triggered_workspace_change"] == 0


def test_trial_impact_only_after_accept():
    """scan 在 accept 之前的（不同 session）不计入。"""
    entries = [
        _make_entry("Test", "scan", {"entries_changed": 5},
                     correlation_id="uuid-1", timestamp="2026-05-13T09:59:00"),
        _make_entry("Test", "triage_accept", {"trial_hash": "abc"},
                     correlation_id="uuid-1", timestamp="2026-05-13T10:00:00"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        result = detect_trial_impact("Test")
    # scan 在 accept 之前 — 不计入
    assert result["triggered_workspace_change"] == 0


def test_trial_impact_multiple_accepts():
    """多个 accept，部分触发。"""
    entries = [
        # Session 1: accept → scan with changes (triggered)
        _make_entry("Test", "triage_accept", {"trial_hash": "a1"},
                     correlation_id="uuid-1", timestamp="2026-05-13T10:00:00"),
        _make_entry("Test", "scan", {"entries_changed": 2},
                     correlation_id="uuid-1", timestamp="2026-05-13T10:01:00"),
        # Session 2: accept → scan without changes (not triggered)
        _make_entry("Test", "triage_accept", {"trial_hash": "a2"},
                     correlation_id="uuid-2", timestamp="2026-05-13T11:00:00"),
        _make_entry("Test", "scan", {"entries_changed": 0},
                     correlation_id="uuid-2", timestamp="2026-05-13T11:01:00"),
        # Session 3: accept → no scan (not triggered)
        _make_entry("Test", "triage_accept", {"trial_hash": "a3"},
                     correlation_id="uuid-3", timestamp="2026-05-13T12:00:00"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        result = detect_trial_impact("Test")
    assert result["total_accepted"] == 3
    assert result["triggered_workspace_change"] == 1
    assert result["avg_trigger_rate"] == 0.33


# ── build_patterns_report ────────────────────────────────────


def test_build_report_aggregation():
    """完整报告聚合三个检测器。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat: add X",
            "source_indices": [0],
            "files_changed": [
                {"path": "src/main.py", "status": "new"},
                {"path": "tests/test_main.py", "status": "new"},
            ],
        }),
        _make_entry("Test", "triage_accept", {"trial_hash": "abc"},
                     correlation_id="uuid-1", timestamp="2026-05-13T10:00:00"),
        _make_entry("Test", "scan", {"entries_changed": 3},
                     correlation_id="uuid-1", timestamp="2026-05-13T10:01:00"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        report = build_patterns_report("Test")
    assert report["project"] == "Test"
    assert len(report["co_changing_modules"]) == 1
    assert len(report["commit_type_clusters"]) == 1
    assert report["trial_impact"]["total_accepted"] == 1
