"""SignalNormalizer — 多源治理数据 → 统一 GovernanceSignal 列表。

物尽其用：PolicyEngine / LessonManager / ContractManager / HistoryManager / Facts
所有来源的数据归一化为 GovernanceSignal，按优先级排序后进入 SignalBus。

优先级排序规则:
1. severity: CRITICAL > HIGH > MEDIUM > LOW
2. 同 severity: category BLOCK > WARN > SUGGEST > NOTIFY
3. 同 category: source 稳定排序 (lesson_trigger > contract_drift > rejection > identity > dependency > fact)
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import json
import uuid
from dataclasses import asdict, is_dataclass

from backend.core.loop.signals import (
    GovernanceSignal,
    SignalSeverity,
    SignalCategory,
)

if TYPE_CHECKING:
    from backend.core.knowledge.models import Lesson

_SOURCE_ORDER = {
    "lesson_trigger": 0,
    "contract_drift": 1,
    "rejection": 2,
    "identity_integrity": 3,
    "dependency_chain": 4,
    "fact": 5,
}

_SEVERITY_ORDER = {
    SignalSeverity.CRITICAL: 0,
    SignalSeverity.HIGH: 1,
    SignalSeverity.MEDIUM: 2,
    SignalSeverity.LOW: 3,
}

_CATEGORY_ORDER = {
    SignalCategory.BLOCK: 0,
    SignalCategory.WARN: 1,
    SignalCategory.SUGGEST: 2,
    SignalCategory.NOTIFY: 3,
}


class SignalNormalizer:
    """将 PolicyEngine 原始输出 + Lesson/Contract/Rejection/Fact 数据归一化。"""

    def normalize(
        self,
        policy_results: dict | None = None,
        lessons: list | None = None,
        rejections: list[dict] | None = None,
        facts: list | None = None,
    ) -> list[GovernanceSignal]:
        """归一化所有来源的治理数据。

        Args:
            policy_results: PolicyEngine.run() 原始输出 dict
            lessons: Lesson 对象列表
            rejections: HistoryManager 中的 rejection 条目列表
            facts: Fact 对象列表

        Returns:
            按优先级排序的 GovernanceSignal 列表
        """
        signals: list[GovernanceSignal] = []

        if policy_results:
            signals.extend(self._from_policy_results(policy_results))

        if lessons:
            signals.extend(self._from_lessons(lessons))

        if rejections:
            signals.extend(self._from_rejections(rejections))

        if facts:
            signals.extend(self._from_facts(facts))

        unique = {}
        for signal in signals:
            if signal.source != "rejection":
                identity = signal.to_dict()
                identity.pop("signal_id", None)
                signal.signal_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                    "gitgo:signal:" + json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str)))
            unique[signal.signal_id] = signal
        return self._sort_by_priority(list(unique.values()))

    # ── 来源转换 ──────────────────────────────────────────────

    def _from_policy_results(self, results: dict) -> list[GovernanceSignal]:
        """PolicyEngine.run() 结果 → GovernanceSignal 列表。

        已知 key 使用专用工厂方法，未知 key 通配 fallback 到 from_fact()，
        确保自定义 PolicyCheck 的输出不被静默丢弃。
        """
        signals: list[GovernanceSignal] = []
        KNOWN_KEYS = {"lesson_triggers", "contract_drift",
                      "identity_integrity", "dependency_chain"}

        for key, items in results.items():
            if not items:
                continue
            if key == "lesson_triggers":
                for lt in items:
                    signals.append(GovernanceSignal.from_lesson_trigger(lt))
            elif key == "contract_drift":
                for drift in items:
                    signals.append(GovernanceSignal.from_contract_drift(drift))
            elif key == "identity_integrity":
                for integrity in items:
                    signals.append(GovernanceSignal.from_identity_integrity(integrity))
            elif key == "dependency_chain":
                for dep in items:
                    signals.append(GovernanceSignal.from_dependency_chain(dep))
            else:
                # 自定义 PolicyCheck → 通配注册为 SUGGEST 级别
                for item in items:
                    item_dict = item if isinstance(item, dict) else {"data": str(item)}
                    rule = str(
                        item_dict.get("rule")
                        or item_dict.get("message")
                        or item_dict.get("data")
                        or key
                    )
                    signals.append(GovernanceSignal(
                        source=f"policy:{key}",
                        severity=SignalSeverity.LOW,
                        category=SignalCategory.SUGGEST,
                        rule=rule,
                        check_id=key,
                        metadata={"raw": item_dict},
                    ))

        return signals

    def _from_lessons(self, lessons: list) -> list[GovernanceSignal]:
        """Lesson 对象 → GovernanceSignal 列表（仅当 lesson 有工具约束时）。"""
        signals: list[GovernanceSignal] = []
        for lesson in lessons:
            trust_tier = str(getattr(lesson, "_gitgo_trust_tier", "verified"))
            applicability = dict(getattr(lesson, "_gitgo_applicability", {}) or {})
            dangerous = getattr(lesson, 'dangerous_tools', None) or []
            prerequisite = getattr(lesson, 'prerequisite_tools', None) or []
            required = getattr(lesson, 'required_tools', None) or []
            if not dangerous and not prerequisite and not required:
                continue  # 无工具约束的 lesson 不产生信号

            # Catalog lessons have not matched a registered checker in the
            # current workspace state.  They are retrieval candidates only,
            # regardless of whether a human verified the lesson itself.  Hard
            # tool constraints are emitted solely by LessonTriggerCheck after
            # an authoritative structured check matches.
            signals.append(GovernanceSignal(
                source="lesson_trigger",
                severity=SignalSeverity.MEDIUM,
                category=SignalCategory.SUGGEST,
                target_tools=[],
                prerequisite_tools=[],
                required_tools=[],
                rule=getattr(lesson, 'rule', ''),
                suggestion=f"Lesson '{getattr(lesson, 'id', '')}' is available for retrieval.",
                check_id=getattr(lesson, 'id', ''),
                metadata={
                    "trust_tier": trust_tier,
                    "source": getattr(lesson, 'source', ''),
                    "origin": getattr(lesson, 'origin', ''),
                    "enforcement_authority": False,
                    "applicability": applicability,
                },
            ))
        return signals

    def _from_rejections(self, rejections: list[dict]) -> list[GovernanceSignal]:
        """Rejection 历史 → GovernanceSignal 列表。"""
        signals: list[GovernanceSignal] = []
        for r in rejections:
            if is_dataclass(r):
                r = asdict(r)
            detail = r.get("detail", {})
            if detail.get("resolved") is True or detail.get("state") in {"resolved", "superseded"}:
                continue
            instruction = str(detail.get("instruction", "")).strip()
            if not instruction:
                instruction = str(detail.get("reason", "")).strip()
            if instruction:
                signals.append(GovernanceSignal.from_rejection(r, instruction))

        return signals

    def _from_facts(self, facts: list) -> list[GovernanceSignal]:
        """Fact 对象 → GovernanceSignal 列表。"""
        return [GovernanceSignal.from_fact(f) for f in facts]

    # ── 优先级排序 ────────────────────────────────────────────

    def _sort_by_priority(
        self, signals: list[GovernanceSignal]
    ) -> list[GovernanceSignal]:
        """按 severity → category → source 三级排序。"""
        return sorted(signals, key=lambda s: (
            _SEVERITY_ORDER.get(s.severity, 99),
            _CATEGORY_ORDER.get(s.category, 99),
            _SOURCE_ORDER.get(s.source, 99),
        ))
