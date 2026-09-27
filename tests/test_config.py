"""测试 Config — trial_path, sync_status, 反序列化"""

from __future__ import annotations

import json
from pathlib import Path

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
        assert cfg.verbose is False
        assert cfg.auto_compact is True

    def test_host_tool_preferences_round_trip(self):
        cfg = Config.from_dict({
            "projects": [],
            "external_editor": r"C:\\Tools\\editor.exe",
            "web_search_mode": "auto",
            "web_search_endpoint": "https://search.example.test/search",
        })
        assert cfg.external_editor.endswith("editor.exe")
        assert cfg.web_search_endpoint == "https://search.example.test/search"
        assert cfg.web_search_mode == "auto"


class TestConfigManagerLegacy:
    def test_default_path_fallback(self, monkeypatch):
        import sys
        # 模拟 frozen 环境
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        path = ConfigManager.default_path()
        assert path.name == ConfigManager.CONFIG_FILE

    def test_default_path_is_user_scoped_and_legacy_repo_config_is_migrated(
        self, monkeypatch, tmp_path_factory,
    ):
        workspace = tmp_path_factory / "workspace"
        user_home = tmp_path_factory / "home"
        workspace.mkdir()
        user_home.mkdir()
        legacy = workspace / "gitgo_config.json"
        legacy.write_text('{"projects": [], "verbose": true}', encoding="utf-8")
        monkeypatch.chdir(workspace)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: user_home))
        monkeypatch.delenv("GITGO_CONFIG_PATH", raising=False)

        loaded = ConfigManager.load()

        assert loaded.verbose is True
        assert ConfigManager.default_path() == user_home / ".gitgo" / "config.json"
        assert ConfigManager.default_path().exists()
        assert not legacy.exists()
        assert list((user_home / ".gitgo" / "migrations").glob(
            "gitgo_config.json.legacy-imported-*"
        ))

    def test_explicit_config_path_never_scans_or_moves_cwd_legacy_config(
        self, monkeypatch, tmp_path_factory,
    ):
        workspace = tmp_path_factory / "live-workspace"
        isolated = tmp_path_factory / "isolated" / "config.json"
        workspace.mkdir()
        legacy = workspace / "gitgo_config.json"
        legacy.write_text('{"projects": [], "verbose": true}', encoding="utf-8")
        monkeypatch.chdir(workspace)
        monkeypatch.setenv("GITGO_CONFIG_PATH", str(isolated))

        loaded = ConfigManager.load()

        assert loaded.verbose is False
        assert legacy.exists()
        assert not isolated.exists()


def test_legacy_privacy_fields_migrate_to_canonical_outbound_policy():
    project = ProjectConfig.from_dict({
        "name": "P",
        "workspace": {"file_access": {"kind": "local", "path": "/ws"}},
        "force_exclude": ["CLAUDE.md", ".env"],
        "security_scan": {"enabled": False, "severity_threshold": "high"},
        "authorship": {"privacy": {"level": 3, "deep_scan": True}},
    })
    assert project.outbound_policy["enabled"] is False
    assert project.outbound_policy["exclude_paths"] == ["CLAUDE.md", ".env"]
    assert project.outbound_policy["severity_threshold"] == "high"
    assert project.outbound_policy["content_level"] == 3
    assert project.outbound_policy["deep_scan"] is True
