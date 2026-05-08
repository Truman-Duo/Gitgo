"""配置管理 - Config dataclass + 读写 + 搜索"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

DEFAULT_FORCE_EXCLUDE = [
    "CLAUDE.md",
    ".claude/",
    "ANBM *",
    "scripts/commit.sh",
    "commit-config.json",
    ".git/",
    "__pycache__/",
    "*.pyc",
    ".venv/",
    ".env",
    ".pytest_cache/",
]

DEFAULT_COMMIT_FORMAT = {
    "prefix": "ANBM",
    "number_start": 0,
    "padding": False,
}


@dataclass
class ProjectConfig:
    name: str = ""
    workspace_path: str = ""
    backup_path: str = ""
    commit_format: dict = field(default_factory=lambda: dict(DEFAULT_COMMIT_FORMAT))
    force_exclude: list = field(default_factory=lambda: list(DEFAULT_FORCE_EXCLUDE))
    sync_base: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> ProjectConfig:
        cf = d.get("commit_format", {})
        return cls(
            name=d.get("name", d.get("project_name", "Unnamed")),
            workspace_path=d.get("workspace_path", d.get("workspace", "")),
            backup_path=d.get("backup_path", ""),
            commit_format={
                "prefix": cf.get("prefix", "ANBM"),
                "number_start": cf.get("number_start", 0),
                "padding": cf.get("padding", False),
            },
            force_exclude=d.get("force_exclude", list(DEFAULT_FORCE_EXCLUDE)),
            sync_base=d.get("sync_base", ""),
        )


@dataclass
class Config:
    projects: list[ProjectConfig] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> Config:
        # 新格式：projects 数组
        if "projects" in d and isinstance(d["projects"], list):
            return cls(projects=[ProjectConfig.from_dict(p) for p in d["projects"]])
        # 旧格式（单项目，顶层字段）→ 自动迁移
        if d.get("backup_path"):
            return cls(projects=[ProjectConfig.from_dict(d)])
        return cls()


class ConfigManager:
    """管理配置的读写和搜索"""

    @staticmethod
    def default_path() -> Path:
        """优先 exe/脚本同目录，其次用户目录"""
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).parent
        else:
            base = Path.cwd()
        candidate = base / "sync_config.json"
        if candidate.exists():
            return candidate
        user_cfg = Path.home() / ".vernier" / "sync_config.json"
        if user_cfg.exists():
            return user_cfg
        # 都不存在 → 默认写在 exe/脚本同目录
        return candidate

    @staticmethod
    def find_config() -> Optional[Path]:
        path = ConfigManager.default_path()
        return path if path.exists() else None

    @staticmethod
    def load(path: Optional[Path] = None) -> Config:
        p = path or ConfigManager.find_config()
        if not p or not p.exists():
            return Config()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            cfg = Config.from_dict(data)
            return cfg
        except (json.JSONDecodeError, OSError):
            return Config()

    @staticmethod
    def save(config: Config, path: Optional[Path] = None) -> Path:
        p = path or ConfigManager.default_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(config)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return p

    @staticmethod
    def get_backup_git_dir(project: ProjectConfig) -> Optional[Path]:
        """验证备份目录是 git 仓库，返回 .git 路径"""
        if not project.backup_path:
            return None
        bp = Path(project.backup_path)
        git_dir = bp / ".git"
        return git_dir if git_dir.exists() else None
