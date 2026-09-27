"""配置管理 - Config dataclass + 读写 + 搜索"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
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
    ".env.*",
    "llm_config.json",
    "gitgo_config.json",
    "sync_config.json",
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

DEFAULT_AUTHORSHIP_CONFIG = {
    "mode": "mixed",
    "strip_commit_coauthors": True,
    "strip_code_comments": False,
    "exclude_tool_configs": [
        "CLAUDE.md", ".claude/", ".codex/", ".codebuddy/", ".cursor/", ".windsurf/",
        "llm_config.json", "gitgo_config.json", "sync_config.json", ".env", ".env.*",
    ],
}

DEFAULT_INTEGRITY_CONFIG = {
    "enabled": True,
    "mass_override_threshold": 0.80,
    "identity_files": [
        "CLAUDE.md",
        ".claude/",
        ".codex/",
        ".codebuddy/",
        ".gitignore",
        "gitgo_config.json",
        "sync_config.json",
    ],
}


def default_outbound_policy() -> dict:
    """Canonical project policy for every outbound/publish path.

    The legacy force_exclude/security_scan/authorship fields remain readable for
    old configs, but new runtime code consumes this projection.
    """
    return {
        "enabled": True,
        "exclude_paths": list(DEFAULT_FORCE_EXCLUDE),
        "include_paths": [],
        "severity_threshold": DEFAULT_SECURITY_SCAN["severity_threshold"],
        "ignored_rules": [],
        "extra_patterns": [],
        "content_level": 2,
        "deep_scan": False,
        "approved_fingerprints": [],
        "version": 1,
    }


def normalize_outbound_policy(raw: dict | None, *, legacy: dict | None = None) -> dict:
    base = default_outbound_policy()
    legacy = dict(legacy or {})
    if legacy:
        base.update({
            "enabled": bool(legacy.get("enabled", True)),
            "exclude_paths": list(legacy.get("force_exclude") or base["exclude_paths"]),
            "severity_threshold": str(legacy.get("severity_threshold") or base["severity_threshold"]),
            "ignored_rules": list(legacy.get("ignored_rules") or []),
            "extra_patterns": list(legacy.get("extra_patterns") or []),
            "content_level": int(legacy.get("content_level", 2) or 2),
            "deep_scan": bool(legacy.get("deep_scan", False)),
        })
    if isinstance(raw, dict):
        for key in base:
            if key in raw:
                base[key] = raw[key]
    base["enabled"] = bool(base["enabled"])
    for key in ("exclude_paths", "include_paths", "ignored_rules", "extra_patterns", "approved_fingerprints"):
        value = base.get(key)
        if not isinstance(value, list):
            raise ValueError(f"outbound_policy.{key} must be a list")
        base[key] = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    base["content_level"] = max(1, min(3, int(base.get("content_level", 2) or 2)))
    base["deep_scan"] = bool(base.get("deep_scan", False))
    base["severity_threshold"] = str(base.get("severity_threshold") or "medium").lower()
    if base["severity_threshold"] not in {"low", "medium", "high", "critical"}:
        raise ValueError("outbound_policy.severity_threshold is invalid")
    base["version"] = max(1, int(base.get("version", 1) or 1))
    return base


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
    integrity: dict = field(default_factory=lambda: dict(DEFAULT_INTEGRITY_CONFIG))
    authorship: dict = field(default_factory=lambda: dict(DEFAULT_AUTHORSHIP_CONFIG))
    outbound_policy: dict = field(default_factory=default_outbound_policy)
    archived: bool = False
    pending_hard_delete_at: str = ""  # ISO timestamp, empty = no pending delete

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
        authorship = d.get("authorship", dict(DEFAULT_AUTHORSHIP_CONFIG))
        privacy = dict(authorship.get("privacy") or {}) if isinstance(authorship, dict) else {}
        outbound = normalize_outbound_policy(d.get("outbound_policy"), legacy={
            **(ss if isinstance(ss, dict) else {}),
            "force_exclude": d.get("force_exclude", list(DEFAULT_FORCE_EXCLUDE)),
            "content_level": privacy.get("level", 2),
            "deep_scan": privacy.get("deep_scan", False),
        })
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
            integrity=d.get("integrity", dict(DEFAULT_INTEGRITY_CONFIG)),
            authorship=authorship,
            outbound_policy=outbound,
            archived=d.get("archived", False),
            pending_hard_delete_at=d.get("pending_hard_delete_at", ""),
        )


@dataclass
class Config:
    schema_version: int = 1
    revision: int = 0
    projects: list[ProjectConfig] = field(default_factory=list)
    language: str = "en"  # UI copy only; commands/protocol identifiers stay stable
    verbose: bool = False
    auto_compact: bool = True
    agent_routing: str = "owner"
    theme: str = "system"  # 主题: "light" | "dark" | "system"
    animation: bool = True  # 是否启用动画
    external_editor: str = ""
    launcher: dict = field(default_factory=lambda: {
        "terminal": "auto", "command": "", "args": [],
    })
    web_search_mode: str = "auto"
    web_search_endpoint: str = ""
    web_search_engine: str = "duckduckgo"
    safety: dict = field(default_factory=lambda: {"delete_delay_minutes": 10})

    @classmethod
    def from_dict(cls, d: dict) -> Config:
        # 整体迁移（旧单项目格式 → projects[]）
        d = migrate_config_dict(d)
        if "projects" in d and isinstance(d["projects"], list):
            return cls(
                schema_version=max(1, int(d.get("schema_version", 1) or 1)),
                revision=max(0, int(d.get("revision", 0) or 0)),
                projects=[ProjectConfig.from_dict(p) for p in d["projects"]],
                language=d.get("language", "en"),
                verbose=bool(d.get("verbose", False)),
                auto_compact=bool(d.get("auto_compact", True)),
                agent_routing="fresh" if d.get("agent_routing") == "fresh" else "owner",
                theme=d.get("theme", "system"),
                animation=d.get("animation", True),
                external_editor=str(d.get("external_editor", "") or ""),
                launcher=dict(d.get("launcher") or {
                    "terminal": "auto", "command": "", "args": [],
                }),
                web_search_mode=(str(d.get("web_search_mode", "auto") or "auto").lower()
                                 if str(d.get("web_search_mode", "auto") or "auto").lower()
                                 in {"auto", "provider", "searxng", "disabled"} else "auto"),
                web_search_endpoint=str(d.get("web_search_endpoint", "") or ""),
                web_search_engine=(str(d.get("web_search_engine", "duckduckgo") or "duckduckgo").lower()
                                   if str(d.get("web_search_engine", "duckduckgo") or "duckduckgo").lower()
                                   in {"google", "bing", "baidu", "yandex", "duckduckgo"}
                                   else "duckduckgo"),
                safety=d.get("safety", {"delete_delay_minutes": 10}),
            )
        return cls(
            schema_version=max(1, int(d.get("schema_version", 1) or 1)),
            revision=max(0, int(d.get("revision", 0) or 0)),
            language=d.get("language", "en"),
            verbose=bool(d.get("verbose", False)),
            auto_compact=bool(d.get("auto_compact", True)),
            agent_routing="fresh" if d.get("agent_routing") == "fresh" else "owner",
            theme=d.get("theme", "system"),
            animation=d.get("animation", True),
            external_editor=str(d.get("external_editor", "") or ""),
            launcher=dict(d.get("launcher") or {
                "terminal": "auto", "command": "", "args": [],
            }),
            web_search_mode=(str(d.get("web_search_mode", "auto") or "auto").lower()
                             if str(d.get("web_search_mode", "auto") or "auto").lower()
                             in {"auto", "provider", "searxng", "disabled"} else "auto"),
            web_search_endpoint=str(d.get("web_search_endpoint", "") or ""),
            web_search_engine=(str(d.get("web_search_engine", "duckduckgo") or "duckduckgo").lower()
                               if str(d.get("web_search_engine", "duckduckgo") or "duckduckgo").lower()
                               in {"google", "bing", "baidu", "yandex", "duckduckgo"}
                               else "duckduckgo"),
            safety=d.get("safety", {"delete_delay_minutes": 10}),
        )


class ConfigManager:
    """管理配置的读写和搜索"""

    CONFIG_FILE = "config.json"
    OLD_CONFIG_FILE = "gitgo_config.json"
    LEGACY_CONFIG_FILE = "sync_config.json"
    _lock = threading.RLock()

    @staticmethod
    def default_path() -> Path:
        """Return the user-scoped, launcher-independent configuration path."""
        override = os.getenv("GITGO_CONFIG_PATH", "").strip()
        if override:
            return Path(override).expanduser().resolve()
        return (Path.home() / ".gitgo" / ConfigManager.CONFIG_FILE).resolve()

    @staticmethod
    def _legacy_paths() -> list[Path]:
        # An explicit destination is an isolation boundary for tests,
        # portable profiles and managed deployments.  Looking back into the
        # caller's cwd here could migrate unrelated live configuration into a
        # temporary profile.
        if os.getenv("GITGO_CONFIG_PATH", "").strip():
            return []
        cwd = Path.cwd()
        package_root = Path(__file__).parent.parent.parent
        candidates = [
            cwd / ConfigManager.OLD_CONFIG_FILE,
            cwd / ConfigManager.LEGACY_CONFIG_FILE,
            Path.home() / ".vernier" / ConfigManager.OLD_CONFIG_FILE,
            Path.home() / ".vernier" / ConfigManager.LEGACY_CONFIG_FILE,
            package_root / ConfigManager.OLD_CONFIG_FILE,
        ]
        canonical = ConfigManager.default_path()
        result: list[Path] = []
        for item in candidates:
            resolved = item.expanduser().resolve()
            if resolved != canonical and resolved not in result:
                result.append(resolved)
        return result

    @staticmethod
    def find_config() -> Optional[Path]:
        path = ConfigManager.default_path()
        if path.exists():
            return path
        return next((candidate for candidate in ConfigManager._legacy_paths()
                     if candidate.exists()), None)

    @staticmethod
    def load(path: Optional[Path] = None, *, strict: bool = False) -> Config:
        with ConfigManager._lock:
            p = path or ConfigManager.find_config()
            if not p or not p.exists():
                return Config()
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                cfg = Config.from_dict(data)
                if path is None and p != ConfigManager.default_path():
                    canonical = ConfigManager.save(cfg, ConfigManager.default_path(), allow_empty=True)
                    migration_dir = canonical.parent / "migrations"
                    migration_dir.mkdir(parents=True, exist_ok=True)
                    archived = migration_dir / f"{p.name}.legacy-imported-{uuid.uuid4().hex[:8]}"
                    os.replace(p, archived)
                return cfg
            except (json.JSONDecodeError, OSError):
                if strict:
                    raise
                return Config()

    @staticmethod
    def save(config: Config, path: Optional[Path] = None, *, allow_empty: bool = False) -> Path:
        with ConfigManager._lock:
            p = path or ConfigManager.default_path()
            # Only implicit legacy discovery migrates to the canonical user
            # file.  An explicit path is an API boundary used by export,
            # tests and managed profiles, and must be honoured verbatim even
            # when its basename is historical.
            if path is None and p.name in {
                ConfigManager.OLD_CONFIG_FILE, ConfigManager.LEGACY_CONFIG_FILE,
            }:
                p = ConfigManager.default_path()
            # 保护：如果当前文件有项目但 config 对象是空的，不覆盖
            if not config.projects and p.exists() and not allow_empty:
                try:
                    existing = json.loads(p.read_text(encoding="utf-8"))
                    if existing.get("projects"):
                        return p  # 保护已有项目不被空 config 覆盖
                except (json.JSONDecodeError, OSError):
                    pass
            p.parent.mkdir(parents=True, exist_ok=True)
            config.schema_version = max(1, int(config.schema_version or 1))
            config.revision = max(0, int(config.revision or 0)) + 1
            data = _serialize_config(config)
            temp_path = p.with_name(f".{p.name}.{uuid.uuid4().hex}.tmp")
            try:
                with open(temp_path, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, indent=2, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, p)
            finally:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
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
