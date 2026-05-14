"""测试 quality.py — 建议质量度量（仅 indices Jaccard，不做 message 文本比较）"""
from __future__ import annotations

from unittest.mock import patch

from backend.core.governance.quality import (
    compute_quality_metrics,
    group_by_commit_type,
    group_by_module,
    load_suggestion_pairs,
)
from backend.core.history import HistoryEntry, HistoryManager


def _make_entry(project: str, operation: str, detail: dict | None = None,
                correlation_id: str = "", timestamp: str = "2026-05-13T10:00:00") -> HistoryEntry:
    return HistoryEntry(
        timestamp=timestamp,
        project_name=project,
        operation=operation,
        status="success" if operation != "suggest_formalize" else "recorded",
        detail=detail or {},
        correlation_id=correlation_id,
    )


# ── load_suggestion_pairs ─────────────────────────────────────


def test_load_pairs_empty():
    """无历史记录时返回空列表。"""
    with patch.object(HistoryManager, "load", return_value=[]):
        pairs = load_suggestion_pairs("Test")
    assert pairs == []


def test_load_pairs_no_suggest_entries():
    """仅有常规操作记录、无 suggest 条目时返回空。"""
    entries = [
        _make_entry("Test", "scan"),
        _make_entry("Test", "formalize", {"commit": "[T-1] feat: x"},
                     correlation_id="uuid-1"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        pairs = load_suggestion_pairs("Test")
    assert pairs == []


def test_load_pairs_direct_mode():
    """add_suggestion 直存模式：suggest 条目同时含 ai_proposal 和 human_decision。"""
    entries = [
        _make_entry("Test", "suggest_formalize", {
            "ai_proposal": {
                "groups": [{"indices": [0, 1], "message": "[T-1] feat: add X"}],
            },
            "human_decision": {
                "indices": [0, 1],
                "commit": "[T-1] feat: add X and Y",
            },
        }, correlation_id="uuid-1"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        pairs = load_suggestion_pairs("Test")
    assert len(pairs) == 1
    assert pairs[0]["suggest_type"] == "formalize"
    assert pairs[0]["ai_proposal"]["groups"][0]["indices"] == [0, 1]
    assert pairs[0]["human_decision"]["indices"] == [0, 1]


def test_load_pairs_correlation_match():
    """correlation_id 匹配模式：suggest 条目和 formalize 执行分开记录。"""
    entries = [
        _make_entry("Test", "suggest_formalize", {
            "ai_proposal": {
                "groups": [{"indices": [0, 1], "message": "[T-1] feat"}],
            },
        }, correlation_id="uuid-1"),
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat: add X",
            "source_indices": [0, 1],
        }, correlation_id="uuid-1"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        pairs = load_suggestion_pairs("Test")
    assert len(pairs) == 1
    assert pairs[0]["human_decision"]["indices"] == [0, 1]
    assert pairs[0]["human_decision"]["commit"] == "[T-1] feat: add X"


def test_load_pairs_filters_by_project():
    """仅返回指定项目的 pairs。"""
    entries = [
        _make_entry("ProjectA", "suggest_formalize", {
            "ai_proposal": {"groups": [{"indices": [0]}]},
            "human_decision": {"indices": [0]},
        }),
        _make_entry("ProjectB", "suggest_formalize", {
            "ai_proposal": {"groups": [{"indices": [1]}]},
            "human_decision": {"indices": [1]},
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        pairs = load_suggestion_pairs("ProjectA")
    assert len(pairs) == 1


# ── compute_quality_metrics ───────────────────────────────────


def test_quality_empty():
    """无 pairs 时返回空报告。"""
    result = compute_quality_metrics([])
    assert result["suggestion_count"] == 0
    assert result["by_type"] == {}
    assert result["by_commit_type"] == {}
    assert result["by_module"] == {}


def test_quality_formalize_accepted():
    """完全采纳：AI indices 与 human indices 重叠 100%。"""
    pairs = [{
        "suggest_type": "formalize",
        "ai_proposal": {
            "groups": [{"indices": [0, 1]}, {"indices": [2]}],
        },
        "human_decision": {
            "indices": [0, 1, 2],
            "commit": "[T-1] feat: add modules",
        },
    }]
    result = compute_quality_metrics(pairs)
    f = result["by_type"]["formalize"]
    assert f["accepted"] == 1
    assert f["acceptance_rate"] == 1.0
    assert f["modification_rate"] == 0.0
    assert f["rejection_rate"] == 0.0


def test_quality_formalize_rejected():
    """完全拒绝：AI indices 与 human indices 无交集。"""
    pairs = [{
        "suggest_type": "formalize",
        "ai_proposal": {
            "groups": [{"indices": [0, 1]}],
        },
        "human_decision": {
            "indices": [2, 3],
            "commit": "[T-1] feat: other stuff",
        },
    }]
    result = compute_quality_metrics(pairs)
    f = result["by_type"]["formalize"]
    assert f["rejected"] == 1
    assert f["acceptance_rate"] == 0.0


def test_quality_formalize_modified():
    """修改后采纳：AI 建议 indices [0,1,2]，人实际执行 [0,1,3] → Jaccard 0.5。"""
    pairs = [{
        "suggest_type": "formalize",
        "ai_proposal": {
            "groups": [{"indices": [0, 1, 2]}],
        },
        "human_decision": {
            "indices": [0, 1, 3],
            "commit": "[T-1] feat: partial",
        },
    }]
    result = compute_quality_metrics(pairs)
    f = result["by_type"]["formalize"]
    assert f["modified"] == 1
    assert f["avg_index_jaccard"] == 0.5


def test_quality_formalize_jaccard_boundary():
    """Jaccard 边界：>=0.8 → accepted, >=0.3 → modified, <0.3 → rejected。"""
    # Jaccard 5/6 ≈ 0.833 → accepted
    pairs_high = [{
        "suggest_type": "formalize",
        "ai_proposal": {"groups": [{"indices": [0, 1, 2, 3, 4]}]},
        "human_decision": {"indices": [0, 1, 2, 3, 4, 5], "commit": "[T-1] feat"},
    }]
    r = compute_quality_metrics(pairs_high)
    assert r["by_type"]["formalize"]["accepted"] == 1

    # Jaccard 2/4 = 0.5 → modified
    pairs_mid = [{
        "suggest_type": "formalize",
        "ai_proposal": {"groups": [{"indices": [0, 1, 2]}]},
        "human_decision": {"indices": [0, 1, 3], "commit": "[T-1] feat"},
    }]
    r = compute_quality_metrics(pairs_mid)
    assert r["by_type"]["formalize"]["modified"] == 1

    # Jaccard 0/4 = 0.0 → rejected
    pairs_low = [{
        "suggest_type": "formalize",
        "ai_proposal": {"groups": [{"indices": [0, 1]}]},
        "human_decision": {"indices": [2, 3], "commit": "[T-1] feat"},
    }]
    r = compute_quality_metrics(pairs_low)
    assert r["by_type"]["formalize"]["rejected"] == 1


def test_quality_triage_accepted():
    """Triage 建议被完全采纳。"""
    pairs = [{
        "suggest_type": "triage",
        "ai_proposal": {
            "recommendations": [{"index": 0, "action": "accept"}],
        },
        "human_decision": {"index": "abc123", "action": "accept"},
    }]
    result = compute_quality_metrics(pairs)
    t = result["by_type"]["triage"]
    assert t["accepted"] == 1
    assert t["total"] == 1


def test_quality_triage_modified():
    """Triage 建议被修改：AI 建议 accept，人选择 discard。"""
    pairs = [{
        "suggest_type": "triage",
        "ai_proposal": {
            "recommendations": [{"index": 0, "action": "accept"}],
        },
        "human_decision": {"index": "abc123", "action": "discard"},
    }]
    result = compute_quality_metrics(pairs)
    t = result["by_type"]["triage"]
    assert t["modified"] == 1


def test_quality_rates_sum_to_one():
    """采纳率 + 修改率 + 拒绝率 ≈ 1.0。"""
    pairs = [
        {"suggest_type": "formalize",
         "ai_proposal": {"groups": [{"indices": [0, 1]}]},
         "human_decision": {"indices": [0, 1], "commit": "[T-1] feat"}},
        {"suggest_type": "formalize",
         "ai_proposal": {"groups": [{"indices": [0]}]},
         "human_decision": {"indices": [1], "commit": "[T-2] fix"}},
        {"suggest_type": "formalize",
         "ai_proposal": {"groups": [{"indices": [0, 1, 2]}]},
         "human_decision": {"indices": [0, 1, 3], "commit": "[T-3] feat"}},
    ]
    result = compute_quality_metrics(pairs)
    f = result["by_type"]["formalize"]
    total_rate = f["acceptance_rate"] + f["modification_rate"] + f["rejection_rate"]
    assert abs(total_rate - 1.0) < 0.05


# ── by_commit_type slicing ───────────────────────────────────


def test_by_commit_type():
    """按 commit type 切片正确。"""
    pairs = [
        {"suggest_type": "formalize",
         "ai_proposal": {"groups": [{"indices": [0]}]},
         "human_decision": {"indices": [0], "commit": "[T-1] feat: add X"}},
        {"suggest_type": "formalize",
         "ai_proposal": {"groups": [{"indices": [1]}]},
         "human_decision": {"indices": [1], "commit": "[T-2] fix: patch Y"}},
        {"suggest_type": "formalize",
         "ai_proposal": {"groups": [{"indices": [2]}]},
         "human_decision": {"indices": [2], "commit": "[T-3] docs: update README"}},
    ]
    result = compute_quality_metrics(pairs)
    by_type = result["by_commit_type"]
    assert "feat" in by_type
    assert "fix" in by_type
    assert "docs" in by_type
    assert by_type["feat"]["total"] == 1


def test_by_commit_type_unknown():
    """无法解析 commit type 时归入 unknown。"""
    pairs = [{
        "suggest_type": "formalize",
        "ai_proposal": {"groups": [{"indices": [0]}]},
        "human_decision": {"indices": [0], "commit": "just a message without tag"},
    }]
    result = compute_quality_metrics(pairs)
    assert "unknown" in result["by_commit_type"]


# ── by_module slicing ────────────────────────────────────────


def test_by_module():
    """按变更模块切片正确。"""
    pairs = [{
        "suggest_type": "formalize",
        "ai_proposal": {"groups": [{"indices": [0]}]},
        "human_decision": {
            "indices": [0],
            "commit": "[T-1] feat: add module",
            "files_changed": [
                {"path": "adapters/ssh.py", "status": "new"},
                {"path": "backend/core/config.py", "status": "modified"},
                {"path": "adapters/factory.py", "status": "modified"},
            ],
        },
    }]
    result = compute_quality_metrics(pairs)
    by_module = result["by_module"]
    assert "adapters" in by_module
    assert "backend" in by_module
    assert by_module["adapters"]["total"] > 0


def test_by_module_root_files():
    """根目录文件归入 (root)。"""
    pairs = [{
        "suggest_type": "formalize",
        "ai_proposal": {"groups": [{"indices": [0]}]},
        "human_decision": {
            "indices": [0],
            "commit": "[T-1] feat",
            "files_changed": [
                {"path": "README.md", "status": "modified"},
            ],
        },
    }]
    result = compute_quality_metrics(pairs)
    assert "(root)" in result["by_module"]


# ── group_by helpers ──────────────────────────────────────────


def test_group_by_commit_type_empty():
    assert group_by_commit_type([]) == {}


def test_group_by_module_empty():
    assert group_by_module([]) == {}


# ── mixed suggest types ──────────────────────────────────────


def test_mixed_suggest_types():
    """同一次工作流中既有 formalize 又有 triage 建议。"""
    pairs = [
        {"suggest_type": "formalize",
         "ai_proposal": {"groups": [{"indices": [0]}]},
         "human_decision": {"indices": [0], "commit": "[T-1] feat"}},
        {"suggest_type": "triage",
         "ai_proposal": {"recommendations": [{"index": 0, "action": "accept"}]},
         "human_decision": {"index": "abc", "action": "accept"}},
    ]
    result = compute_quality_metrics(pairs)
    assert result["suggestion_count"] == 2
    assert "formalize" in result["by_type"]
    assert "triage" in result["by_type"]
    assert result["by_type"]["formalize"]["total"] == 1
    assert result["by_type"]["triage"]["total"] == 1
