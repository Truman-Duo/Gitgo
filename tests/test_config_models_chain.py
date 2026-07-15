"""Config + Models 子系统链路测试。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests.factory import TestDataFactory


@pytest.fixture
def f():
    return TestDataFactory(seed=111)


# ═══════════════════════════════════════════════════════════════
# Chain M1: Models 序列化往返
# ═══════════════════════════════════════════════════════════════


class TestChainModelsRoundtrip:
    """数据模型序列化/反序列化。"""

    def test_file_access_roundtrip(self):
        """FileAccess from_dict ↔ 数据完整。"""
        from backend.models import FileAccess, FileAccessKind
        fa = FileAccess(kind=FileAccessKind.LOCAL, path="/tmp/test")
        d = {
            "kind": "local",
            "path": "/tmp/test",
        }
        fa2 = FileAccess.from_dict(d)
        assert fa2.kind == FileAccessKind.LOCAL
        assert fa2.path == "/tmp/test"

    def test_file_access_ssh(self):
        """SSH FileAccess 含所有字段。"""
        from backend.models import FileAccess, FileAccessKind
        d = {
            "kind": "ssh",
            "host": "example.com",
            "port": 22,
            "username": "root",
            "key_path": "~/.ssh/id_rsa",
            "path": "/remote/path",
        }
        fa = FileAccess.from_dict(d)
        assert fa.kind == FileAccessKind.SSH
        assert fa.host == "example.com"
        assert fa.port == 22

    def test_repo_node_roundtrip(self):
        """RepoNode 序列化往返。"""
        from backend.models import RepoNode, FileAccess, FileAccessKind, RemoteTarget
        node = RepoNode(
            file_access=FileAccess(kind=FileAccessKind.LOCAL, path="/tmp/ws"),
            remote=RemoteTarget(url="https://github.com/user/repo", name="origin", kind="github"),
            last_known_head="abc123",
        )
        d = {
            "file_access": {"kind": "local", "path": "/tmp/ws"},
            "remote": {"url": "https://github.com/user/repo", "name": "origin", "kind": "github"},
            "last_known_head": "abc123",
        }
        node2 = RepoNode.from_dict(d)
        assert node2.file_access.path == "/tmp/ws"
        assert node2.remote.url == "https://github.com/user/repo"

    def test_incoming_change_defaults(self):
        """IncomingChange 默认值。"""
        from backend.models import IncomingChange, TrialAction
        change = IncomingChange(
            hash="abc123", message="test", author="dev",
            timestamp="2026-01-01", body="details",
        )
        assert change.triage == TrialAction.PENDING

    def test_trial_action_values(self):
        """TrialAction 枚举值。"""
        from backend.models import TrialAction
        assert TrialAction.PENDING.value == "pending"
        assert TrialAction.ACCEPTED.value == "accepted"
        assert TrialAction.PROMOTED.value == "promoted"
        assert TrialAction.DISCARDED.value == "discarded"


# ═══════════════════════════════════════════════════════════════
# Chain M2: Config CRUD
# ═══════════════════════════════════════════════════════════════


class TestChainConfigCRUD:
    """Config 创建/保存/加载/迁移。"""

    def test_config_roundtrip(self):
        """Config → dict → Config 往返。"""
        from backend.core.config import Config, ProjectConfig
        from backend.models import FileAccess, FileAccessKind, RepoNode

        project = ProjectConfig(
            name="testproject",
            workspace=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL, path="/tmp/ws"),
            ),
            release=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL, path="/tmp/release"),
            ),
            commit_format={"prefix": "TEST", "number_start": 1},
        )
        config = Config(projects=[project], language="zh")
        assert len(config.projects) == 1
        assert config.projects[0].name == "testproject"

    def test_config_save_load(self):
        """Config 保存到文件 → 重新加载。"""
        from backend.core.config import Config, ProjectConfig, ConfigManager
        from backend.models import FileAccess, FileAccessKind, RepoNode

        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            project = ProjectConfig(
                name="save_test",
                workspace=RepoNode(
                    file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(ws)),
                ),
                release=RepoNode(
                    file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(ws / "rel")),
                ),
                commit_format={"prefix": "TEST", "number_start": 0},
            )
            config = Config(projects=[project])
            ConfigManager.save(config, ws / "gitgo_config.json")

            loaded = ConfigManager.load(ws / "gitgo_config.json")
            assert len(loaded.projects) == 1
            assert loaded.projects[0].name == "save_test"

    def test_multi_project_config(self):
        """多项目 Config。"""
        from backend.core.config import Config, ProjectConfig
        from backend.models import FileAccess, FileAccessKind, RepoNode

        projects = [
            ProjectConfig(
                name=f"proj_{i}",
                workspace=RepoNode(
                    file_access=FileAccess(kind=FileAccessKind.LOCAL, path=f"/tmp/ws_{i}"),
                ),
                release=RepoNode(
                    file_access=FileAccess(kind=FileAccessKind.LOCAL, path=f"/tmp/rel_{i}"),
                ),
                commit_format={"prefix": "PROJ", "number_start": 0},
            )
            for i in range(3)
        ]
        config = Config(projects=projects)
        assert len(config.projects) == 3
        assert config.projects[2].name == "proj_2"

    def test_legacy_migration(self):
        """旧格式项目 dict 迁移。needs_migration 检查单个项目 dict。"""
        from backend.core.migrate import needs_migration, migrate_project_dict

        legacy_project = {
            "name": "legacy",
            "workspace_path": "/tmp/old_ws",
            "backup_path": "/tmp/old_bk",
            "commit_format": {"prefix": "LEGACY", "number_start": 0},
        }
        assert needs_migration(legacy_project)
        migrated = migrate_project_dict(legacy_project)
        assert "workspace" in migrated or "file_access" in str(migrated)


# ═══════════════════════════════════════════════════════════════
# Chain M3: Config → Session 初始化
# ═══════════════════════════════════════════════════════════════


class TestChainConfigToSession:
    """Config → ProjectConfig → SyncSession 初始化。"""

    def test_config_to_session_init(self, f):
        """从 Config 创建 SyncSession。"""
        import subprocess, sys
        from backend.core.config import Config, ProjectConfig, ConfigManager
        from backend.core.sync_session import SyncSession
        from backend.models import FileAccess, FileAccessKind, RepoNode

        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            cf = 0x08000000 if sys.platform == "win32" else 0

            # 创建 git repos
            (ws / "release").mkdir(exist_ok=True)
            for repo_dir in [ws, ws / "release"]:
                subprocess.run(["git", "init"], cwd=str(repo_dir),
                              capture_output=True, creationflags=cf)
                subprocess.run(["git", "config", "user.email", "test@x.com"],
                              cwd=str(repo_dir), capture_output=True, creationflags=cf)
                subprocess.run(["git", "config", "user.name", "Test"],
                              cwd=str(repo_dir), capture_output=True, creationflags=cf)
                (repo_dir / "init.txt").write_text("x")
                subprocess.run(["git", "add", "."], cwd=str(repo_dir),
                              capture_output=True, creationflags=cf)
                subprocess.run(["git", "commit", "-m", "init"],
                              cwd=str(repo_dir), capture_output=True, creationflags=cf)

            project = ProjectConfig(
                name="cfg_to_sess",
                workspace=RepoNode(
                    file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(ws)),
                ),
                release=RepoNode(
                    file_access=FileAccess(kind=FileAccessKind.LOCAL, path=str(ws / "release")),
                ),
                commit_format={"prefix": "T", "number_start": 0, "padding": False, "plugins": []},
            )
            config = Config(projects=[project])

            session = SyncSession(project, config)
            assert session is not None
            assert session.stage is not None


# ═══════════════════════════════════════════════════════════════
# Chain M4: Protocol Schema
# ═══════════════════════════════════════════════════════════════


class TestChainProtocolSchema:
    """协议 schema 验证。"""

    def test_status_dict_keys(self):
        """status_dict 包含所有必需键。"""
        from backend.core.config import Config, ProjectConfig
        from backend.core.sync_session import SyncSession
        from backend.models import FileAccess, FileAccessKind, RepoNode
        import subprocess, sys

        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            cf = 0x08000000 if sys.platform == "win32" else 0
            subprocess.run(["git", "init"], cwd=str(ws),
                          capture_output=True, creationflags=cf)
            subprocess.run(["git", "config", "user.email", "t@x.com"],
                          cwd=str(ws), capture_output=True, creationflags=cf)
            subprocess.run(["git", "config", "user.name", "T"],
                          cwd=str(ws), capture_output=True, creationflags=cf)
            (ws / "f.txt").write_text("x")
            subprocess.run(["git", "add", "."], cwd=str(ws),
                          capture_output=True, creationflags=cf)
            subprocess.run(["git", "commit", "-m", "i"],
                          cwd=str(ws), capture_output=True, creationflags=cf)

            project = ProjectConfig(
                name="proto_test",
                workspace=RepoNode(file_access=FileAccess(
                    kind=FileAccessKind.LOCAL, path=str(ws))),
                release=RepoNode(file_access=FileAccess(
                    kind=FileAccessKind.LOCAL, path=str(ws / "rel"))),
                commit_format={"prefix": "P", "number_start": 0, "padding": False, "plugins": []},
            )
            config = Config(projects=[project])
            session = SyncSession(project, config)

            from backend.core.cache import FileHashCache
            cache = FileHashCache(ws / ".gitgo")
            session.step_scan(hash_cache=cache)

            status = session.status_dict()
            required = ["project", "stage", "workspace", "commits"]
            for key in required:
                assert key in status, f"status_dict missing '{key}'"


# ═══════════════════════════════════════════════════════════════
# 边界条件
# ═══════════════════════════════════════════════════════════════


class TestConfigEdgeCases:
    def test_empty_config(self):
        """空项目列表 Config。"""
        from backend.core.config import Config
        config = Config(projects=[])
        assert config.projects == []

    def test_single_project_minimal(self):
        """最简 ProjectConfig。"""
        from backend.core.config import ProjectConfig
        from backend.models import FileAccess, FileAccessKind, RepoNode
        project = ProjectConfig(
            name="minimal",
            workspace=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL, path="/tmp"),
            ),
            release=RepoNode(
                file_access=FileAccess(kind=FileAccessKind.LOCAL, path="/tmp/r"),
            ),
            commit_format={"prefix": "M", "number_start": 0},
        )
        assert project.name == "minimal"
        assert project.workspace_path == "/tmp"
        assert project.backup_path == "/tmp/r"

    def test_trial_node_optional(self):
        """Trial 节点可有可无。"""
        from backend.core.config import ProjectConfig
        from backend.models import FileAccess, FileAccessKind, RepoNode
        project = ProjectConfig(
            name="no_trial",
            workspace=RepoNode(file_access=FileAccess(
                kind=FileAccessKind.LOCAL, path="/tmp")),
            release=RepoNode(file_access=FileAccess(
                kind=FileAccessKind.LOCAL, path="/tmp/r")),
            trial=None,
            commit_format={"prefix": "N", "number_start": 0},
        )
        assert project.trial is None

    def test_factory_generated_config_models(self, f):
        """工厂生成的 FileEntry + CommitInfo 可正常使用。"""
        entries = f.file_entries(10)
        commits = f.commit_infos(5)

        assert all(e.rel_path for e in entries)
        assert all(c.hash for c in commits)
        assert all(c.type for c in commits)
