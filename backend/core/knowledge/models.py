from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


@dataclass
class Lesson:
    """一条知识教训。抽象层不含项目名/文件路径/人名。"""
    id: str = ""
    tech_stack: str = ""
    category: str = ""  # api_migration | architecture | dependency | process
    severity: str = "medium"  # low | medium | high | critical

    trigger: str = ""  # 什么情况下触发（LLM 判据）
    rule: str = ""     # 正确做法（LLM 判据）

    # 解析历史（默认截断，节省 token）
    resolution_history: dict | None = None

    # 检查规则（自动执行）
    check: dict | None = None

    # 元数据
    source: str = "manual"  # manual | auto_harvested
    abstract: bool = False  # True=抽象层, False=实例层
    project_name: str = ""
    verified_at: str = ""
    created_at: str = ""
    verified_count: int = 0
    verified_in: list[str] | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None and v != "" and v != [] and v != 0}

    @classmethod
    def from_dict(cls, d: dict) -> Lesson:
        return cls(
            id=d.get("id", ""),
            tech_stack=d.get("tech_stack", ""),
            category=d.get("category", ""),
            severity=d.get("severity", "medium"),
            trigger=d.get("trigger", ""),
            rule=d.get("rule", ""),
            resolution_history=d.get("resolution_history"),
            check=d.get("check"),
            source=d.get("source", "manual"),
            abstract=d.get("abstract", False),
            project_name=d.get("project_name", ""),
            verified_at=d.get("verified_at", ""),
            created_at=d.get("created_at", ""),
            verified_count=d.get("verified_count", 0),
            verified_in=d.get("verified_in"),
        )

