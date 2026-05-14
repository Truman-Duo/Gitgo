"""测试 state_bundle.py — 治理状态快照导出"""
from __future__ import annotations

from unittest.mock import patch

from backend.core.config import Config, ProjectConfig
from backend.core.governance.state_bundle import collect_state_bundle
from backend.core.history import HistoryEntry, HistoryManager
from backend.core.sync_session import SyncSession
from backend.models import FileAccess, FileAccessKind, RepoNode


def _make_config(name="BundleTest"):
    return Config(
        projects=[ProjectConfig(
            name=name,
            workspace=RepoNode(file_access=FileAccess(kind=FileAccessKind.LOCAL, path="/tmp/bundle_ws")),
            release=RepoNode(file_access=FileAccess(kind=FileAccessKind.LOCAL, path="/tmp/bundle_bk")),
            trial=RepoNode(file_access=FileAccess(kind=FileAccessKind.LOCAL, path="")),
            commit_format={"prefix": "BT", "number_start": 1, "padding": False, "plugins": []},
            force_exclude=[],
        )],
        language="zh",
    )


def _make_entry(project: str, operation: str, detail: dict | None = None,
                correlation_id: str = "", timestamp: str = "2026-05-16T10:00:00") -> HistoryEntry:
    return HistoryEntry(
        timestamp=timestamp,
        project_name=project,
        operation=operation,
        detail=detail or {},
        correlation_id=correlation_id,
    )


# ── Full bundle ──────────────────────────────────────────────────


def test_bundle_structure():
    """完整 bundle 含所有顶级 key。"""
    with patch.object(HistoryManager, "load", return_value=[]):
        cfg = _make_config()
        session = SyncSession(cfg.projects[0], cfg)
        bundle = collect_state_bundle(session, minimal=False)

    assert "gitgo_protocol_version" in bundle
    assert bundle["gitgo_protocol_version"] == "1.0"
    assert "exported_at" in bundle
    assert "project" in bundle
    assert "current_state" in bundle
    assert "governance_summary" in bundle
    assert "recent_history" in bundle
    assert "recent_suggestions" in bundle

    # project 子结构
    p = bundle["project"]
    assert p["name"] == "BundleTest"
    assert p["workspace_path"] is not None
    assert "commit_prefix" in p

    # governance_summary 子结构
    gs = bundle["governance_summary"]
    assert "quality" in gs
    assert "patterns" in gs


def test_bundle_minimal():
    """minimal 模式不含 history/suggestions。"""
    with patch.object(HistoryManager, "load", return_value=[]):
        cfg = _make_config()
        session = SyncSession(cfg.projects[0], cfg)
        bundle = collect_state_bundle(session, minimal=True)

    assert "gitgo_protocol_version" in bundle
    assert "current_state" in bundle
    assert "governance_summary" in bundle
    assert "recent_history" not in bundle
    assert "recent_suggestions" not in bundle


def test_bundle_with_history():
    """有历史记录时 history 和 suggestions 被正确过滤。"""
    entries = [
        _make_entry("BundleTest", "scan", {"entries_changed": 3}),
        _make_entry("BundleTest", "formalize", {
            "commit": "[BT-1] feat: add",
            "source_indices": [0, 1],
        }, correlation_id="c1"),
        _make_entry("BundleTest", "suggest_formalize", {
            "indices": [0, 1],
        }, correlation_id="c1"),
        _make_entry("OtherProject", "scan", {"entries_changed": 1}),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        cfg = _make_config()
        session = SyncSession(cfg.projects[0], cfg)
        bundle = collect_state_bundle(session, minimal=False)

    # 仅 BundleTest 的条目
    hist = bundle["recent_history"]
    assert len(hist) == 3  # 3 BundleTest entries, 1 OtherProject filtered out
    for h in hist:
        assert h["project_name"] == "BundleTest"

    # suggestions 仅 suggest_* 条目
    sugg = bundle["recent_suggestions"]
    assert len(sugg) == 1
    assert sugg[0]["operation"] == "suggest_formalize"


def test_bundle_governance_summary_empty():
    """空历史时 governance summary 为空报告。"""
    with patch.object(HistoryManager, "load", return_value=[]):
        cfg = _make_config()
        session = SyncSession(cfg.projects[0], cfg)
        bundle = collect_state_bundle(session, minimal=False)

    q = bundle["governance_summary"]["quality"]
    assert q["suggestion_count"] == 0


def test_bundle_current_state_matches_status():
    """current_state 应与 status_dict(semantic=True) 一致。"""
    with patch.object(HistoryManager, "load", return_value=[]):
        cfg = _make_config()
        session = SyncSession(cfg.projects[0], cfg)
        bundle = collect_state_bundle(session, minimal=True)

    expected = session.status_dict(semantic=True)
    assert bundle["current_state"] == expected


def test_bundle_json_serializable():
    """验证 bundle 可被 json.dumps 序列化。"""
    import json

    with patch.object(HistoryManager, "load", return_value=[]):
        cfg = _make_config()
        session = SyncSession(cfg.projects[0], cfg)
        bundle = collect_state_bundle(session, minimal=False)

    # 不应抛出异常
    json_str = json.dumps(bundle, indent=2, ensure_ascii=False)
    assert len(json_str) > 0

    # 验证可被 json.tool 解析
    parsed = json.loads(json_str)
    assert parsed["gitgo_protocol_version"] == "1.0"
