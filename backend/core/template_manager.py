"""Commit template manager — 多套 commit message 模板的持久化管理"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path

from backend.core.config import ConfigManager


# ── 内置默认模板 ──────────────────────────────────────────
# 与 build_commit_template() 当前硬编码输出逐字一致

_DEFAULT_HEADER = "[{prefix}-{number}] {type_str}{scope_str}: {subject}"
_DEFAULT_BODY = (
    "Project: {project_name}\n"
    "\n"
    "Synced from {commit_count} workspace commit(s):\n"
    "{commit_list}\n"
    "\n"
    "---\n"
    "\n"
    "# 请编辑正式 commit message（以上为模板，删除此说明行）\n"
)


# ── 数据模型 ─────────────────────────────────────────────

@dataclass
class CommitTemplate:
    """命名 commit message 模板"""
    name: str = "default"
    description: str = ""
    header_format: str = _DEFAULT_HEADER
    body_format: str = _DEFAULT_BODY
    prefix_override: str | None = None  # 覆盖项目 commit_format.prefix


_BUILTIN_DEFAULT = CommitTemplate(
    name="default",
    description="gitgo 默认格式",
    header_format=_DEFAULT_HEADER,
    body_format=_DEFAULT_BODY,
    prefix_override=None,
)


# ── 管理器 ───────────────────────────────────────────────

class TemplateManager:
    """管理 commit message 模板的持久化。

    模板存储于用户级 Gitgo 配置目录，不写入当前项目。
    """

    TEMPLATE_FILE = "commit-config.json"

    @staticmethod
    def _default_path() -> Path:
        return ConfigManager.default_path().parent / TemplateManager.TEMPLATE_FILE

    @staticmethod
    def _legacy_paths() -> list[Path]:
        # An explicit config root is an isolation boundary (tests, portable
        # profiles, managed deployments).  Never reach back into the caller's
        # working directory in that mode.
        if os.getenv("GITGO_CONFIG_PATH", "").strip():
            return []
        candidates = [
            Path.cwd() / TemplateManager.TEMPLATE_FILE,
            Path(sys.executable).parent / TemplateManager.TEMPLATE_FILE,
            Path.home() / ".vernier" / TemplateManager.TEMPLATE_FILE,
        ]
        canonical = TemplateManager._default_path().resolve()
        result: list[Path] = []
        for candidate in candidates:
            resolved = candidate.expanduser().resolve()
            if resolved != canonical and resolved not in result:
                result.append(resolved)
        return result

    @staticmethod
    def _read_path() -> Path:
        canonical = TemplateManager._default_path()
        if canonical.exists():
            return canonical
        return next((path for path in TemplateManager._legacy_paths() if path.exists()), canonical)

    @staticmethod
    def load() -> list[CommitTemplate]:
        path = TemplateManager._read_path()
        if not path.exists():
            return [_BUILTIN_DEFAULT]

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            templates = []
            for item in data.get("templates", []):
                templates.append(CommitTemplate(
                    name=item.get("name", ""),
                    description=item.get("description", ""),
                    header_format=item.get("header_format", _DEFAULT_HEADER),
                    body_format=item.get("body_format", _DEFAULT_BODY),
                    prefix_override=item.get("prefix_override"),
                ))
            loaded = templates if templates else [_BUILTIN_DEFAULT]
            canonical = TemplateManager._default_path()
            if path.resolve() != canonical.resolve():
                TemplateManager.save(loaded)
                migration_dir = canonical.parent / "migrations"
                migration_dir.mkdir(parents=True, exist_ok=True)
                archived = migration_dir / (
                    f"{path.name}.legacy-imported-{uuid.uuid4().hex[:8]}"
                )
                os.replace(path, archived)
            return loaded
        except (json.JSONDecodeError, OSError):
            return [_BUILTIN_DEFAULT]

    @staticmethod
    def save(templates: list[CommitTemplate]) -> Path:
        path = TemplateManager._default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "templates": [
                {
                    "name": t.name,
                    "description": t.description,
                    "header_format": t.header_format,
                    "body_format": t.body_format,
                    "prefix_override": t.prefix_override,
                }
                for t in templates
            ]
        }
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    @staticmethod
    def get_template(name: str) -> CommitTemplate | None:
        templates = TemplateManager.load()
        for t in templates:
            if t.name == name:
                return t
        return None

    @staticmethod
    def get_default() -> CommitTemplate:
        templates = TemplateManager.load()
        return templates[0] if templates else _BUILTIN_DEFAULT
