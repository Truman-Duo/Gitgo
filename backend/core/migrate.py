"""配置迁移 — 旧扁平格式 → RepoNode 格式。

提供自动迁移能力，在加载配置时透明转换。
也作为独立 CLI 工具：``python -m gitgo.migrate [--preview]``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def needs_migration(d: dict) -> bool:
    """判断项目 dict 是否仍为旧扁平格式。"""
    return "workspace_path" in d or "backup_path" in d


def migrate_project_dict(d: dict) -> dict:
    """将单个项目从旧格式转换为 RepoNode 格式。

    幂等：若已是新格式，返回副本。
    """
    # 已是新格式
    if not needs_migration(d):
        return dict(d)

    result = dict(d)

    # 提取旧字段
    ws_path = result.pop("workspace_path", result.pop("workspace", ""))
    bk_path = result.pop("backup_path", "")
    sync_base = result.pop("sync_base", "")

    # 保留兼容键名
    name = result.get("name", result.get("project_name", "Unnamed"))
    if "project_name" in result:
        result.pop("project_name")
    result["name"] = name

    # 构造 RepoNode
    result["workspace"] = {
        "file_access": {"kind": "local", "path": ws_path},
        "last_known_head": sync_base,
    }
    result["release"] = {
        "file_access": {"kind": "local", "path": bk_path},
    }

    return result


def migrate_config_dict(d: dict) -> dict:
    """转换整个配置 dict（单项目或多项目均支持）。"""
    result = dict(d)

    if "projects" in result and isinstance(result["projects"], list):
        result["projects"] = [migrate_project_dict(p) for p in result["projects"]]
    elif needs_migration(result):
        # 单项目旧格式 → 转为多项目列表
        result = {"projects": [migrate_project_dict(result)]}
        # 保留顶层字段（如 language）
        if "language" in d:
            result["language"] = d["language"]

    return result


def _preview(config_path: Path) -> None:
    """预览迁移效果，不修改文件。"""
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    migrated = migrate_config_dict(raw)
    import json as _json
    print("=== 原始配置 ===")
    _json.dump(raw, sys.stdout, indent=2, ensure_ascii=False)
    print("\n\n=== 迁移后 ===")
    _json.dump(migrated, sys.stdout, indent=2, ensure_ascii=False)
    print()


def main() -> None:
    """CLI 入口。"""
    import argparse
    parser = argparse.ArgumentParser(description="gitgo 配置迁移工具")
    parser.add_argument("--preview", action="store_true",
                        help="预览迁移效果，不修改文件")
    args, _ = parser.parse_known_args()

    from backend.core.config import ConfigManager
    path = ConfigManager.default_path()
    if not path.exists():
        print(f"未找到配置文件: {path}")
        sys.exit(1)

    if args.preview:
        _preview(path)
        return

    # 执行迁移
    raw = json.loads(path.read_text(encoding="utf-8"))
    migrated = migrate_config_dict(raw)
    path.write_text(
        json.dumps(migrated, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"已迁移: {path}")


if __name__ == "__main__":
    main()
