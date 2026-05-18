"""Identity Guard — 项目环境完整性保护

Layer 1: Integrity Detection — scan 时检测全量覆盖 / 身份文件删除 / 目录骨架崩塌
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.core.operations.models import FileEntry


# ── 默认身份文件列表 ────────────────────────────────────
# 项目可通过 integrity.identity_files 追加

_DEFAULT_IDENTITY_FILES = [
    "CLAUDE.md",
    ".claude/",
    ".codex/",
    ".codebuddy/",
    ".gitignore",
    "gitgo_config.json",
    "sync_config.json",
]


def _run_integrity_checks(
    entries: list[FileEntry],
    workspace_path: str | Path,
    project,  # ProjectConfig
) -> list[dict]:
    """运行全部完整性检测，返回警告列表。"""
    warnings = []
    ws = Path(workspace_path)

    # Rule 1: 全量覆盖检测
    result = _detect_mass_override(entries, project)
    if result:
        warnings.append(result)

    # Rule 2: 身份文件删除检测
    result = _detect_identity_file_deletion(ws, project)
    if result:
        warnings.append(result)

    # Rule 3: 目录结构突变
    result = _detect_structure_collapse(entries, ws)
    if result:
        warnings.append(result)

    return warnings


def _detect_mass_override(
    entries: list[FileEntry],
    project,  # ProjectConfig
) -> dict | None:
    """检测全量覆盖：entries 中变更占比超过阈值。

    entries 来自 compare_files()，已排除 force_exclude 文件。
    正常开发: 5-30%  覆盖事故: 80%+
    """
    if not entries:
        return None
    cfg = getattr(project, "integrity", {}) or {}
    threshold = cfg.get("mass_override_threshold", 0.80)
    changed = sum(1 for e in entries if e.status in ("new", "modified"))
    ratio = changed / len(entries)

    if ratio >= threshold:
        return {
            "rule": "mass_override",
            "level": "warning",
            "message": (
                f"Mass override detected: {changed}/{len(entries)} files "
                f"changed ({ratio:.0%}). "
                f"This may indicate the project directory was replaced "
                f"by another project."
            ),
            "changed_ratio": ratio,
            "changed_count": changed,
            "total_count": len(entries),
        }
    return None


def _detect_identity_file_deletion(
    workspace_path: Path,
    project,  # ProjectConfig
) -> dict | None:
    """检测身份性文件是否被删除。"""
    cfg = getattr(project, "integrity", {}) or {}
    identity_files = cfg.get("identity_files", _DEFAULT_IDENTITY_FILES)

    missing = []
    for rel_path in identity_files:
        full = workspace_path / rel_path.strip("/")
        if not full.exists():
            missing.append(rel_path)

    if missing:
        return {
            "rule": "identity_file_deleted",
            "level": "alert",
            "message": (
                f"Identity files missing: {', '.join(missing)}. "
                f"Project identity may be compromised."
            ),
            "missing_files": missing,
        }
    return None


def _detect_structure_collapse(
    entries: list[FileEntry],
    workspace_path: Path,
) -> dict | None:
    """检测目录骨架突变：顶级目录 Jaccard 相似度 < 0.3。

    基线由 _save_directory_skeleton() 在 sync 成功后写入 .gitgo/。
    """
    skeleton_path = workspace_path / ".gitgo" / "directory_skeleton.json"
    if not skeleton_path.exists():
        return None  # 首次运行，无基线

    try:
        old_data = json.loads(skeleton_path.read_text(encoding="utf-8"))
        old_dirs = set(old_data.get("dirs", []))
    except (json.JSONDecodeError, OSError):
        return None

    # 当前顶级目录（从 entries 中提取）
    current_dirs = set()
    for entry in entries:
        parts = entry.rel_path.replace("\\", "/").split("/")
        if len(parts) >= 1 and parts[0]:
            current_dirs.add(parts[0])

    if not old_dirs:
        return None

    union = old_dirs | current_dirs
    intersection = old_dirs & current_dirs
    jaccard = len(intersection) / max(len(union), 1)

    if jaccard < 0.3:
        return {
            "rule": "structure_collapse",
            "level": "warning",
            "message": (
                f"Directory structure collapsed: Jaccard {jaccard:.2f} "
                f"(before: {sorted(old_dirs)}, now: {sorted(current_dirs)}). "
                f"Project may have been replaced."
            ),
            "jaccard": jaccard,
            "old_dirs": sorted(old_dirs),
            "new_dirs": sorted(current_dirs),
        }
    return None


def _save_directory_skeleton(workspace_path: Path) -> None:
    """将当前顶级目录骨架写入 .gitgo/directory_skeleton.json 作为基线。"""
    skeleton_path = workspace_path / ".gitgo" / "directory_skeleton.json"
    skeleton_path.parent.mkdir(parents=True, exist_ok=True)

    dirs = []
    files = []
    try:
        for entry in sorted(workspace_path.iterdir()):
            name = entry.name
            # 跳过隐藏文件（保留 .claude .codex 等身份目录）
            if name.startswith(".") and name not in (
                ".git", ".claude", ".codex", ".codebuddy", ".github",
            ):
                continue
            if entry.is_dir():
                dirs.append(name)
            else:
                files.append(name)
    except PermissionError:
        pass

    skeleton_path.write_text(
        json.dumps({"dirs": dirs, "files": files}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
