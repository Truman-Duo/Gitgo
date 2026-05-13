"""gitgo 核心工作流模块。

公开 API 一览：:

    # 数据类
    FileEntry, CommitInfo, FormalCommit

    # 枚举
    SessionStage

    # 工作流类
    SyncSession

    # 工作流函数
    scan_workspace, compare_files, get_git_log,
    build_commit_template, validate_commit_message,
    sync_to_backup, get_exclude_patterns, get_file_diff,
    push_to_backup

依赖模块：config, plugin_loader, history
"""

from __future__ import annotations

from backend.core.operations import (
    CommitInfo,
    FileEntry,
    _find_next_number,
    build_commit_template,
    compare_files,
    get_exclude_patterns,
    get_file_diff,
    get_git_log,
    get_trial_log,
    push_to_backup,
    scan_workspace,
    sync_to_backup,
    validate_commit_message,
)

from backend.core.sync_session import FormalCommit, SessionStage, SyncSession

__all__ = [
    "FileEntry",
    "CommitInfo",
    "FormalCommit",
    "SessionStage",
    "SyncSession",
    "_find_next_number",
    "scan_workspace",
    "compare_files",
    "get_git_log",
    "build_commit_template",
    "validate_commit_message",
    "sync_to_backup",
    "get_exclude_patterns",
    "get_file_diff",
    "get_trial_log",
    "push_to_backup",
]
