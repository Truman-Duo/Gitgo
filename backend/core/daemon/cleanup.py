"""Resource cleanup on shutdown.

Extracted from daemon/__init__.py (pure structural refactor).
"""

from __future__ import annotations

from pathlib import Path


def _cleanup_resources(workspace_path: str) -> None:
    """清理 daemon 关闭时的临时资源。

    - 清理旧快照备份（.gitgo/snapshots/）
    - 清理已完成会话的持久化文件（.gitgo/sessions/ 中无对应运行进程的）
    - 不删除正在运行的进程的会话文件
    """
    ws = Path(workspace_path)

    # 清理过期快照（保留最近 MAX_SNAPSHOTS 个）
    snap_dir = ws / ".gitgo" / "snapshots"
    if snap_dir.exists():
        try:
            backups = sorted(snap_dir.glob("*@v*"),
                           key=lambda p: p.stat().st_mtime)
            from backend.core.loop.tool_execution import MAX_SNAPSHOTS
            if len(backups) > MAX_SNAPSHOTS:
                for old in backups[:len(backups) - MAX_SNAPSHOTS]:
                    try:
                        old.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    # 清理临时文件（.gitgo/tmp/）
    tmp_dir = ws / ".gitgo" / "tmp"
    if tmp_dir.exists():
        try:
            import shutil
            shutil.rmtree(str(tmp_dir), ignore_errors=True)
        except OSError:
            pass
