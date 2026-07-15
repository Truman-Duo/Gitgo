"""Lesson 数据模型 —— 三层知识结构 (pending → instance → abstract)。

v0.35: 新增有效性追踪、检索追踪、origin 标记、内容哈希去重。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class Lesson:
    """一条知识教训。抽象层不含项目名/文件路径/人名。"""

    id: str = ""
    tech_stack: str = ""
    category: str = ""  # api_migration | architecture | dependency | process
    severity: str = "medium"  # low | medium | high | critical

    # ── 收割产生的字段 ──
    trigger: str = ""  # 触发条件（子字符串匹配文件路径）
    rule: str = ""     # 可行动的约束（testable proposition）
    resolution_history: list[dict] = field(default_factory=list)

    # ── 工具约束（Harness 层消费）──
    dangerous_tools: list[str] = field(default_factory=list)
    prerequisite_tools: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    check: dict | None = None  # {"pattern": "正则"}

    # ── 元数据 ──
    source: str = "manual"  # manual | auto_harvested
    origin: str = ""        # v0.35: "manual" | "auto_verify" | "harvest"
                             # auto_verify 可被人一键 revert 到 pending
    abstract: bool = False  # True=抽象层, False=实例层
    project_name: str = ""
    verified_at: str = ""
    created_at: str = ""
    verified_count: int = 0
    verified_in: list[str] = field(default_factory=list)

    # ── 有效性追踪（回收 + 联想消费）──
    trigger_count: int = 0            # 总触发次数
    applied_count: int = 0            # Agent 遵循了的次数
    violated_after_count: int = 0    # 已有 lesson 但仍违反

    # ── 检索追踪（热/温/冷分层，滑动窗口）──
    recent_retrievals: list[str] = field(default_factory=list)
    # 近 N 次检索的 ISO timestamp，最多保留 10 条

    # ── 收割重试追踪 ──
    harvest_retry_count: int = 0     # LLM 总结此信号失败次数，≥5 自动 discard

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items()
                if v is not None and v != "" and v != [] and v != 0}

    @classmethod
    def from_dict(cls, d: dict) -> "Lesson":
        return cls(
            id=d.get("id", ""),
            tech_stack=d.get("tech_stack", ""),
            category=d.get("category", ""),
            severity=d.get("severity", "medium"),
            trigger=d.get("trigger", ""),
            rule=d.get("rule", ""),
            resolution_history=d.get("resolution_history", []),
            dangerous_tools=d.get("dangerous_tools", []),
            prerequisite_tools=d.get("prerequisite_tools", []),
            required_tools=d.get("required_tools", []),
            check=d.get("check"),
            source=d.get("source", "manual"),
            origin=d.get("origin", ""),
            abstract=d.get("abstract", False),
            project_name=d.get("project_name", ""),
            verified_at=d.get("verified_at", ""),
            created_at=d.get("created_at", ""),
            verified_count=d.get("verified_count", 0),
            verified_in=d.get("verified_in", []),
            trigger_count=d.get("trigger_count", 0),
            applied_count=d.get("applied_count", 0),
            violated_after_count=d.get("violated_after_count", 0),
            recent_retrievals=d.get("recent_retrievals", []),
            harvest_retry_count=d.get("harvest_retry_count", 0),
        )


def lesson_content_hash(trigger: str, rule: str) -> str:
    """SHA-256 前 16 字符，用于精确去重。

    只比较 trigger + rule（不比较其他字段，允许相似模式重复存在）。
    """
    content = f"{trigger}|{rule}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ── v0.35 Phase 3: 热/温/冷分类 + 回收 ──────────────────

RECENT_ROUND_WINDOW = 5
HOT_THRESHOLD = 3
STICKY_CAP = 10
MAX_RETRIEVAL_LOG = 10


def classify_lesson_heat(lesson: Lesson) -> str:
    """基于近期检索频率分类。滑动窗口，非累计计数。

    返回 "hot" | "warm" | "cold"。
    """
    recent = (lesson.recent_retrievals or [])[-MAX_RETRIEVAL_LOG:]
    if len(recent) >= HOT_THRESHOLD:
        return "hot"
    elif len(recent) >= 1:
        return "warm"
    else:
        return "cold"


def get_sticky_lessons(lessons: list[Lesson]) -> list[str]:
    """热 lesson 中取 top-K sticky（只返回 lesson_id）。

    按 severity + 最近活跃度排序，最多 STICKY_CAP 条。
    """
    hot = [l for l in lessons if classify_lesson_heat(l) == "hot"]
    hot.sort(key=lambda l: (
        -severity_rank(l.severity),
        -len(l.recent_retrievals or []),
    ))
    return [l.id for l in hot[:STICKY_CAP]]


def severity_rank(severity: str) -> int:
    _map = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    return _map.get(severity, 1)
