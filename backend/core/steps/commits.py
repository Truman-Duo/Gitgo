"""Commit loading and formalization — pure functions."""

from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING

from backend.core.operations.models import CommitInfo
from backend.core.operations.git import get_git_log, _find_next_number, build_commit_template
from backend.core.config import ProjectConfig

if TYPE_CHECKING:
    from backend.adapters.git_runner import GitRunner
    from backend.core.sync_session import FormalCommit


def load_workspace_commits(
    workspace_path: str | Path,
    sync_base: str = "",
    git_runner=None,
) -> list[CommitInfo]:
    """从 workspace git log 加载自上次 sync 以来的 commits。"""
    ws = Path(workspace_path)
    if git_runner is None:
        from backend.adapters.local_git_runner import LocalGitRunner
        git_runner = LocalGitRunner(ws)
    if not git_runner.is_git_repo():
        return []
    return get_git_log(ws, sync_base or None, git_runner=git_runner)


def create_formal_commit(
    commits: list[CommitInfo],
    project: ProjectConfig,
    workspace_path: str = "",
    backup_path: str = "",
    git_runner: "GitRunner | None" = None,
    message: str | None = None,
    template_name: str | None = None,
) -> "FormalCommit | None":
    """从选中的 workspace commits 创建 formal commit。"""
    from backend.core.sync_session import FormalCommit

    if not commits:
        return None

    prefix = project.commit_format.get("prefix", "ANBM")
    number_start = project.commit_format.get("number_start", 0)

    max_n = max((number_start - 1), -1)
    repo_max = _find_next_number(
        backup_path, prefix,
        git_runner=git_runner,
        workspace_path=workspace_path,
    )
    next_n = max(max_n + 1, repo_max)

    if workspace_path:
        counter_file = Path(workspace_path) / ".gitgo" / "next_number"
        counter_file.parent.mkdir(parents=True, exist_ok=True)
        counter_file.write_text(str(next_n))

    msg = message or build_commit_template(
        commits, project,
        template_name=template_name,
        git_runner=git_runner,
    )

    fc = FormalCommit(
        prefix=prefix,
        number=next_n,
        message=msg,
        source_indices=tuple(range(len(commits))),
        synced=False,
        pushed=False,
    )
    return fc
