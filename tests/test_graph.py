"""测试 graph.py — 语义变更图"""
from __future__ import annotations

from unittest.mock import patch

from backend.core.governance.graph import build_graph
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


# ── nodes ─────────────────────────────────────────────────────


def test_graph_empty():
    """无 formalize/push 记录时返回空图和空边。"""
    with patch.object(HistoryManager, "load", return_value=[]):
        g = build_graph("Test")
    assert g["project"] == "Test"
    assert g["nodes"] == []
    assert g["edges"] == []


def test_graph_formal_nodes():
    """formalize 条目转为 formal 节点。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat: add X",
            "source_indices": [0, 1],
            "files_changed": [
                {"path": "src/a.py", "status": "new"},
                {"path": "src/b.py", "status": "new"},
            ],
        }),
        _make_entry("Test", "formalize", {
            "commit": "[T-2] fix: patch",
            "source_indices": [2],
            "files_changed": [
                {"path": "src/a.py", "status": "modified"},
            ],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        g = build_graph("Test")
    assert len(g["nodes"]) == 2
    ids = {n["id"] for n in g["nodes"]}
    assert "[T-1]" in ids
    assert "[T-2]" in ids
    n1 = [n for n in g["nodes"] if n["id"] == "[T-1]"][0]
    assert n1["type"] == "formal"
    assert n1["source_commits"] == 2


def test_graph_incoming_nodes():
    """triage_accept 条目转为 incoming 节点。"""
    entries = [
        _make_entry("Test", "triage_accept", {
            "trial_hash": "abc123def456",
            "trial_message": "fix: security patch",
        }, correlation_id="uuid-1"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        g = build_graph("Test")
    assert len(g["nodes"]) == 1
    assert g["nodes"][0]["type"] == "incoming"
    assert "abc123def456" in g["nodes"][0]["trial_hash"]


def test_graph_dedup_nodes():
    """相同 commit id 的节点去重。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat: add X",
            "files_changed": [{"path": "a.py", "status": "new"}],
        }),
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat: add X",  # 重复
            "files_changed": [{"path": "a.py", "status": "new"}],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        g = build_graph("Test")
    assert len(g["nodes"]) == 1


# ── file_overlap edges ────────────────────────────────────────


