"""测试 operations — 特别是 Phase 4 的 get_trial_log"""

from __future__ import annotations

import subprocess

from backend.core.operations import get_trial_log


class TestGetTrialLog:
    def test_no_since_hash_returns_empty(self, tmp_path_factory):
        """since_hash=None 时只记录 HEAD，返回空列表"""
        changes = get_trial_log(str(tmp_path_factory), since_hash=None)
        assert changes == []

    def test_returns_incoming_change_objects(self, git_repo):
        """正常情况返回 IncomingChange 列表"""
        first = _rev_parse(git_repo)
        changes = get_trial_log(str(git_repo), since_hash=first)
        # since_hash==HEAD 时应该为空
        assert len(changes) == 0

    def test_with_new_commits(self, git_repo):
        first = _rev_parse(git_repo)
        # 做一个新 commit
        (git_repo / "new.txt").write_text("new")
        subprocess.run(["git", "add", "-A"], cwd=git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "feat: new file"], cwd=git_repo, capture_output=True)

        changes = get_trial_log(str(git_repo), since_hash=first)
        assert len(changes) == 1
        assert changes[0].message == "feat: new file"
        assert len(changes[0].hash) == 40
        assert changes[0].triage.value == "pending"

    def test_multiple_new_commits(self, git_repo):
        first = _rev_parse(git_repo)
        for i in range(3):
            (git_repo / f"f{i}.txt").write_text(str(i))
            subprocess.run(["git", "add", "-A"], cwd=git_repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"commit {i}"], cwd=git_repo, capture_output=True)

        changes = get_trial_log(str(git_repo), since_hash=first)
        assert len(changes) == 3
        # 应该是按时间顺序排列
        for i, c in enumerate(changes):
            assert c.message == f"commit {i}"

    def test_body_content(self, git_repo):
        first = _rev_parse(git_repo)
        (git_repo / "a.txt").write_text("a")
        subprocess.run(["git", "add", "-A"], cwd=git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "feat: with body\n\nLong description here."], cwd=git_repo, capture_output=True)

        changes = get_trial_log(str(git_repo), since_hash=first)
        assert len(changes) == 1
        assert "Long description here." in changes[0].body


def _rev_parse(repo_path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path, capture_output=True, text=True,
    )
    return r.stdout.strip()
