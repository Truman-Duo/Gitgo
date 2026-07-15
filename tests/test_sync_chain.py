"""SyncSession 子系统链路测试 —— scan→formalize→sync→push 管线。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests.factory import TestDataFactory


@pytest.fixture
def f():
    return TestDataFactory(seed=55)


@pytest.fixture
def tmp_git():
    """创建真实 git 仓库 + mirror backup 用于 SyncSession 测试。"""
    import subprocess, sys
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        cf = 0x08000000 if sys.platform == "win32" else 0
        def _git(cmd, cwd=ws):
            subprocess.run(["git"] + cmd, cwd=str(cwd),
                          capture_output=True, creationflags=cf)

        # Workspace repo
        _git(["init"])
        _git(["config", "user.email", "test@gitgo.dev"])
        _git(["config", "user.name", "Gitgo Test"])
        (ws / "README.md").write_text("# test")
        _git(["add", "."])
        _git(["commit", "-m", "initial"])

        # Mirror backup repo
        bk = ws / "backup_mirror"
        bk.mkdir()
        _git(["init"], cwd=bk)
        _git(["config", "user.email", "test@gitgo.dev"], cwd=bk)
        _git(["config", "user.name", "Gitgo Test"], cwd=bk)
        (bk / "README.md").write_text("# test")
        _git(["add", "."], cwd=bk)
        _git(["commit", "-m", "initial mirror"], cwd=bk)

        yield ws


# ═══════════════════════════════════════════════════════════════
# Chain S1: FileEntry → CommitInfo 数据模型
# ═══════════════════════════════════════════════════════════════


class TestChainDataModels:
    """FileEntry + CommitInfo 生成和使用。"""

    def test_file_entries_from_factory_valid(self, f):
        """工厂生成的 FileEntry 字段完整。"""
        entries = f.file_entries(20)
        statuses = {e.status for e in entries}
        assert statuses <= {"new", "modified", "same", "renamed"}
        assert all(e.rel_path for e in entries)

    def test_commit_infos_from_factory_valid(self, f):
        """工厂生成的 CommitInfo 字段完整。"""
        commits = f.commit_infos(10)
        assert all(c.hash for c in commits)
        assert all(c.type in (
            "feat", "fix", "docs", "refactor", "test", "chore", "perf", "ci",
        ) for c in commits)

    def test_sync_chain_from_factory(self, f):
        """sync_chain 生成连贯数据。"""
        chain = f.sync_chain(file_count=10)
        assert len(chain["scan"]["entries"]) == 10
        assert len(chain["scan"]["commits"]) >= 1
        assert chain["formal"]["number"] >= 1


# ═══════════════════════════════════════════════════════════════
# Chain S2: SyncSession 初始化 → 扫描
# ═══════════════════════════════════════════════════════════════


class TestChainSyncSessionInit:
    """SyncSession 初始化和基本操作。"""

    def test_sync_session_loads_config(self, tmp_git):
        """SyncSession 从 Config 正确初始化。"""
        from backend.core.config import Config, ProjectConfig
        from backend.core.sync_session import SyncSession
        from backend.models import FileAccess, FileAccessKind, RepoNode

        release = tmp_git / "release"
        release.mkdir()
        import subprocess, sys
        cf = 0x08000000 if sys.platform == "win32" else 0
        subprocess.run(["git", "init"], cwd=str(release),
                       capture_output=True, creationflags=cf)
        subprocess.run(["git", "config", "user.email", "test@gitgo.dev"],
                       cwd=str(release), capture_output=True, creationflags=cf)
        subprocess.run(["git", "config", "user.name", "Gitgo Test"],
                       cwd=str(release), capture_output=True, creationflags=cf)
        (release / "init.txt").write_text("init")
        subprocess.run(["git", "add", "."], cwd=str(release),
                       capture_output=True, creationflags=cf)
        subprocess.run(["git", "commit", "-m", "init release"],
                       cwd=str(release), capture_output=True, creationflags=cf)

        project = ProjectConfig(
            name="testproject",
            note="chain test",
            workspace=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(tmp_git)),
            ),
            release=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(release)),
            ),
            commit_format={"prefix": "TEST", "number_start": 0, "padding": False, "plugins": []},
        )
        config = Config(projects=[project])

        session = SyncSession(project, config)
        assert session.stage is not None

    def test_step_scan(self, tmp_git):
        """step_scan 后有 FileEntry 产出。"""
        from backend.core.config import Config, ProjectConfig
        from backend.core.sync_session import SyncSession
        from backend.models import FileAccess, FileAccessKind, RepoNode
        from backend.core.cache import FileHashCache

        project = ProjectConfig(
            name="testproject",
            workspace=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(tmp_git)),
            ),
            release=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL,
                                       path=str(tmp_git / "backup_mirror")),
            ),
            commit_format={"prefix": "TEST", "number_start": 0, "padding": False, "plugins": []},
        )

        (tmp_git / "backup").mkdir(exist_ok=True)
        config = Config(projects=[project])
        session = SyncSession(project, config)

        cache = FileHashCache(tmp_git / ".gitgo")
        session.step_scan(hash_cache=cache)

        assert len(session.entries) > 0
        # 应该有至少一个 "same" 或 "new" 状态的文件
        statuses = {e.status for e in session.entries}
        assert statuses & {"same", "new", "modified"}

    def test_step_load_commits(self, tmp_git):
        """step_load_commits 产生 CommitInfo 列表。"""
        from backend.core.config import Config, ProjectConfig
        from backend.core.sync_session import SyncSession
        from backend.models import FileAccess, FileAccessKind, RepoNode

        project = ProjectConfig(
            name="testproject",
            workspace=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(tmp_git)),
            ),
            release=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL,
                                       path=str(tmp_git / "backup2")),
            ),
            commit_format={"prefix": "TEST", "number_start": 0, "padding": False, "plugins": []},
        )
        config = Config(projects=[project])
        session = SyncSession(project, config)

        session.step_load_commits()
        assert len(session.commits) >= 1

    def test_step_status_dict(self, tmp_git):
        """status_dict 包含所有顶层键。"""
        from backend.core.config import Config, ProjectConfig
        from backend.core.sync_session import SyncSession
        from backend.models import FileAccess, FileAccessKind, RepoNode
        from backend.core.cache import FileHashCache

        project = ProjectConfig(
            name="testproject",
            workspace=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(tmp_git)),
            ),
            release=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL,
                                       path=str(tmp_git / "backup3")),
            ),
            commit_format={"prefix": "TEST", "number_start": 0, "padding": False, "plugins": []},
        )
        config = Config(projects=[project])
        session = SyncSession(project, config)
        cache = FileHashCache(tmp_git / ".gitgo")
        session.step_scan(hash_cache=cache)

        status = session.status_dict(semantic=True)
        assert "project" in status
        assert "stage" in status


# ═══════════════════════════════════════════════════════════════
# Chain S3: Formalize → Edit → Delete → Dissolve
# ═══════════════════════════════════════════════════════════════


class TestChainFormalManagement:
    """Formal commit 生命周期管理。"""

    def test_create_and_delete_formal(self, tmp_git):
        """step_create_formal_commit → step_delete_formal 往返。"""
        from backend.core.config import Config, ProjectConfig
        from backend.core.sync_session import SyncSession
        from backend.models import FileAccess, FileAccessKind, RepoNode
        from backend.core.cache import FileHashCache

        project = ProjectConfig(
            name="testproject",
            workspace=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(tmp_git)),
            ),
            release=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL,
                                       path=str(tmp_git / "backup_mirror")),
            ),
            commit_format={"prefix": "TEST", "number_start": 0, "padding": False, "plugins": []},
        )
        config = Config(projects=[project])
        session = SyncSession(project, config)
        cache = FileHashCache(tmp_git / ".gitgo")
        session.step_scan(hash_cache=cache)
        session.step_load_commits()

        # 选择所有 commits
        indices = list(range(len(session.commits)))
        session.selected_workspace = set(indices)

        fc = session.step_create_formal_commit(
            indices, message="[TEST-1] feat: chain test commit",
        )
        assert fc is not None
        assert fc.message.startswith("[TEST-1]")

        # 删除
        ok = session.step_delete_formal(0)
        assert ok


# ═══════════════════════════════════════════════════════════════
# Chain S4: Trial 流程
# ═══════════════════════════════════════════════════════════════


class TestChainTrial:
    """Trial 三叉决策：check → accept/promote/discard。"""

    def test_trial_check_no_trial_node(self, tmp_git):
        """无 trial 节点 → 空 incoming。"""
        from backend.core.config import Config, ProjectConfig
        from backend.core.sync_session import SyncSession
        from backend.models import FileAccess, FileAccessKind, RepoNode

        project = ProjectConfig(
            name="testproject",
            workspace=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(tmp_git)),
            ),
            release=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL,
                                       path=str(tmp_git / "backup_mirror")),
            ),
            commit_format={"prefix": "TEST", "number_start": 0, "padding": False, "plugins": []},
        )
        config = Config(projects=[project])
        session = SyncSession(project, config)

        incoming = session.step_check_trial()
        assert incoming == []


# ═══════════════════════════════════════════════════════════════
# Chain S5: Session 持久化
# ═══════════════════════════════════════════════════════════════


class TestChainSessionPersistence:
    """session 保存/恢复/重置。"""

    def test_save_and_load(self, tmp_git):
        """save_session → load_session 往返。"""
        from backend.core.config import Config, ProjectConfig
        from backend.core.sync_session import SyncSession
        from backend.models import FileAccess, FileAccessKind, RepoNode
        from backend.core.cache import FileHashCache

        project = ProjectConfig(
            name="testproject",
            workspace=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(tmp_git)),
            ),
            release=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL,
                                       path=str(tmp_git / "backup_mirror")),
            ),
            commit_format={"prefix": "TEST", "number_start": 0, "padding": False, "plugins": []},
        )
        config = Config(projects=[project])
        session = SyncSession(project, config)
        cache = FileHashCache(tmp_git / ".gitgo")
        session.step_scan(hash_cache=cache)
        session.step_load_commits()

        path = session.save_session()
        assert path.exists()

        loaded = SyncSession.load_session(project, config)
        assert loaded is not None
        assert loaded.stage is not None

    def test_reset_clears_state(self, tmp_git):
        """reset 清空 entries 和 commits。"""
        from backend.core.config import Config, ProjectConfig
        from backend.core.sync_session import SyncSession
        from backend.models import FileAccess, FileAccessKind, RepoNode
        from backend.core.cache import FileHashCache

        project = ProjectConfig(
            name="testproject",
            workspace=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(tmp_git)),
            ),
            release=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL,
                                       path=str(tmp_git / "backup_mirror")),
            ),
            commit_format={"prefix": "TEST", "number_start": 0, "padding": False, "plugins": []},
        )
        config = Config(projects=[project])
        session = SyncSession(project, config)
        cache = FileHashCache(tmp_git / ".gitgo")
        session.step_scan(hash_cache=cache)
        session.step_load_commits()

        assert len(session.entries) > 0
        session.reset()
        assert len(session.entries) == 0


# ═══════════════════════════════════════════════════════════════
# Chain S6: Diff 摘要
# ═══════════════════════════════════════════════════════════════


class TestChainDiff:
    """Diff 摘要生成。"""

    def test_diff_summary_on_real_commit(self, tmp_git):
        """对真实 git commit 生成 diff summary。"""
        import subprocess, sys
        cf = 0x08000000 if sys.platform == "win32" else 0

        # 修改文件
        (tmp_git / "README.md").write_text("# test\n\nUpdated for chain test")
        subprocess.run(["git", "add", "."], cwd=str(tmp_git),
                       capture_output=True, creationflags=cf)
        subprocess.run(["git", "commit", "-m", "feat: update readme"],
                       cwd=str(tmp_git), capture_output=True, creationflags=cf)

        from backend.core.operations.diff import get_diff_summary
        from backend.adapters.local_git_runner import LocalGitRunner

        runner = LocalGitRunner(str(tmp_git))
        head = runner.rev_parse("HEAD")
        parent = runner.rev_parse("HEAD~1") if runner.rev_parse("HEAD~1") else None

        summary = get_diff_summary(head, runner, parent_hash=parent)
        assert isinstance(summary, list)
        for item in summary:
            assert "path" in item
            assert "status" in item


# ═══════════════════════════════════════════════════════════════
# Chain S7: 完整 Sync 管线
# ═══════════════════════════════════════════════════════════════


class TestFullSyncPipeline:
    """scan → formalize → sync 完整管线。"""

    def test_scan_to_formalize_pipeline(self, tmp_git):
        """完整 scan → load commits → create formal → status。"""
        from backend.core.config import Config, ProjectConfig
        from backend.core.sync_session import SyncSession
        from backend.models import FileAccess, FileAccessKind, RepoNode
        from backend.core.cache import FileHashCache

        project = ProjectConfig(
            name="testproject",
            workspace=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(tmp_git)),
            ),
            release=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL,
                                       path=str(tmp_git / "backup_mirror")),
            ),
            commit_format={"prefix": "TEST", "number_start": 0, "padding": False, "plugins": []},
        )
        config = Config(projects=[project])
        session = SyncSession(project, config)
        cache = FileHashCache(tmp_git / ".gitgo")

        # Step 1: Scan
        session.step_scan(hash_cache=cache)
        assert len(session.entries) > 0

        # Step 2: Load commits
        session.step_load_commits()
        assert len(session.commits) >= 1

        # Step 3: Create formal commit
        indices = list(range(len(session.commits)))
        session.selected_workspace = set(indices)
        fc = session.step_create_formal_commit(
            indices, message="[TEST-100] feat: full pipeline test",
        )
        assert fc is not None

        # Step 4: Status 应该有 formal commits
        status = session.status_dict()
        assert "stage" in status
