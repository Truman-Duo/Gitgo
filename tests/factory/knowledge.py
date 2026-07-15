"""Knowledge 子系统测试数据生成器。"""

from __future__ import annotations

from datetime import datetime

from backend.core.knowledge.models import Lesson
from tests.factory import pools


class KnowledgeGenerator:
    def __init__(self, factory):
        self.f = factory

    def lesson(self, **overrides) -> Lesson:
        """生成一条随机 lesson。"""
        rng = self.f.rng
        trigger = overrides.pop("trigger", None) or self.f._pick(pools.FILE_PATHS)
        rule_template = self.f._pick(pools.LESSON_RULES)
        rule = overrides.pop("rule", None) or rule_template.format(
            file=trigger,
            tool=self.f._pick(pools.TOOL_NAMES),
            other=self.f._pick(pools.FILE_PATHS),
            category=self.f._pick(pools.CATEGORIES),
            action=self.f._pick(pools.ACTIONS),
            action2=self.f._pick(pools.ACTIONS),
        )

        return Lesson(
            id=overrides.pop("id", None) or self.f._next_id("L"),
            trigger=trigger,
            rule=rule,
            severity=overrides.pop("severity", None)
                or self.f._pick(pools.SEVERITIES, pools.SEVERITY_WEIGHTS),
            category=overrides.pop("category", None)
                or self.f._pick(["process", "dependency", "architecture", "api_migration"]),
            tech_stack=overrides.pop("tech_stack", None)
                or self.f._pick(["python", "typescript", ""]),
            project_name=overrides.pop("project_name", "testproject"),
            verified_count=overrides.pop("verified_count", self.f._int(0, 15)),
            verified_in=overrides.pop("verified_in", None)
                or [self.f._pick(["testproject", "lexi", "shard"])]
                if self.f._bool(0.6) else [],
            verified_at=overrides.pop("verified_at", None)
                or self.f._ts(self.f._int(1, 1440)) if self.f._bool(0.5) else "",
            source=overrides.pop("source", "manual"),
            origin=overrides.pop("origin", None)
                or self.f._pick(["manual", "harvest", "auto_verify", ""]),
            trigger_count=overrides.pop("trigger_count", self.f._int(0, 20)),
            applied_count=overrides.pop("applied_count", self.f._int(0, 10)),
            violated_after_count=overrides.pop("violated_after_count", self.f._int(0, 5)),
            recent_retrievals=overrides.pop("recent_retrievals", None)
                or self._random_retrievals(),
            dangerous_tools=overrides.pop("dangerous_tools", None)
                or self.f._pick_n(pools.TOOL_NAMES, self.f._int(0, 2)),
            prerequisite_tools=overrides.pop("prerequisite_tools", None)
                or self.f._pick_n(pools.TOOL_NAMES, self.f._int(0, 2)),
            required_tools=overrides.pop("required_tools", None)
                or self.f._pick_n(pools.TOOL_NAMES, self.f._int(0, 2)),
            harvest_retry_count=overrides.pop("harvest_retry_count", self.f._int(0, 3)),
            **overrides,
        )

    def lessons(self, n: int = 5, **overrides) -> list[Lesson]:
        """生成 N 条随机 lesson。"""
        return [self.lesson(**overrides) for _ in range(n)]

    def signal(self, signal_type: str | None = None) -> dict:
        """生成一条随机未处理信号。"""
        trigger = self.f._pick(pools.FILE_PATHS)
        rule_template = self.f._pick(pools.LESSON_RULES)
        rule = rule_template.format(
            file=trigger,
            tool=self.f._pick(pools.TOOL_NAMES),
            other=self.f._pick(pools.FILE_PATHS),
            category=self.f._pick(pools.CATEGORIES),
            action=self.f._pick(pools.ACTIONS),
            action2=self.f._pick(pools.ACTIONS),
        )
        return {
            "signal_type": signal_type or self.f._pick(pools.SIGNAL_TYPES),
            "trigger": trigger,
            "rule": rule,
            "detail": {
                "file": trigger,
                "severity": self.f._pick(pools.SEVERITIES),
                "count": self.f._int(1, 10),
            },
            "harvest_retry_count": 0,
        }

    def signals(self, n: int = 5, signal_type: str | None = None) -> list[dict]:
        """生成 N 条随机信号。"""
        return [self.signal(signal_type) for _ in range(n)]

    def mock_llm_response(self, lesson_count: int = 2) -> list[dict]:
        """生成 Mock LLM 返回的 lesson JSON。"""
        lessons = self.lessons(lesson_count)
        result = []
        for l in lessons:
            result.append({
                "trigger": l.trigger,
                "rule": l.rule,
                "severity": l.severity,
                "category": l.category,
                "dangerous_tools": l.dangerous_tools,
                "prerequisite_tools": l.prerequisite_tools,
                "required_tools": l.required_tools,
            })
        return result

    def query_and_lessons(self, n_lessons: int = 5) -> tuple[str, list[Lesson]]:
        """生成一个查询词 + 包含匹配该词的 lesson 列表。

        确保至少 1 条 lesson 的 trigger 包含该查询词。
        """
        query = self.f._pick(pools.SEARCH_QUERIES)
        lessons = []
        # 第一条强制匹配
        matched = self.lesson(trigger=f"src/{query}_module.py")
        lessons.append(matched)
        # 其余随机
        lessons += self.lessons(n_lessons - 1)
        return query, lessons

    # ── 内部 ──────────────────────────────────────────────

    def _random_retrievals(self) -> list[str]:
        count = self.f._int(0, 12)
        if count == 0:
            return []
        return [self.f._ts(self.f._int(1, 1440)) for _ in range(count)]
