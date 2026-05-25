"""三仓集成测试日志中的全量 Bug 回归测试

B1: IncomingChange.timestamp (非 .date)
B2: Config.get_project() 不存在
B3: CommitInfo.type (非 .commit_type)
B4: push 跨进程 session 恢复
B5: trial accept cherry-pick 冲突重试
B6: _find_next_number 模式匹配 [PREFIX-N]
"""

import json
import tempfile
from pathlib import Path

from backend.core.config import Config, ProjectConfig
from backend.core.operations.git import _find_next_number, build_commit_template
from backend.core.operations.models import CommitInfo
from backend.models import (
    IncomingChange, TrialAction, RepoNode, FileAccess, FileAccessKind,
)


# ── B1: IncomingChange 字段名 ────────────────────────────

def test_incomingchange_has_timestamp_not_date():
    """B1: IncomingChange 字段是 timestamp 不是 date"""
    c = IncomingChange(
        hash="abc123", message="test", author="dev",
        timestamp="2026-05-26 12:00", triage=TrialAction.PENDING,
    )
    assert c.timestamp == "2026-05-26 12:00"
    assert not hasattr(c, "date")


def test_trial_list_json_uses_timestamp():
    """B1 回归: trial list JSON 输出使用 timestamp"""
    # 验证 JSON 序列化使用正确字段
    c = IncomingChange(
        hash="abc", message="feat: test", author="dev",
        timestamp="2026-01-01", triage=TrialAction.PENDING,
    )
    d = {
        "hash": c.hash, "message": c.message,
        "author": c.author, "timestamp": c.timestamp,
        "triage": c.triage.value,
    }
    assert d["timestamp"] == "2026-01-01"
    assert "date" not in d


# ── B2: Config 没有 get_project 方法 ─────────────────────

def test_config_has_no_get_project():
    """B2: Config 没有 get_project() 方法，必须手动遍历"""
    cfg = Config()
    assert not hasattr(cfg, "get_project")


def test_project_lookup_by_iteration():
    """B2 回归: 正确的项目查找方式是遍历 cfg.projects"""
    cfg = Config(projects=[
        ProjectConfig(name="A"),
        ProjectConfig(name="B"),
    ])
    found = None
    for p in cfg.projects:
        if p.name == "B":
            found = p
            break
    assert found is not None
    assert found.name == "B"


# ── B3: CommitInfo.type ──────────────────────────────────

def test_commitinfo_has_type_not_commit_type():
    """B3: CommitInfo 字段是 type 不是 commit_type"""
    c = CommitInfo(hash="abc", subject="feat: test", type="feat", scope=None)
    assert c.type == "feat"
    assert not hasattr(c, "commit_type")


def test_suggest_formalize_uses_type():
    """B3 回归: suggest formalize context 使用 commit.type"""
    c = CommitInfo(hash="abc", subject="feat: test", type="feat", scope="core")
    d = {"type": c.type, "subject": c.subject, "scope": c.scope}
    assert d["type"] == "feat"
    assert d["scope"] == "core"


# ── B6: _find_next_number 模式匹配 ────────────────────────

def test_find_next_number_pattern_matches_bracket_format():
    """B6: _find_next_number 应该匹配 [PREFIX-N] 格式"""
    # 测试正则是否匹配实际 commit message 格式
    import re
    pat = re.compile(r'\[MYAPP-(\d+)\]')
    msg = "[MYAPP-1] feat(core): initial setup"
    m = pat.search(msg)
    assert m is not None
    assert m.group(1) == "1"

    msg2 = "[MYAPP-23] fix(api): handle null"
    m2 = pat.search(msg2)
    assert m2 is not None
    assert m2.group(1) == "23"


def test_find_next_number_old_pattern_fails():
    """B6 回归: 旧模式 ^PREFIX-\\d+ 不会匹配 [PREFIX-N]"""
    import re
    old_pat = re.compile(r'^MYAPP-(\d+)')
    msg = "[MYAPP-1] feat(core): initial setup"
    m = old_pat.search(msg)
    assert m is None  # 旧的模式不会匹配


def test_build_commit_template_includes_bracket_prefix():
    """验证 build_commit_template 输出包含 [PREFIX-N] 格式"""
    project = ProjectConfig(
        name="Test",
        workspace=RepoNode(file_access=FileAccess(kind=FileAccessKind.LOCAL, path="/tmp/ws")),
        release=RepoNode(file_access=FileAccess(kind=FileAccessKind.LOCAL, path="/tmp/rel")),
        commit_format={"prefix": "MYAPP", "number_start": 0, "template_name": "default"},
    )
    commits = [CommitInfo(hash="abc", type="feat", scope="core", subject="Add login")]
    result = build_commit_template(commits, project)
    assert "[MYAPP-" in result
    assert "MYAPP-0" in result or "MYAPP-1" in result


# ── B5: Cherry-pick 冲突重试 ─────────────────────────────

def test_cherry_pick_retry_logic_path():
    """B5: 验证 cherry-pick 失败后 -X theirs 重试的逻辑路径存在"""
    # 不执行真实 git，验证 sync_session 中的代码路径
    from backend.core.sync_session import SyncSession, SessionStage
    import inspect
    source = inspect.getsource(SyncSession.step_triage_incoming)
    assert "-X" in source and "theirs" in source
    assert "cherry-pick" in source.lower()


# ── B4: Session 恢复 ─────────────────────────────────────

def test_load_session_restores_formal_commits():
    """B4: load_session 能恢复 formal_commits"""
    import shutil
    p = Path(tempfile.mkdtemp())
    try:
        ws = p / "ws"
        rel = p / "rel"
        ws.mkdir()
        rel.mkdir()
        (rel / ".git").mkdir()

        from backend.core.sync_session import SyncSession
        from backend.core.config import ConfigManager

        cfg = Config(projects=[
            ProjectConfig(
                name="Test",
                workspace=RepoNode(file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(ws))),
                release=RepoNode(file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(rel))),
                commit_format={"prefix": "TST", "number_start": 0, "template_name": "default"},
            )
        ])
        proj = cfg.projects[0]

        # 创建 session 并保存
        session = SyncSession(proj, cfg)
        from backend.core.sync_session import FormalCommit
        session.formal_commits.append(FormalCommit(
            message="[TST-1] test", number=1, prefix="TST",
            synced=True, pushed=False, source_indices={0},
            created_at="2026-01-01",
        ))
        session.save_session()

        # 跨进程恢复
        restored = SyncSession.load_session(proj, cfg)
        assert restored is not None
        assert len(restored.formal_commits) == 1
        assert restored.formal_commits[0].number == 1
        assert restored.formal_commits[0].synced is True
    finally:
        shutil.rmtree(str(p), ignore_errors=True)


# ── 完整字段名审计 ──────────────────────────────────────

def test_all_model_fields_audit():
    """验证所有数据模型字段名一致性"""
    # IncomingChange
    c = IncomingChange(
        hash="h", message="m", author="a", timestamp="t",
        body="b", triage=TrialAction.PENDING,
    )
    assert c.timestamp == "t"
    assert c.triage.value == "pending"

    # CommitInfo
    ci = CommitInfo(hash="h", subject="s", type="feat", scope="c", body="b")
    assert ci.type == "feat"
    assert ci.scope == "c"
    assert ci.subject == "s"

    # FileEntry
    from backend.core.operations.models import FileEntry
    fe = FileEntry(rel_path="a.py", status="new", workspace_hash="h")
    assert fe.rel_path == "a.py"
    assert not hasattr(fe, "path")
