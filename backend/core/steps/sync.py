"""Sync and push steps — pure functions for loop integration."""

from __future__ import annotations
from pathlib import Path
from typing import Callable

from backend.core.operations.models import FileEntry
from backend.core.operations.sync import sync_to_backup, push_to_backup


def sync_files(
    workspace_path: str | Path,
    backup_path: str | Path,
    files: list[FileEntry],
    commit_message: str,
    *,
    ws_adapter=None,
    bk_adapter=None,
    git_runner=None,
    on_progress: Callable | None = None,
    plugin_ids: list[str] | None = None,
) -> bool:
    """同步文件从 workspace 到 backup 仓库。返回成功/失败。"""
    return sync_to_backup(
        files, commit_message,
        str(workspace_path), str(backup_path),
        on_progress,
        plugin_ids=plugin_ids,
        ws_adapter=ws_adapter, bk_adapter=bk_adapter,
        git_runner=git_runner,
    )


def push_to_remote(
    backup_path: str | Path,
    *,
    git_runner=None,
    skip_scan: bool = False,
    progress_callback: Callable | None = None,
) -> tuple[bool, list[str]]:
    """推送到远程仓库。返回 (success, warnings)。"""
    bp = str(backup_path)
    return push_to_backup(
        bp,
        progress_callback=progress_callback,
        skip_scan=skip_scan,
    )
