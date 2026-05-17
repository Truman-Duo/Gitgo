"""Commit template manager — 多套 commit message 模板的持久化管理"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path


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

    模板存储于 commit-config.json，与 gitgo_config.json 同目录。
    """

    TEMPLATE_FILE = "commit-config.json"

    @staticmethod
    def _default_path() -> Path:
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).parent
        else:
            base = Path.cwd()

        candidate = base / TemplateManager.TEMPLATE_FILE
        if candidate.exists():
            return candidate

        user_path = Path.home() / ".vernier" / TemplateManager.TEMPLATE_FILE
        if user_path.exists():
            return user_path

        return candidate

    @staticmethod
    def load() -> list[CommitTemplate]:
        path = TemplateManager._default_path()
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
            return templates if templates else [_BUILTIN_DEFAULT]
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
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
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
