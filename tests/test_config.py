"""测试 Config — trial_path, sync_status, 反序列化"""

from __future__ import annotations

import json

from backend.core.config import Config, ConfigManager, ProjectConfig
from backend.models import FileAccessKind, SyncStatus


class TestProjectConfigTrial:
    def test_trial_path_default(self):
        p = ProjectConfig()
        assert p.trial_path == ""

    def test_trial_path_setter_creates_trial_node(self):
        p = ProjectConfig()
        p.trial_path = "/tmp/trial"
        assert p.trial_path == "/tmp/trial"
        assert p.trial is not None
        assert p.trial.file_access.path == "/tmp/trial"

    def test_trial_path_setter_twice(self):
        p = ProjectConfig()
        p.trial_path = "/tmp/trial1"
        p.trial_path = "/tmp/trial2"
        assert p.trial_path == "/tmp/trial2"

    def test_trial_node_initially_none(self):
        p = ProjectConfig()
        assert p.trial is None


class TestProjectConfigSyncStatus:
    def test_sync_status_missing_when_no_path(self):
        p = ProjectConfig()
        assert p.sync_status == SyncStatus.MISSING

    def test_sync_status_empty_when_local_path_missing_git(self, tmp_path_factory):
        p = ProjectConfig()
        p.release.file_access.path = str(tmp_path_factory / "nonexistent")
        assert p.sync_status == SyncStatus.EMPTY

    def test_sync_status_valid_for_local_git_repo(self, git_repo):
        p = ProjectConfig()
        p.release.file_access.path = str(git_repo)
        assert p.sync_status == SyncStatus.VALID

    def test_sync_status_ssh_without_host(self):
        p = ProjectConfig()
        p.release.file_access.kind = FileAccessKind.SSH
        p.release.file_access.path = "/remote/repo"
        assert p.sync_status == SyncStatus.EMPTY

    def test_sync_status_ssh_valid(self):
        p = ProjectConfig()
        p.release.file_access.kind = FileAccessKind.SSH
        p.release.file_access.host = "example.com"
        p.release.file_access.path = "/remote/repo"
        assert p.sync_status == SyncStatus.VALID


class TestProjectConfigFromDict:
    def test_from_dict_with_trial(self):
        d = {
            "name": "P",
            "workspace": {"file_access": {"kind": "local", "path": "/ws"}},
            "release": {"file_access": {"kind": "local", "path": "/bk"}},
            "trial": {"file_access": {"kind": "local", "path": "/tr"}},
            "commit_format": {},
            "force_exclude": [],
        }
        p = ProjectConfig.from_dict(d)
        assert p.trial_path == "/tr"
        assert p.name == "P"

    def test_from_dict_without_trial(self):
        d = {
            "name": "P",
            "workspace": {"file_access": {"kind": "local", "path": "/ws"}},
            "release": {"file_access": {"kind": "local", "path": "/bk"}},
            "commit_format": {},
            "force_exclude": [],
        }
        p = ProjectConfig.from_dict(d)
        assert p.trial is None
        assert p.trial_path == ""


class TestConfigFromDict:
    def test_config_with_trial_project(self, config_json_str):
        d = json.loads(config_json_str)
        cfg = Config.from_dict(d)
        assert len(cfg.projects) == 1
        p = cfg.projects[0]
        assert p.trial_path == "/tmp/trial"
        assert p.trial is not None

    def test_config_language(self, config_json_str):
        d = json.loads(config_json_str)
        cfg = Config.from_dict(d)
        assert cfg.language == "zh"


class TestConfigManagerLegacy:
    def test_default_path_fallback(self, monkeypatch):
        import sys
        # 模拟 frozen 环境
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        path = ConfigManager.default_path()
        assert path.name == ConfigManager.CONFIG_FILE
