"""测试 LocalGitRunner — 本地 git 命令实现（使用真实 git 仓库）"""

from __future__ import annotations

from pathlib import Path

import pytest


class TestLocalGitRunner:
    """全部使用 git_repo fixture（真实 git init + initial commit）。"""

    def test_is_git_repo(self, git_runner):
        assert git_runner.is_git_repo()

    def test_is_git_repo_false(self, tmp_path_factory):
        from backend.adapters.local_git_runner import LocalGitRunner
        runner = LocalGitRunner(tmp_path_factory / "not_repo")
        assert not runner.is_git_repo()

    def test_rev_parse_head(self, git_runner):
        sha = git_runner.rev_parse("HEAD")
        assert sha is not None
        assert len(sha) == 40

    def test_rev_parse_invalid_ref(self, git_runner):
        assert git_runner.rev_parse("INVALID_REF_12345") is None

    def test_add_all_and_commit(self, git_runner, git_repo: Path):
        new_file = git_repo / "new.txt"
        new_file.write_text("new content")
        assert git_runner.add_all()
        ok, err = git_runner.commit("test: new file")
        assert ok, f"commit failed: {err}"
        # 验证 commit 存在
        sha = git_runner.rev_parse("HEAD")
        assert sha is not None

    def test_log(self, git_runner):
        entries = git_runner.log()
        assert len(entries) >= 1
        assert "|||" in entries[0]

    def test_log_with_custom_format(self, git_runner):
        entries = git_runner.log(fmt="%H|||%an", max_count=1)
        assert len(entries) == 1
        parts = entries[0].split("|||")
        assert len(parts) == 2
        assert len(parts[0]) == 40  # SHA
        assert parts[1] == "Tester"

    def test_log_since_hash(self, git_runner, git_repo: Path):
        # 获取 initial commit 的 hash
        first = git_runner.rev_parse("HEAD")
        # 再做一个 commit
        (git_repo / "f2.txt").write_text("2")
        git_runner.add_all()
        git_runner.commit("second")

        # 从 first 开始查 — 应该只返回第二个
        entries = git_runner.log(since_hash=first)
        assert len(entries) == 1
        # 用 reverse=False 确认顺序
        entries_desc = git_runner.log(since_hash=first, reverse=False)
        assert len(entries_desc) == 1

    def test_log_with_grep(self, git_runner, git_repo: Path):
        (git_repo / "f3.txt").write_text("3")
        git_runner.add_all()
        git_runner.commit(feat_msg := "feat: important feature")

        entries = git_runner.log(grep="feat")
        assert len(entries) >= 1

    def test_diff(self, git_runner, git_repo: Path):
        (git_repo / "README.md").write_text("# Modified")
        diff = git_runner.diff()
        assert diff != ""
        assert "Modified" in diff

    def test_diff_no_changes(self, git_runner):
        diff = git_runner.diff()
        assert diff == ""

    def test_push_to_nowhere_fails(self, git_runner):
        ok, err = git_runner.push("nonexistent")
        assert not ok

    def test_fetch_to_nowhere_fails(self, git_runner):
        ok, err = git_runner.fetch("nonexistent")
        assert not ok

    def test_cherry_pick_nonexistent(self, git_runner):
        ok, err = git_runner.cherry_pick("0000000000000000000000000000000000000000")
        assert not ok

    def test_run_timeout(self, git_runner):
        from backend.adapters.git_runner import CompletedProcess
        r = git_runner.run(["--help"], timeout=5.0)
        assert r.returncode == 0
        assert "usage:" in r.stdout

    def test_completed_process(self):
        from backend.adapters.git_runner import CompletedProcess
        cp = CompletedProcess(["git", "test"], 0, "out", "err")
        assert cp.returncode == 0
        assert cp.stdout == "out"
        assert cp.stderr == "err"
