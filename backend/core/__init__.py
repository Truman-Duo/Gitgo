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

from importlib import import_module

_EXPORTS = {
    **dict.fromkeys(("CommitInfo", "FileEntry", "_find_next_number", "build_commit_template",
                    "compare_files", "get_exclude_patterns", "get_file_diff", "get_git_log",
                    "get_trial_log", "push_to_backup", "scan_workspace", "sync_to_backup",
                    "validate_commit_message"), "backend.core.operations"),
    **dict.fromkeys(("FormalCommit", "SessionStage", "SyncSession"), "backend.core.sync_session"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_EXPORTS[name]), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))

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