def test_graph_file_overlap():
    """两个 formal commit 有文件重叠 → file_overlap 边。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat: add A",
            "files_changed": [
                {"path": "src/a.py", "status": "new"},
                {"path": "src/shared.py", "status": "modified"},
            ],
        }),
        _make_entry("Test", "formalize", {
            "commit": "[T-2] feat: add B",
            "files_changed": [
                {"path": "src/b.py", "status": "new"},
                {"path": "src/shared.py", "status": "modified"},
            ],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        g = build_graph("Test")
    edges = [e for e in g["edges"] if e["type"] == "file_overlap"]
    assert len(edges) == 1
    assert "src/shared.py" in edges[0]["overlap_files"]
    assert edges[0]["overlap_ratio"] > 0


def test_graph_file_overlap_below_threshold():
    """Jaccard < 0.3 时不产生边。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat",
            "files_changed": [
                {"path": "a.py", "status": "new"},
                {"path": "b.py", "status": "new"},
                {"path": "c.py", "status": "new"},
                {"path": "d.py", "status": "new"},
            ],
        }),
        _make_entry("Test", "formalize", {
            "commit": "[T-2] fix",
            "files_changed": [
                {"path": "a.py", "status": "modified"},
            ],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        g = build_graph("Test")
    # Jaccard = 1/4 = 0.25 < 0.3 → no edge
    edges = [e for e in g["edges"] if e["type"] == "file_overlap"]
    assert len(edges) == 0


def test_graph_file_overlap_no_intersection():
    """无共同文件时不产生边。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat",
            "files_changed": [{"path": "src/a.py", "status": "new"}],
        }),
        _make_entry("Test", "formalize", {
            "commit": "[T-2] fix",
            "files_changed": [{"path": "tests/b.py", "status": "new"}],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        g = build_graph("Test")
    edges = [e for e in g["edges"] if e["type"] == "file_overlap"]
    assert len(edges) == 0


# ── same_push edges ───────────────────────────────────────────


def test_graph_same_push():
    """批量 push 的 commits 互相关联。"""
    entries = [
        _make_entry("Test", "push", {
            "commits": ["[T-1]", "[T-2]", "[T-3]"],
        }, timestamp="2026-05-13T15:00:00"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        g = build_graph("Test")
    push_edges = [e for e in g["edges"] if e["type"] == "same_push"]
    # 3 commits → 3 choose 2 = 3 pairs
    assert len(push_edges) == 3
    for e in push_edges:
        assert e["pushed_at"] == "2026-05-13T15:00:00"


def test_graph_same_push_single_commit():
    """单个 commit push 不产生 same_push 边。"""
    entries = [
        _make_entry("Test", "push", {"commits": ["[T-1]"]}),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        g = build_graph("Test")
    push_edges = [e for e in g["edges"] if e["type"] == "same_push"]
    assert len(push_edges) == 0


# ── trial_source edges ────────────────────────────────────────


def test_graph_trial_source():
    """triage_accept 与 formalize 同 correlation_id → trial_source 边。"""
    entries = [
        _make_entry("Test", "triage_accept", {
            "trial_hash": "abc123",
            "trial_message": "fix: CVE",
        }, correlation_id="uuid-shared"),
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat: add X",
            "files_changed": [{"path": "src/a.py", "status": "new"}],
        }, correlation_id="uuid-shared"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        g = build_graph("Test")
    ts_edges = [e for e in g["edges"] if e["type"] == "trial_source"]
    assert len(ts_edges) == 1
    assert "incoming:abc123" in ts_edges[0]["from"]
    assert ts_edges[0]["to"] == "[T-1]"


def test_graph_trial_source_no_match():
    """不同 correlation_id 不产生 trial_source 边。"""
    entries = [
        _make_entry("Test", "triage_accept", {
            "trial_hash": "abc",
        }, correlation_id="uuid-1"),
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat",
            "files_changed": [{"path": "a.py", "status": "new"}],
        }, correlation_id="uuid-2"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        g = build_graph("Test")
    ts_edges = [e for e in g["edges"] if e["type"] == "trial_source"]
    assert len(ts_edges) == 0


# ── full graph ────────────────────────────────────────────────


def test_graph_full():
    """完整图：nodes + 三种边类型。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat: add A",
            "source_indices": [0],
            "files_changed": [
                {"path": "src/a.py", "status": "new"},
                {"path": "src/shared.py", "status": "modified"},
            ],
        }, correlation_id="uuid-1"),
        _make_entry("Test", "formalize", {
            "commit": "[T-2] feat: add B",
            "source_indices": [1],
            "files_changed": [
                {"path": "src/b.py", "status": "new"},
                {"path": "src/shared.py", "status": "modified"},
            ],
        }, correlation_id="uuid-2"),
        _make_entry("Test", "push", {
            "commits": ["[T-1]", "[T-2]"],
        }, timestamp="2026-05-13T15:00:00"),
        _make_entry("Test", "triage_accept", {
            "trial_hash": "def456",
            "trial_message": "fix: CVE",
        }, correlation_id="uuid-1"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        g = build_graph("Test")

    assert len(g["nodes"]) == 3  # 2 formal + 1 incoming
    assert len(g["edges"]) >= 3  # file_overlap + same_push + trial_source

    edge_types = {e["type"] for e in g["edges"]}
    assert "file_overlap" in edge_types
    assert "same_push" in edge_types
    assert "trial_source" in edge_types


def test_graph_filters_by_project():
    """仅返回指定项目的图。"""
    entries = [
        _make_entry("A", "formalize", {
            "commit": "[A-1] feat",
            "files_changed": [{"path": "a.py", "status": "new"}],
        }),
        _make_entry("B", "formalize", {
            "commit": "[B-1] feat",
            "files_changed": [{"path": "b.py", "status": "new"}],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        g = build_graph("A")
    assert len(g["nodes"]) == 1
    assert g["nodes"][0]["id"] == "[A-1]"
