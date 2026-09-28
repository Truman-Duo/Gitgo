"""同步与推送 — sync_to_backup / push_to_backup"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

from backend.adapters import FileAdapter, GitRunner, LocalFileAdapter, LocalGitRunner

from .models import FileEntry
from .security import _security_scan
from .utils import _entry_to_dict


# ── 插件钩子 ──────────────────────────────────────────


def _fire_sync_complete(
    plugin_ids: list[str] | None, success: bool, commit_hash: str, files_count: int
) -> None:
    if plugin_ids:
        from plugin_loader import get_orchestrator
        try:
            get_orchestrator().on_sync_complete(
                plugin_ids, {"success": success, "commit_hash": commit_hash, "files_count": files_count}
            )
        except Exception:
            pass


def _fire_push_complete(
    plugin_ids: list[str] | None, success: bool, remote: str = "origin"
) -> None:
    if plugin_ids:
        from plugin_loader import get_orchestrator
        try:
            get_orchestrator().on_push_complete(
                plugin_ids, {"success": success, "remote": remote}
            )
        except Exception:
            pass


# ── 同步 ──────────────────────────────────────────────


def sync_to_backup(
    entries: list[FileEntry],
    commit_message: str,
    workspace: str | Path = "",
    backup: str | Path = "",
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    plugin_ids: Optional[list[str]] = None,
    privacy_config: Optional[dict] = None,
    *,
    ws_adapter: FileAdapter | None = None,
    bk_adapter: FileAdapter | None = None,
    git_runner: GitRunner | None = None,
) -> bool:
    """执行同步：拷贝文件 → git add → git commit"""
    if ws_adapter is None:
        ws_adapter = LocalFileAdapter(Path(workspace).resolve())
    if bk_adapter is None:
        bk_adapter = LocalFileAdapter(Path(backup).resolve())
    if git_runner is None:
        git_runner = LocalGitRunner(Path(backup).resolve())
    selected = [e for e in entries if e.selected]

    if not selected:
        if progress_callback:
            progress_callback(0, 0, "没有选中任何文件")
        return False

    from backend.core.authorship import (
        collect_private_fingerprints,
        scan_files_privacy,
        should_exclude_outbound_file,
    )

    fingerprints = collect_private_fingerprints(
        workspace, file_adapter=ws_adapter,
    )
    exclude_patterns = (privacy_config or {}).get("exclude_tool_configs")
    publishable = []
    excluded_count = 0
    for entry in selected:
        if not ws_adapter.exists(entry.rel_path):
            continue
        try:
            data = ws_adapter.read_bytes(entry.rel_path)
        except OSError:
            if progress_callback:
                progress_callback(0, len(selected), "隐私检查无法读取待发布文件")
            return False
        if should_exclude_outbound_file(
            entry.rel_path, data, known_fingerprints=fingerprints,
            exclude_patterns=exclude_patterns,
        ):
            excluded_count += 1
            continue
        publishable.append(entry)

    privacy = (privacy_config or {}).get("privacy", {})
    alerts = scan_files_privacy(
        str(workspace), [entry.rel_path for entry in publishable],
        level=max(2, int(privacy.get("level", 2) or 2)),
        deep_scan=bool(privacy.get("deep_scan", False)),
        approved_fingerprints=list(privacy.get("approved_fingerprints") or []),
        file_adapter=ws_adapter,
    )
    if any(alert.get("level") == "error" for alert in alerts):
        if progress_callback:
            progress_callback(0, len(selected), f"隐私硬门阻止提交（{len(alerts)} 项）")
        return False

    try:
        purged_count = _purge_private_tracked_files(
            bk_adapter, git_runner, fingerprints,
            exclude_patterns=exclude_patterns,
        )
    except (OSError, RuntimeError):
        if progress_callback:
            progress_callback(0, len(selected), "无法验证发布仓库的私有文件清单")
        return False

    selected = publishable
    total = len(selected) + purged_count
    if not selected and not purged_count:
        if progress_callback:
            progress_callback(0, 0, "没有可发布文件")
        return False
    if excluded_count and progress_callback:
        progress_callback(0, total, f"隐私层已静默排除 {excluded_count} 个私有对象")

    if plugin_ids:
        from plugin_loader import get_orchestrator

        orch = get_orchestrator()
        block_msg = orch.on_sync_start(
            plugin_ids,
            [_entry_to_dict(e) for e in selected],
            commit_message,
        )
        if block_msg:
            if progress_callback:
                progress_callback(0, total, f"插件阻止同步: {block_msg}")
            return False

    for i, entry in enumerate(selected):
        if progress_callback:
            progress_callback(i, total, f"拷贝 {entry.rel_path}")

        if not ws_adapter.exists(entry.rel_path):
            continue

        parent = str(Path(entry.rel_path).parent)
        if parent:
            bk_adapter.mkdir(parent, parents=True, exist_ok=True)
        try:
            data = ws_adapter.read_bytes(entry.rel_path)
            bk_adapter.write_bytes(entry.rel_path, data)
        except OSError as e:
            if progress_callback:
                progress_callback(i, total, f"拷贝失败 {entry.rel_path}: {e}")
            return False

    if progress_callback:
        progress_callback(total, total, "正在提交到备份仓库...")

    try:
        if not git_runner.add_all(timeout=120):
            if progress_callback:
                progress_callback(total, total, "git add 失败")
            _fire_sync_complete(plugin_ids, False, "", len(selected))
            return False

        commit_ok, commit_stderr = git_runner.commit(commit_message, timeout=30)
        if not commit_ok:
            if "nothing to commit" in commit_stderr:
                if progress_callback:
                    progress_callback(total, total, "没有变更需要提交")
                _fire_sync_complete(plugin_ids, True, "", len(selected))
                return True
            if progress_callback:
                progress_callback(total, total, f"git commit 失败: {commit_stderr}")
            _fire_sync_complete(plugin_ids, False, "", len(selected))
            return False

        if progress_callback:
            progress_callback(total, total, "[OK] 提交成功")

        _ch = git_runner.rev_parse("HEAD") or ""

        _fire_sync_complete(plugin_ids, True, _ch, len(selected))
        return True

    except (subprocess.TimeoutExpired, OSError) as e:
        if progress_callback:
            progress_callback(total, total, f"Git 操作失败: {e}")
        _fire_sync_complete(plugin_ids, False, "", len(selected))
        return False


def _purge_private_tracked_files(
    backup_adapter: FileAdapter,
    git_runner: GitRunner,
    known_fingerprints: set[str],
    *,
    exclude_patterns: list[str] | None = None,
) -> int:
    """Remove private artifacts already tracked in the release worktree.

    This is intentionally independent from .gitignore: ignored/tracked files
    remain tracked, so the privacy boundary must inspect the Git index itself.
    """
    from backend.core.authorship import should_exclude_outbound_file

    result = git_runner.run(["ls-files", "-z"], timeout=30)
    if result.returncode != 0:
        raise RuntimeError("cannot enumerate tracked release files")
    tracked = [path for path in result.stdout.split("\0") if path]
    private_paths: list[str] = []
    for rel_path in tracked:
        try:
            if not backup_adapter.exists(rel_path):
                continue
            data = backup_adapter.read_bytes(rel_path)
        except OSError as exc:
            raise RuntimeError("cannot inspect tracked release file") from exc
        if should_exclude_outbound_file(
            rel_path, data, known_fingerprints=known_fingerprints,
            exclude_patterns=exclude_patterns,
        ):
            private_paths.append(rel_path)

    for index in range(0, len(private_paths), 50):
        batch = private_paths[index:index + 50]
        removed = git_runner.run(
            ["rm", "-f", "--ignore-unmatch", "--", *batch], timeout=60,
        )
        if removed.returncode != 0:
            raise RuntimeError("cannot purge private release artifacts")
    return len(private_paths)


# ── 推送 ──────────────────────────────────────────────


def push_to_backup(
    backup: str | Path = "",
    remote: str = "origin",
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    skip_scan: bool = False,
    security_config: Optional[dict] = None,
    plugin_ids: Optional[list[str]] = None,
    *,
    git_runner: GitRunner | None = None,
) -> tuple[bool, list[dict]]:
    """执行 git push 到远程仓库。

    返回 (success, warnings)：
    - success: push 是否成功（被安全检查阻断也算 False）
    - warnings: 安全检查发现的敏感信息列表（空列表表示无警告）
    """
    if git_runner is None:
        git_runner = LocalGitRunner(Path(backup).resolve())
    if not git_runner.is_git_repo():
        if progress_callback:
            progress_callback(0, 1, "备份目录不是 git 仓库")
        return False, []

    if progress_callback:
        progress_callback(0, 1, f"正在 push 到 {remote}...")

    warnings = _security_scan(str(backup), security_config, git_runner=git_runner)
    hard_warnings = [
        warning for warning in warnings
        if warning.get("severity") in {"high", "critical"}
    ]
    if hard_warnings or (warnings and not skip_scan):
        if progress_callback:
            progress_callback(0, 1, f"安全检查发现 {len(warnings)} 项敏感信息")
        return False, warnings

    if plugin_ids:
        from plugin_loader import get_orchestrator

        orch = get_orchestrator()
        block_msg = orch.on_push_start(plugin_ids)
        if block_msg:
            if progress_callback:
                progress_callback(0, 1, f"插件阻止 push: {block_msg}")
            _fire_push_complete(plugin_ids, False, remote)
            return False, []

    try:
        result = git_runner.run(["push", remote], timeout=60)
        if result.returncode == 0:
            if progress_callback:
                progress_callback(1, 1, f"[OK] Push 成功:\n{result.stdout.strip()}")
            _fire_push_complete(plugin_ids, True, remote)
            return True, []
        else:
            if progress_callback:
                progress_callback(0, 1, f"Push 失败: {result.stderr.strip()}")
            _fire_push_complete(plugin_ids, False, remote)
            return False, []
    except OSError as e:
        if progress_callback:
            progress_callback(0, 1, f"Push 出错: {e}")
        _fire_push_complete(plugin_ids, False, remote)
        return False, []
