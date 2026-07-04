"""扫描与对比 — scan_workspace / compare_files / get_file_diff / get_exclude_patterns"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from backend.adapters import FileAdapter, LocalFileAdapter
from backend.core.config import ProjectConfig

from .models import FileEntry
from .utils import _normalize_path, _is_excluded, _read_gitignore


def scan_workspace(
    workspace: str | Path = "",
    exclude_patterns: list[str] | None = None,
    *,
    file_adapter: FileAdapter | None = None,
) -> list[str]:
    """扫描工作区，返回所有未排除文件的相对路径列表"""
    if file_adapter is None:
        file_adapter = LocalFileAdapter(Path(workspace).resolve())
    results: list[str] = []
    for dirpath, dirnames, filenames in file_adapter.walk(""):
        if ".git" in dirnames:
            dirnames.remove(".git")

        for fn in filenames:
            rel_path = f"{dirpath}/{fn}" if dirpath else fn
            try:
                if file_adapter.is_symlink(rel_path):
                    continue
                rel = _normalize_path(rel_path)
                if not _is_excluded(rel, exclude_patterns or []):
                    results.append(rel)
            except (ValueError, OSError):
                continue
    return sorted(results)


def compare_files(
    workspace: str | Path = "",
    backup: str | Path = "",
    file_list: list[str] | None = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    *,
    ws_adapter: FileAdapter | None = None,
    bk_adapter: FileAdapter | None = None,
    normalize_eol: bool = False,
    hash_cache: "FileHashCache | None" = None,
) -> list[FileEntry]:
    """对比工作区和备份仓库的文件，返回带状态的文件列表

    normalize_eol=True 时将 \\r\\n 替换为 \\n 再比较，
    避免 Windows/Linux 换行符差异导致误报。
    """
    def _hash(adapter: FileAdapter, path: str) -> str:
        # Cache lookup: stat mtime+size → check cache for sha256
        cached_sha = None
        if hash_cache is not None and not normalize_eol:
            import os
            try:
                full = Path(_resolve_adapter_root(adapter, workspace, backup)) / path
                st = os.stat(full)
                cached_sha = hash_cache.lookup(path, st.st_mtime, st.st_size)
            except OSError:
                pass
        if cached_sha is not None:
            return cached_sha

        if not normalize_eol:
            h = adapter.hash_file(path)
        else:
            data = adapter.read_bytes(path)
            data = data.replace(b"\r\n", b"\n")
            import hashlib
            h = hashlib.sha256(data).hexdigest()

        # Store in cache
        if hash_cache is not None and not normalize_eol:
            try:
                hash_cache.store(path, st.st_mtime, st.st_size, h)
            except (OSError, UnboundLocalError):
                pass

        return h

    def _resolve_adapter_root(adapter: FileAdapter, ws: str | Path,
                               bk: str | Path) -> Path:
        """从 adapter 获取根目录路径，用于 os.stat。"""
        root = getattr(adapter, "_root", None)
        if root:
            return Path(root)
        # Fallback: guess by checking if adapter's root matches workspace or backup
        if adapter is ws_adapter:
            return Path(ws).resolve()
        return Path(bk).resolve()

    if ws_adapter is None:
        ws_adapter = LocalFileAdapter(Path(workspace).resolve())
    if bk_adapter is None:
        bk_adapter = LocalFileAdapter(Path(backup).resolve())

    total = len(file_list) if file_list else 0
    entries: list[FileEntry] = []
    backup_by_hash: dict[str, list[str]] = {}
    ws_by_hash: dict[str, list[str]] = {}

    if total > 0 and progress_callback:
        progress_callback(0, total, "正在扫描备份仓库...")
    for dirpath, dirnames, filenames in bk_adapter.walk(""):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for fn in filenames:
            rel_path = f"{dirpath}/{fn}" if dirpath else fn
            try:
                if bk_adapter.is_symlink(rel_path):
                    continue
                rel = _normalize_path(rel_path)
                if not rel.startswith(".git/"):
                    backup_by_hash.setdefault(
                        _hash(bk_adapter, rel_path), []
                    ).append(rel)
            except (ValueError, OSError):
                continue

    file_list = file_list or []
    for idx, rel in enumerate(file_list):
        if progress_callback:
            progress_callback(idx + 1, total, rel)

        if not ws_adapter.exists(rel):
            continue
        if ws_adapter.is_binary(rel):
            continue

        ws_hash = _hash(ws_adapter, rel)
        bk_exists = bk_adapter.exists(rel)

        if not bk_exists:
            entries.append(
                FileEntry(
                    rel_path=rel,
                    status="new",
                    workspace_hash=ws_hash,
                    selected=True,
                )
            )
        else:
            bk_hash = _hash(bk_adapter, rel)
            if ws_hash == bk_hash:
                entries.append(
                    FileEntry(
                        rel_path=rel,
                        status="same",
                        workspace_hash=ws_hash,
                        backup_hash=bk_hash,
                        selected=False,
                    )
                )
            else:
                entries.append(
                    FileEntry(
                        rel_path=rel,
                        status="modified",
                        workspace_hash=ws_hash,
                        backup_hash=bk_hash,
                        selected=True,
                    )
                )

        ws_by_hash.setdefault(ws_hash, []).append(rel)

    path_to_entry = {e.rel_path: e for e in entries}
    for ws_hash, ws_paths in ws_by_hash.items():
        bk_paths = backup_by_hash.get(ws_hash, [])
        if not bk_paths:
            continue
        for wp in ws_paths:
            entry = path_to_entry.get(wp)
            if not entry or entry.status != "new":
                continue
            for bp in bk_paths:
                if bp != wp:
                    entry.status = "renamed"
                    entry.old_path = bp
                    entry.selected = True
                    bk_paths.remove(bp)
                    break

    return entries


def get_exclude_patterns(
    project: ProjectConfig,
    workspace: Path,
    *,
    file_adapter: FileAdapter | None = None,
) -> list[str]:
    """合并 .gitignore 规则 + force_exclude + 硬编码保护的规则"""
    patterns = _read_gitignore(workspace, file_adapter=file_adapter)
    patterns.extend(project.force_exclude)
    # 硬编码保护：这些目录在任何项目中都不能被 sync
    patterns.extend([
        ".git/", ".claude/", ".codex/", ".codebuddy/", ".cursor/",
        ".gitgo/", "gitgo_config.json", "gitgo_history.json",
    ])
    return patterns


def get_file_diff(
    workspace: Path,
    backup: Path,
    entry: FileEntry,
    *,
    ws_adapter: FileAdapter | None = None,
    bk_adapter: FileAdapter | None = None,
) -> str:
    """返回 workspace vs backup 的统一差异格式文本。"""
    import difflib

    if entry.status == "same":
        return ""

    if ws_adapter is None:
        ws_adapter = LocalFileAdapter(workspace)
    if bk_adapter is None:
        bk_adapter = LocalFileAdapter(backup)

    ws_lines = (
        ws_adapter.read_text(entry.rel_path).splitlines(keepends=True)
        if ws_adapter.exists(entry.rel_path)
        else []
    )
    bk_lines = (
        bk_adapter.read_text(entry.rel_path).splitlines(keepends=True)
        if bk_adapter.exists(entry.rel_path)
        else []
    )

    if entry.status == "new":
        return "".join(f"+{line}" for line in ws_lines)
    elif entry.status == "renamed":
        return "（重命名文件，内容不变）"
    elif entry.status == "modified":
        diff = difflib.unified_diff(
            bk_lines,
            ws_lines,
            fromfile=f"a/{entry.old_path or entry.rel_path}",
            tofile=f"b/{entry.rel_path}",
        )
        return "".join(diff)
    return ""
