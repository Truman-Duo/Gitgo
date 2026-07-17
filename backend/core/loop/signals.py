"""GovernanceSignal — PolicyEngine 与 Loop Harness 之间的统一信号格式。

物尽其用：PolicyEngine / LessonManager / ContractManager / HistoryManager / Facts
所有治理数据归一化为 GovernanceSignal，通过 SignalBus 路由到 HarnessPlugin。

语义约定:
- severity: 信号严重级别（CRITICAL 硬阻断 > HIGH 软阻断 > MEDIUM 建议 > LOW 通知）
- category: 信号消费方式（BLOCK 阻止 / WARN 警告 / SUGGEST 建议 / NOTIFY 通知）
- target_tools / target_files: 受影响的工具和文件（空 = 全局）
- prerequisite_tools: 执行目标工具前必须先执行的前置工具
- required_tools: 任务完成前必须执行的必要工具
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum


class SignalSeverity(Enum):
    CRITICAL = "critical"  # 硬阻断 — 不可覆写
    HIGH = "high"          # 软阻断 — 可被人覆写
    MEDIUM = "medium"      # 建议 — 不阻断但强烈建议
    LOW = "low"            # 通知 — 仅信息


class SignalCategory(Enum):
    BLOCK = "block"        # 阻止操作
    WARN = "warn"          # 警告但允许继续
    SUGGEST = "suggest"    # 建议执行某操作
    NOTIFY = "notify"      # 仅通知


@dataclass
class GovernanceSignal:
    """PolicyEngine 输出归一化后的统一信号。

    各来源 (lesson_trigger, contract_drift, identity_integrity, dependency_chain,
    rejection, fact) 的原始数据统一转换为此格式后进入 SignalBus。
    """

    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source: str = ""  # "lesson_trigger"|"contract_drift"|"identity_integrity"|"dependency_chain"|"rejection"|"fact"
    severity: SignalSeverity = SignalSeverity.MEDIUM
    category: SignalCategory = SignalCategory.WARN

    # 影响范围
    target_tools: list[str] = field(default_factory=list)
    target_files: list[str] = field(default_factory=list)

    # 工具依赖
    prerequisite_tools: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)

    # 人类可读
    rule: str = ""
    suggestion: str = ""

    # 来源追踪
    check_id: str = ""  # 产生此信号的检查 ID（lesson_id / check_name）
    metadata: dict = field(default_factory=dict)

    # ── 工厂方法 ──────────────────────────────────────────────

    @classmethod
    def from_lesson_trigger(cls, lesson_data: dict) -> "GovernanceSignal":
        """从 LessonTriggerCheck 输出创建信号。"""
        severity_map = {
            "critical": SignalSeverity.CRITICAL,
            "high": SignalSeverity.HIGH,
            "medium": SignalSeverity.MEDIUM,
            "low": SignalSeverity.LOW,
        }
        return cls(
            source="lesson_trigger",
            severity=severity_map.get(
                lesson_data.get("severity", "medium"), SignalSeverity.MEDIUM
            ),
            category=SignalCategory.BLOCK,
            target_tools=lesson_data.get("dangerous_tools", []),
            target_files=[lesson_data.get("file", "")] if lesson_data.get("file") else [],
            prerequisite_tools=lesson_data.get("prerequisite_tools", []),
            required_tools=lesson_data.get("required_tools", []),
            rule=lesson_data.get("rule", ""),
            suggestion=f"请先执行: {', '.join(lesson_data.get('prerequisite_tools', []))}"
                if lesson_data.get("prerequisite_tools") else "",
            check_id=lesson_data.get("lesson_id", ""),
        )

    @classmethod
    def from_contract_drift(cls, drift_data: dict) -> "GovernanceSignal":
        """从 ContractDriftCheck 输出创建信号。"""
        return cls(
            source="contract_drift",
            severity=SignalSeverity.HIGH,
            category=SignalCategory.WARN,
            target_files=[drift_data.get("file", "")] if drift_data.get("file") else [],
            target_tools=drift_data.get("target_tools", []),
            rule=drift_data.get("rule", "contract drift"),
            suggestion=drift_data.get("suggestion", "请先 scan 验证文件签名"),
            check_id="contract_drift_check",
            metadata={"message": drift_data.get("message", "")},
        )

    @classmethod
    def from_identity_integrity(cls, integrity_data: dict) -> "GovernanceSignal":
        """从 IdentityIntegrityCheck 输出创建信号。"""
        severity = SignalSeverity.HIGH if integrity_data.get("level") == "error" else SignalSeverity.MEDIUM
        return cls(
            source="identity_integrity",
            severity=severity,
            category=SignalCategory.WARN,
            rule=integrity_data.get("message", integrity_data.get("rule", "identity integrity")),
            check_id="identity_integrity_check",
        )

    @classmethod
    def from_dependency_chain(cls, dep_data: dict) -> "GovernanceSignal":
        """从 DependencyChainCheck 输出创建信号。"""
        return cls(
            source="dependency_chain",
            severity=SignalSeverity.MEDIUM,
            category=SignalCategory.SUGGEST,
            target_files=dep_data.get("affected_files", []),
            rule=dep_data.get("message", "dependency chain affected"),
            suggestion="请检查受影响文件的导入兼容性",
            check_id="dependency_chain_check",
        )

    @classmethod
    def from_rejection(cls, rejection_data: dict, instruction: str) -> "GovernanceSignal":
        """从 rejection 历史创建信号。"""
        return cls(
            source="rejection",
            severity=SignalSeverity.HIGH,
            category=SignalCategory.WARN,
            rule=instruction,
            suggestion=f"历史纠正指令: {instruction[:100]}",
            check_id=rejection_data.get("correlation_id", ""),
        )

    @classmethod
    def from_fact(cls, fact) -> "GovernanceSignal":
        """从 Fact 输出创建信号。"""
        from backend.core.fact.file_patterns import Fact
        severity_map = {
            "critical": SignalSeverity.CRITICAL,
            "high": SignalSeverity.HIGH,
            "medium": SignalSeverity.MEDIUM,
            "low": SignalSeverity.LOW,
        }
        return cls(
            source="fact",
            severity=severity_map.get(fact.severity, SignalSeverity.MEDIUM),
            category=SignalCategory.NOTIFY,
            rule=fact.summary,
            check_id=fact.fact_id,
            metadata={"fact_type": fact.fact_type},
        )


@dataclass
class HarnessResult:
    """HarnessPlugin 执行后的决策结果。"""

    allowed: bool = True          # 操作是否允许
    blocked: bool = False         # 是否被阻断
    warnings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    missing_tools: list[str] = field(default_factory=list)
    nudge_text: str = ""          # 注入到 AgentSession 的提示文本

    @classmethod
    def allow(cls) -> "HarnessResult":
        return cls(allowed=True)

    @classmethod
    def block(cls, reason: str) -> "HarnessResult":
        return cls(allowed=False, blocked=True, warnings=[reason])

    @classmethod
    def warn(cls, message: str) -> "HarnessResult":
        return cls(warnings=[message])

    @classmethod
    def suggest(cls, message: str) -> "HarnessResult":
        return cls(suggestions=[message])
