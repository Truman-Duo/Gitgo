"""Commit message submission — 验证并创建正式 commit"""

from typing import Optional

from backend.core.config import ProjectConfig
from backend.core import (
    FormalCommit,
    SyncSession,
    validate_commit_message,
)


def submit_commit_message(
    session: SyncSession,
    project: ProjectConfig,
    message: str,
) -> Optional[FormalCommit]:
    """验证 commit message → 通过 step_create_formal_commit 创建 FormalCommit。"""
    message = message.strip()
    if not message:
        return None

    err = validate_commit_message(message)
    if err:
        return None

    return session.step_create_formal_commit(message=message, selected_indices=set())
