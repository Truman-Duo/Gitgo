"""Workspace scan and comparison — pure functions, no session dependency."""

from __future__ import annotations
from pathlib import Path
from typing import Callable

from backend.core.operations.scan import scan_workspace, get_exclude_patterns, compare_files
from backend.core.operations.models import FileEntry
from backend.core.config import ProjectConfig


def scan_and_compare(
    workspace_path: str | Path,
    backup_path: str | Path,
    *,
    project: ProjectConfig | None = None,
    file_list: list[str] | None = None,
    ws_adapter=None,
    bk_adapter=None,
    on_progress: Callable | None = None,
    normalize_eol: bool = True,
) -> list[FileEntry]:
    """扫描工作区并对比备份仓库。

    如果 file_list 不为空，只扫描指定文件（增量模式）。
    否则扫描 workspace 全部文件（全量模式）。
    """
    ws = Path(workspace_path)
    bp = Path(backup_path)

    if file_list:
        files = file_list
    else:
        exclude = []
        if project:
            exclude = get_exclude_patterns(project, ws, file_adapter=ws_adapter)
        files = scan_workspace(ws, exclude, file_adapter=ws_adapter)

    return compare_files(
        ws, bp, files, on_progress,
        ws_adapter=ws_adapter, bk_adapter=bk_adapter,
        normalize_eol=normalize_eol,
    )


def scan_incremental(
    workspace_path: str | Path,
    backup_path: str | Path,
    changed_files: list[str],
    *,
    ws_adapter=None,
    bk_adapter=None,
    on_progress: Callable | None = None,
    normalize_eol: bool = True,
) -> list[FileEntry]:
    """增量扫描——只对比指定文件列表。"""
    return scan_and_compare(
        workspace_path, backup_path,
        file_list=changed_files,
        ws_adapter=ws_adapter, bk_adapter=bk_adapter,
        on_progress=on_progress, normalize_eol=normalize_eol,
    )
