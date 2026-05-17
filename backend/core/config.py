"""配置管理 - Config dataclass + 读写 + 搜索"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional

from backend.models import FileAccessKind, RepoNode, SyncStatus
from backend.core.migrate import migrate_config_dict, needs_migration

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
    "plugins": [],
    "template_name": "default",
}

DEFAULT_SECURITY_SCAN = {
    "enabled": True,
    "severity_threshold": "medium",
    "ignored_rules": [],
    "extra_patterns": [],
}


@dataclass
class ProjectConfig:
    name: str = ""
    note: str = ""
    workspace: RepoNode = field(default_factory=RepoNode)
    release: RepoNode = field(default_factory=RepoNode)
    trial: Optional[RepoNode] = None
    commit_format: dict = field(default_factory=lambda: dict(DEFAULT_COMMIT_FORMAT))
    force_exclude: list = field(default_factory=lambda: list(DEFAULT_FORCE_EXCLUDE))
    security_scan: dict = field(default_factory=lambda: dict(DEFAULT_SECURITY_SCAN))

    # ── 向后兼容 property ─────────────────────────────────

    @property
    def workspace_path(self) -> str:
        return self.workspace.file_access.path

    @workspace_path.setter
    def workspace_path(self, value: str) -> None:
        self.workspace.file_access.path = value

    @property
    def backup_path(self) -> str:
        return self.release.file_access.path

    @backup_path.setter
    def backup_path(self, value: str) -> None:
        self.release.file_access.path = value

    @property
    def sync_base(self) -> str:
        return self.workspace.last_known_head

    @sync_base.setter
    def sync_base(self, value: str) -> None:
        self.workspace.last_known_head = value

    @property
    def trial_path(self) -> str:
        return self.trial.file_access.path if self.trial else ""

    @trial_path.setter
    def trial_path(self, value: str) -> None:
        if not self.trial:
            self.trial = RepoNode()
        self.trial.file_access.path = value

    @property
    def project_name(self) -> str:
        return self.name

    @property
    def sync_status(self) -> SyncStatus:
        if not self.release.file_access.path:
            return SyncStatus.MISSING
        if self.release.file_access.kind == FileAccessKind.SSH:
            fa = self.release.file_access
            if not fa.host or not fa.path:
                return SyncStatus.EMPTY
            return SyncStatus.VALID
        bp = Path(self.release.file_access.path)
        if not bp.exists() or not (bp / ".git").exists():
            return SyncStatus.EMPTY
        return SyncStatus.VALID

    @classmethod
    def from_dict(cls, d: dict) -> ProjectConfig:
        # 旧格式自动迁移
        if needs_migration(d):
            from backend.core.migrate import migrate_project_dict
            d = migrate_project_dict(d)

        cf = d.get("commit_format", {})
        ss = d.get("security_scan", {})
        return cls(
            name=d.get("name", "Unnamed"),
            workspace=RepoNode.from_dict(d.get("workspace")) or RepoNode(),
            release=RepoNode.from_dict(d.get("release")) or RepoNode(),
            trial=RepoNode.from_dict(d.get("trial")),
            commit_format={
                "prefix": cf.get("prefix", "ANBM"),
                "number_start": cf.get("number_start", 0),
                "padding": cf.get("padding", False),
                "plugins": cf.get("plugins", []),
                "template_name": cf.get("template_name", "default"),
            },
            force_exclude=d.get("force_exclude", list(DEFAULT_FORCE_EXCLUDE)),
            security_scan=ss if ss else dict(DEFAULT_SECURITY_SCAN),
        )


@dataclass
class Config:
    projects: list[ProjectConfig] = field(default_factory=list)
    language: str = "zh"  # 界面语言代码
    theme: str = "system"  # 主题: "light" | "dark" | "system"
    animation: bool = True  # 是否启用动画

    @classmethod
    def from_dict(cls, d: dict) -> Config:
        # 整体迁移（旧单项目格式 → projects[]）
        d = migrate_config_dict(d)
        if "projects" in d and isinstance(d["projects"], list):
            return cls(
                projects=[ProjectConfig.from_dict(p) for p in d["projects"]],
                language=d.get("language", "zh"),
                theme=d.get("theme", "system"),
                animation=d.get("animation", True),
            )
        return cls(language=d.get("language", "zh"), theme=d.get("theme", "system"), animation=d.get("animation", True))


class ConfigManager:
    """管理配置的读写和搜索"""

    CONFIG_FILE = "gitgo_config.json"
    LEGACY_CONFIG_FILE = "sync_config.json"

    @staticmethod
    def default_path() -> Path:
        """优先 exe/脚本同目录，其次用户目录"""
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).parent
        else:
            base = Path.cwd()
        # 优先新文件名
        candidate = base / ConfigManager.CONFIG_FILE
        if candidate.exists():
            return candidate
        # 兼容旧文件名
        legacy = base / ConfigManager.LEGACY_CONFIG_FILE
        if legacy.exists():
            return legacy
        user_cfg = Path.home() / ".vernier" / ConfigManager.CONFIG_FILE
        if user_cfg.exists():
            return user_cfg
        user_legacy = Path.home() / ".vernier" / ConfigManager.LEGACY_CONFIG_FILE
        if user_legacy.exists():
            return user_legacy
        # 都不存在 → 默认写新文件名到 exe/脚本同目录
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
        # 如果加载的是旧文件名，迁移到新文件名
        if p.name == ConfigManager.LEGACY_CONFIG_FILE:
            p = p.with_name(ConfigManager.CONFIG_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = _serialize_config(config)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return p

    @staticmethod
    def get_backup_git_dir(project: ProjectConfig) -> Optional[Path]:
        """验证备份目录是 git 仓库，返回 .git 路径"""
        if not project.release.file_access.path:
            return None
        bp = Path(project.release.file_access.path)
        git_dir = bp / ".git"
        return git_dir if git_dir.exists() else None


# ── 序列化辅助 ──────────────────────────────────────────────


def _enum_to_str(obj):
    """递归将 Enum 转换为 value。"""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: _enum_to_str(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_enum_to_str(v) for v in obj]
    return obj


def _serialize_config(config: Config) -> dict:
    """将 Config 序列化为纯 dict（Enum 转 string）。"""
    return _enum_to_str(asdict(config))
