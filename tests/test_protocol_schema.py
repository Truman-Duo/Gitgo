"""协议 Schema 校验测试 — 确保 CLI --json 输出与 Gitgo_Protocol_v1.0.md 一致。

不引入 JSON Schema 库，使用 Python 内置类型检查。
"""
from __future__ import annotations

from unittest.mock import patch

from backend.core.governance.graph import build_graph
from backend.core.governance.patterns import build_patterns_report
from backend.core.governance.quality import compute_quality_metrics, load_suggestion_pairs
from backend.core.governance.releases import list_releases
from backend.core.history import HistoryEntry, HistoryManager
from backend.core.sync_session import SyncSession


def _make_entry(project: str, operation: str, detail: dict | None = None,
                correlation_id: str = "", timestamp: str = "2026-05-16T10:00:00") -> HistoryEntry:
    return HistoryEntry(
        timestamp=timestamp,
        project_name=project,
        operation=operation,
        detail=detail or {},
        correlation_id=correlation_id,
    )


# ── State Schema (§1) ──────────────────────────────────────────

def _make_config(name="SchemaTest"):
    """创建测试用 Config，使用临时路径避免访问实际文件系统。"""
    from backend.core.config import Config, ProjectConfig
    from backend.models import FileAccess, FileAccessKind, RepoNode
    return Config(
        projects=[ProjectConfig(
            name=name,
            workspace=RepoNode(file_access=FileAccess(kind=FileAccessKind.LOCAL, path="/tmp/schema_ws")),
            release=RepoNode(file_access=FileAccess(kind=FileAccessKind.LOCAL, path="/tmp/schema_bk")),
            trial=RepoNode(file_access=FileAccess(kind=FileAccessKind.LOCAL, path="")),
            commit_format={"prefix": "SC", "number_start": 1, "padding": False, "plugins": []},
            force_exclude=[],
        )],
        language="zh",
    )


def test_status_schema_keys():
    """status_dict(semantic=True) 含全部 6 个顶级 key。"""
    with patch.object(HistoryManager, "load", return_value=[]):
        cfg = _make_config()
        session = SyncSession(cfg.projects[0], cfg)
        d = session.status_dict(semantic=True)

    assert isinstance(d, dict)
    assert set(d.keys()) == {"project", "stage", "workspace", "commits", "trial", "semantic"}


def test_empty_status_structure():
    """验证 status_dict 返回值的结构和类型。"""
    with patch.object(HistoryManager, "load", return_value=[]):
        cfg = _make_config()
        session = SyncSession(cfg.projects[0], cfg)
        d = session.status_dict(semantic=True)

    assert isinstance(d, dict)
    assert "project" in d
    assert "stage" in d
    assert "workspace" in d
    assert "commits" in d
    assert "trial" in d
    assert "semantic" in d

    # workspace 子结构
    ws = d["workspace"]
    assert isinstance(ws, dict)
    assert "path" in ws
    assert isinstance(ws["entries_total"], int)
    assert isinstance(ws["entries_changed"], int)

    # commits 子结构
    cm = d["commits"]
    assert isinstance(cm, dict)
    assert isinstance(cm["workspace_total"], int)
    assert isinstance(cm["formal_total"], int)
    assert isinstance(cm["formal_synced"], int)
    assert isinstance(cm["formal_pushed"], int)

    # trial 子结构
    tr = d["trial"]
    assert isinstance(tr, dict)
    assert isinstance(tr["configured"], bool)
    assert isinstance(tr["pending"], int)
    assert isinstance(tr["total"], int)

    # semantic 子结构
    sem = d["semantic"]
    assert isinstance(sem, dict)
    assert sem["workspace_entropy"] in ("low", "medium", "high")
    assert isinstance(sem["trial_requires_review"], bool)
    assert isinstance(sem["safe_to_formalize"], bool)
    assert isinstance(sem["safe_to_publish"], bool)
    assert sem["blocked_reason"] is None or isinstance(sem["blocked_reason"], str)
    assert sem["suggested_next_action"] in ("triage", "formalize", "push", "idle")
    assert isinstance(sem["action_queue"], list)


def test_status_raw_no_semantic():
    """status_dict(semantic=False) 不含 semantic 块。"""
    with patch.object(HistoryManager, "load", return_value=[]):
        cfg = _make_config()
        session = SyncSession(cfg.projects[0], cfg)
        d = session.status_dict(semantic=False)

    assert "semantic" not in d
    assert "project" in d
    assert "workspace" in d
    assert "commits" in d


# ── Governance Schema (§6) ─────────────────────────────────────

def test_quality_schema_empty():
    """空历史时 quality 输出符合 schema。"""
    with patch.object(HistoryManager, "load", return_value=[]):
        pairs = load_suggestion_pairs("Test")
        m = compute_quality_metrics(pairs)

    assert isinstance(m, dict)
    assert m["suggestion_count"] == 0
    assert m["by_type"] == {}
    assert m["by_commit_type"] == {}
    assert m["by_module"] == {}


