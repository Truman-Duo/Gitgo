"""pytest 共享 fixtures"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from backend.adapters.local_file_adapter import LocalFileAdapter
from backend.adapters.local_git_runner import LocalGitRunner


@pytest.fixture
def tmp_path_factory() -> Iterator[Path]:
    """创建临时目录，测试后自动清理。"""
    with tempfile.TemporaryDirectory(prefix="gitgo_test_") as d:
        yield Path(d)


@pytest.fixture
def file_adapter(tmp_path_factory: Path) -> LocalFileAdapter:
    return LocalFileAdapter(tmp_path_factory)


@pytest.fixture
def git_repo(tmp_path_factory: Path) -> Path:
    """初始化一个 git 仓库，做一次 initial commit。"""
    repo = tmp_path_factory / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, capture_output=True)
    readme = repo / "README.md"
    readme.write_text("# Test")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, capture_output=True)
    return repo


@pytest.fixture
def git_runner(git_repo: Path) -> LocalGitRunner:
    return LocalGitRunner(git_repo)


@pytest.fixture
def config_json_str() -> str:
    return """{
    "projects": [
        {
            "name": "TestProject",
            "workspace": {
                "file_access": {"kind": "local", "path": "/tmp/ws"},
                "last_known_head": "abc123"
            },
            "release": {
                "file_access": {"kind": "local", "path": "/tmp/bk"}
            },
            "trial": {
                "file_access": {"kind": "local", "path": "/tmp/trial"}
            },
            "commit_format": {"prefix": "TEST", "number_start": 0, "padding": false, "plugins": []},
            "force_exclude": [],
            "security_scan": {"enabled": true, "severity_threshold": "medium", "ignored_rules": [], "extra_patterns": []}
        }
    ],
    "language": "zh"
}"""
