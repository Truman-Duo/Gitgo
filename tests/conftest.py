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
from backend.core.storage import close_owned_storage


@pytest.fixture(autouse=True)
def isolate_default_gitgo_state(
    tmp_path_factory: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Never let a test's implicit workspace write the user's live state.

    Most storage tests inject ``state_home`` explicitly, but older unittest
    cases legitimately use ``Path('.')`` or omit a workspace.  Redirect the
    default facade for every test and close its process-level LRU on both
    boundaries so a cached runtime cannot outlive the temporary directory.
    """
    close_owned_storage()
    # Runtime evidence must remain outside every tool execution workspace.
    with tempfile.TemporaryDirectory(prefix="gitgo_host_state_") as state_dir:
        root = Path(state_dir)
        monkeypatch.setenv("GITGO_STATE_HOME", str(root / "state"))
        monkeypatch.setenv("GITGO_CONFIG_PATH", str(root / "config" / "config.json"))
        try:
            yield
        finally:
            close_owned_storage()


@pytest.fixture
def tmp_path_factory() -> Iterator[Path]:
    """创建临时目录，测试后自动清理。"""
    with tempfile.TemporaryDirectory(prefix="gitgo_test_") as d:
        yield Path(d).resolve()


@pytest.fixture
def file_adapter(tmp_path_factory: Path) -> LocalFileAdapter:
    return LocalFileAdapter(tmp_path_factory)


@pytest.fixture
def git_repo(tmp_path_factory: Path) -> Path:
    """初始化一个 git 仓库，做一次 initial commit。"""
    repo = tmp_path_factory / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@local.gitgo.invalid"],
        cwd=repo, capture_output=True,
    )
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


# ── v0.35: TestDataFactory fixtures ──────────────────────

@pytest.fixture
def factory():
    """固定种子 factory（CI 确定性测试）。"""
    from tests.factory import TestDataFactory
    return TestDataFactory(seed=42)


@pytest.fixture
def runner_transport_only(monkeypatch):
    """Test runner/protocol semantics independently of machine ACL provisioning.

    Native containment is separately exercised without this mock in
    test_native_sandbox.py. This fixture is opt-in, never applied globally.
    """
    def launch(argv, policy, **kwargs):
        return subprocess.Popen(argv, **kwargs)
    monkeypatch.setattr("backend.core.sandbox.sandbox_popen", launch)