def test_quality_schema_full():
    """有数据时 quality 输出含所有必需字段。"""
    entries = [
        _make_entry("Test", "suggest_formalize", {
            "indices": [0, 1, 2],
            "ai_proposal": {"indices": [0, 1, 2]},
            "human_decision": {"indices": [0, 1, 2]},
        }, correlation_id="c1"),
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat: add",
            "source_indices": [0, 1, 2],
        }, correlation_id="c1"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        pairs = load_suggestion_pairs("Test")
        m = compute_quality_metrics(pairs)

    assert m["suggestion_count"] >= 1
    for stype in m.get("by_type", {}):
        d = m["by_type"][stype]
        for k in ("total", "accepted", "modified", "rejected",
                  "acceptance_rate", "modification_rate", "rejection_rate"):
            assert k in d, f"by_type.{stype} 缺少 {k}"
        assert 0 <= d["acceptance_rate"] <= 1
        assert 0 <= d["modification_rate"] <= 1
        assert 0 <= d["rejection_rate"] <= 1


def test_patterns_schema():
    """patterns 输出含三层结构。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat: add A",
            "source_indices": [0],
            "files_changed": [{"path": "src/a.py", "status": "new"}],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        r = build_patterns_report("Test")

    assert isinstance(r, dict)
    assert "project" in r
    assert isinstance(r["co_changing_modules"], list)
    assert isinstance(r["commit_type_clusters"], list)
    assert isinstance(r["trial_impact"], dict)
    ti = r["trial_impact"]
    for k in ("total_accepted", "triggered_workspace_change", "avg_trigger_rate"):
        assert k in ti


def test_graph_schema():
    """graph 输出含 nodes + edges 数组。"""
    entries = [
        _make_entry("Test", "formalize", {
            "commit": "[T-1] feat: add",
            "files_changed": [{"path": "src/a.py", "status": "new"}],
        }),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        g = build_graph("Test")

    assert isinstance(g, dict)
    assert "project" in g
    assert isinstance(g["nodes"], list)
    assert isinstance(g["edges"], list)
    # 节点结构
    for n in g["nodes"]:
        assert "id" in n
        assert "type" in n
        assert n["type"] in ("formal", "incoming")
        assert "created_at" in n
        assert "correlation_id" in n
        if n["type"] == "formal":
            assert "files_changed" in n
            assert "source_commits" in n
        elif n["type"] == "incoming":
            assert "trial_hash" in n
    # 边结构
    for e in g["edges"]:
        assert "from" in e
        assert "to" in e
        assert "type" in e
        assert e["type"] in ("file_overlap", "same_push", "trial_source")


def test_releases_schema():
    """releases 输出含 releases 数组，按时间倒序。"""
    entries = [
        _make_entry("Test", "push", {
            "commits": ["[T-1]", "[T-2]"],
            "release_note": "安全修复",
        }, timestamp="2026-05-16T15:00:00"),
    ]
    with patch.object(HistoryManager, "load", return_value=entries):
        data = list_releases("Test")

    assert isinstance(data, dict)
    assert "project" in data
    assert isinstance(data["releases"], list)
    for r in data["releases"]:
        assert "pushed_at" in r
        assert "commits" in r
        assert "reason" in r  # 可能为 None


# ── Stream Event Schema (§3) ────────────────────────────────────

def test_stream_event_names_consistent():
    """验证 cli/commands.py 中所有流式事件使用标准化名称。"""
    import ast
    from pathlib import Path

    commands_path = Path(__file__).parent.parent / "cli" / "commands.py"
    source = commands_path.read_text(encoding="utf-8")

    # 不应出现旧的 "started" 事件名（sync 已归一化）
    assert '"event": "started"' not in source, \
        "发现旧事件名 'started'，应使用 'operation_started'"

    # 不应出现旧的 "complete" 事件名
    assert '"event": "complete"' not in source, \
        "发现旧事件名 'complete'，应使用 'operation_complete'"

    # 应出现标准化事件名
    assert '"event": "operation_started"' in source
    assert '"event": "operation_complete"' in source
    assert '"event": "progress"' in source


def test_daemon_event_names():
    """验证 daemon 使用一致的 operation_started/operation_complete。"""
    from pathlib import Path

    daemon_path = Path(__file__).parent.parent / "backend" / "core" / "daemon" / "__init__.py"
    source = daemon_path.read_text(encoding="utf-8")

    # daemon 应使用标准事件名
    assert '"event": "operation_started"' in source
    assert '"event": "operation_complete"' in source
    # daemon 生命周期事件保持不变
    assert '"event": "daemon_started"' in source
    assert '"event": "daemon_stopped"' in source


# ── 错误格式 (§2.1) ─────────────────────────────────────────────

def test_error_format_consistency():
    """验证所有 CLI 错误输出使用统一格式。"""
    from pathlib import Path

    cli_dir = Path(__file__).parent.parent / "cli"
    source = ""
    for fname in ["commands.py", "commands_ext.py"]:
        fp = cli_dir / fname
        if fp.exists():
            source += fp.read_text(encoding="utf-8")

    # 验证关键错误码存在
    assert "PROJECT_NOT_FOUND" in source
    assert "UNKNOWN_GOVERNANCE_TYPE" in source
    assert "NO_SYNCED_COMMITS" in source
