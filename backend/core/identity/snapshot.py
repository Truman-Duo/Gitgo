"""Memory Snapshot — sync 时自动快照工具记忆到 backup

Layer 2: .claude/ .codex/ .codebuddy/ 文件级快照 + 增量拷贝 + 5 次保留上限
"""

from __future__ import annotations

import filecmp
import shutil
from datetime import datetime
from pathlib import Path

MEMORY_SOURCES = [".claude", ".codex", ".codebuddy"]
_MAX_SNAPSHOTS = 5


def snapshot_tool_memories(
    workspace_path: str | Path,
    backup_path: str | Path,
    project,  # ProjectConfig
) -> dict:
    """将 workspace 的工具记忆目录快照到 backup 的 .gitgo/memories/。

    首次全量 copytree，之后用 filecmp 只拷贝变化的文件。
    保留最近 5 次快照，旧快照自动清理。
    """
    ws = Path(workspace_path)
    dest_root = Path(backup_path) / ".gitgo" / "memories"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    snapped = []
    for src_name in MEMORY_SOURCES:
        src = ws / src_name
        if not src.exists():
            continue

        dest = dest_root / f"{src_name}_{ts}"

        if src.is_dir():
            _copy_dir_incremental(src, dest)
        else:
            shutil.copy2(src, dest)
        snapped.append(src_name)

    # 清理旧快照（按源分组的最近 N 个）
    _prune_old_snapshots(dest_root)

    return {"snapped": snapped, "timestamp": ts, "dest": str(dest_root)}


def _copy_dir_incremental(src: Path, dest: Path) -> None:
    """增量拷贝目录：只拷贝源比目标新或不存在的文件。"""
    if not dest.exists():
        shutil.copytree(src, dest)
        return

    # 遍历源，只拷贝变更
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dest / rel
        if item.is_dir():
            target.mkdir(exist_ok=True)
        else:
            if not target.exists() or not filecmp.cmp(str(item), str(target), shallow=True):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)


def _prune_old_snapshots(dest_root: Path) -> None:
    """每种记忆源保留最近 _MAX_SNAPSHOTS 个快照。"""
    for src_name in MEMORY_SOURCES:
        matches = sorted(
            [p for p in dest_root.iterdir()
             if p.name.startswith(f"{src_name}_")],
            key=lambda p: p.name,  # 时间戳可字符串排序
            reverse=True,
        )
        for old in matches[_MAX_SNAPSHOTS:]:
            if old.is_dir():
                shutil.rmtree(old)
            else:
                old.unlink()


def restore_tool_memories(
    backup_path: str | Path,
    workspace_path: str | Path,
    snapshot_timestamp: str | None = None,
) -> dict:
    """从 .gitgo/memories/ 恢复工具记忆到 workspace。

    snapshot_timestamp=None 时使用最新快照。
    """
    src_root = Path(backup_path) / ".gitgo" / "memories"
    ws = Path(workspace_path)

    if not src_root.exists():
        return {"restored": [], "error": "no_snapshots"}

    # 收集快照版本
    snapshots: dict[str, list[tuple[str, Path]]] = {}
    for item in src_root.iterdir():
        name = item.name
        for src_name in MEMORY_SOURCES:
            if name.startswith(f"{src_name}_"):
                ts_val = name[len(src_name) + 1:]
                snapshots.setdefault(src_name, []).append((ts_val, item))

    if not snapshots:
        return {"restored": []}

    restored = []
    for src_name, versions in snapshots.items():
        versions.sort(key=lambda x: x[0], reverse=True)
        if snapshot_timestamp:
            match = next((v for t, v in versions if t == snapshot_timestamp), None)
        else:
            match = versions[0][1] if versions else None

        if match is None:
            continue

        dest = ws / src_name
        if match.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(match, dest)
        else:
            shutil.copy2(match, dest)
        restored.append(src_name)

    return {"restored": restored}


def list_memory_snapshots(backup_path: str | Path) -> list[dict]:
    """列出所有可用快照。"""
    src_root = Path(backup_path) / ".gitgo" / "memories"
    if not src_root.exists():
        return []

    result = []
    for src_name in MEMORY_SOURCES:
        for item in sorted(src_root.iterdir(), reverse=True):
            if item.name.startswith(f"{src_name}_"):
                ts_val = item.name[len(src_name) + 1:]
                result.append({
                    "source": src_name,
                    "timestamp": ts_val,
                    "path": str(item),
                    "is_dir": item.is_dir(),
                })
    return result
