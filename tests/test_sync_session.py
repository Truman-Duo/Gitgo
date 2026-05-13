"""测试 SyncSession — 尤其是 Trial 三叉工作流方法"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.config import Config, ProjectConfig
from backend.core.sync_session import SessionStage, SyncSession
from backend.models import FileAccess, FileAccessKind, IncomingChange, RepoNode, TrialAction


def _make_project(git_repo: Path, trial_path: str | None = None) -> ProjectConfig:
    """构造含 trial 或非 trial 的 ProjectConfig。"""
    p = ProjectConfig(
        name="Test",
        workspace=RepoNode(file_access=FileAccess(
            kind=FileAccessKind.LOCAL, path=str(git_repo)
        )),
        release=RepoNode(file_access=FileAccess(
            kind=FileAccessKind.LOCAL, path=str(git_repo)
        )),
    )
    if trial_path:
        p.trial = RepoNode(file_access=FileAccess(
            kind=FileAccessKind.LOCAL, path=trial_path,
        ))
    return p


def _make_session(project: ProjectConfig) -> SyncSession:
    return SyncSession(project=project, config=Config())


class TestSessionStage:
    def test_trial_stages_exist(self):
        assert SessionStage.TRIAL_CHECKING is not None
        assert SessionStage.TRIAL_REVIEWING is not None


class TestSyncSessionInit:
    def test_default_trial_fields(self, git_repo: Path):
        """没有 trial 配置时 trial 字段为空"""
        session = _make_session(_make_project(git_repo))
        assert session.incoming_changes == []
        assert session.trial_adapter is None
        assert session.trial_git_runner is None

    def test_trial_node_passed(self, git_repo: Path):
        """有 trial 节点时 trial 适配器初始为 None（懒加载），step_check_trial 后创建"""
        session = _make_session(_make_project(git_repo, trial_path=str(git_repo)))
        # __init__ 时 trial 适配器为 None（懒加载）
        assert session.trial_adapter is None
        assert session.trial_git_runner is None
        # step_check_trial 后创建
        session.step_check_trial()
        assert session.trial_adapter is not None
        assert session.trial_git_runner is not None


class TestSyncSessionTrialMethods:
    def test_step_check_trial_no_trial(self, git_repo: Path):
        """没有 trial 节点时返回空列表"""
        session = _make_session(_make_project(git_repo))
        changes = session.step_check_trial()
        assert changes == []

    def test_step_check_trial_first_time(self, git_repo: Path):
        """首次检查只记录 HEAD，返回空列表"""
        session = _make_session(_make_project(git_repo, trial_path=str(git_repo)))
        changes = session.step_check_trial()
        assert changes == []
        # last_known_head 已被记录在 project.trial 上
        assert len(session.project.trial.last_known_head) == 40

    def test_step_check_trial_second_time_no_new(self, git_repo: Path):
        """第二次检查，没有新 commit"""
        session = _make_session(_make_project(git_repo, trial_path=str(git_repo)))
        session.step_check_trial()  # 第一次 — 记录 HEAD
        changes = session.step_check_trial()  # 第二次 — 无新 commit
        assert changes == []

    def test_step_triage_invalid_index(self, git_repo: Path):
        """无效索引返回 False"""
        session = _make_session(_make_project(git_repo, trial_path=str(git_repo)))
        session.incoming_changes = [IncomingChange(hash="abc", message="test")]
        result = session.step_triage_incoming(5, "discard")
        assert result is False

    def test_step_triage_discard(self, git_repo: Path):
        """discard 只标记不执行 git 操作"""
        session = _make_session(_make_project(git_repo, trial_path=str(git_repo)))
        session.step_check_trial()
        change = IncomingChange(hash="abc123", message="test msg")
        session.incoming_changes = [change]

        ok = session.step_triage_incoming(0, "discard")
        assert ok
        assert change.triage == TrialAction.DISCARDED


class TestSyncSessionHooks:
    def test_triage_decision_hook(self, git_repo: Path):
        """on_triage_decision 钩子在 discard 时不被调用（默认实现返回 None）。
        但 step_triage_incoming 仍可独立运行。"""
        session = _make_session(_make_project(git_repo, trial_path=str(git_repo)))
        change = IncomingChange(hash="abc", message="m")
        session.incoming_changes = [change]

        callback = MagicMock()
        session.on_triage_decision = callback
        session.step_triage_incoming(0, "discard")
        assert change.triage == TrialAction.DISCARDED


class TestSyncSessionReset:
    def test_reset_clears_trial(self, git_repo: Path):
        session = _make_session(_make_project(git_repo, trial_path=str(git_repo)))
        session.incoming_changes = [IncomingChange(hash="abc")]
        session.reset()
        assert session.incoming_changes == []
