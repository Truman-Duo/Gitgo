"""RetentionAdvisor — 上下文裁剪时保留高优先级治理信息（原 context_window._retention_priority）。

消费所有来源的信号，计算消息在上下文裁剪时的保留优先级。
优先级规则:
- rejection 纠正指令匹配 → 1.0（最高，不可裁剪）
- lesson_trigger 文件匹配 → 0.8
- contract_drift 文件匹配 → 0.7
- critical_features 匹配 → 0.6
- 默认 → 0.3
"""

from __future__ import annotations

from backend.core.loop.signals import GovernanceSignal, HarnessResult
from backend.core.loop.signal_bus import HarnessPlugin


class RetentionAdvisor(HarnessPlugin):
    """上下文裁剪顾问：基于治理信号计算消息保留优先级。"""

    name = "retention_advisor"
    description = "裁剪时保留 rejection 指令、lesson 文件、contract drift 等高优先级信息"

    subscribed_sources = []  # 全部来源
    subscribed_severities = []  # 全部级别

    def on_signals(
        self,
        signals: list[GovernanceSignal],
        process,  # AgentProcess | None
    ) -> HarnessResult:
        """分析信号批次，返回裁剪优先级建议。

        HarnessResult.suggestions 中包含优先级规则说明。
        """
        result = HarnessResult()

        rejection_count = sum(1 for s in signals if s.source == "rejection")
        lesson_count = sum(1 for s in signals if s.source == "lesson_trigger")
        drift_count = sum(1 for s in signals if s.source == "contract_drift")

        if rejection_count:
            result.suggestions.append(
                f"保留 {rejection_count} 条 rejection 纠正指令（优先级 1.0）"
            )
        if lesson_count:
            result.suggestions.append(
                f"保留 {lesson_count} 条 lesson 相关文件引用（优先级 0.8）"
            )
        if drift_count:
            result.suggestions.append(
                f"保留 {drift_count} 条 contract drift 文件引用（优先级 0.7）"
            )

        return result

    def retention_priority(
        self,
        content: str | dict,
        signals: list[GovernanceSignal] | None = None,
    ) -> float:
        """计算消息的保留优先级（0-1）。委托给纯函数版。"""
        return retention_priority_from_signals(content, signals or [])


def retention_priority_from_signals(
    content: str | dict,
    signals: list[GovernanceSignal],
) -> float:
    """纯函数版：从信号列表计算消息保留优先级。

    方便在无 RetentionAdvisor 实例时直接调用（如 context_window.py 的 _retention_priority）。
    """
    message = content if isinstance(content, dict) else {"content": str(content or "")}
    if not signals or not message.get("content"):
        return 0.3

    score = 0.3
    signal_ids = {
        str(item) for item in (message.get("governance_signal_ids", []) or [])
    }
    referenced_files = {
        str(item) for item in (message.get("referenced_files", []) or [])
    }
    semantic_tags = {
        str(item) for item in (message.get("semantic_tags", []) or [])
    }

    for sig in signals:
        if sig.source == "rejection" and sig.signal_id in signal_ids:
            return 1.0

        if sig.source == "lesson_trigger":
            for fname in sig.target_files:
                if fname and fname in referenced_files:
                    score = max(score, 0.8)

        if sig.source == "contract_drift":
            for fname in sig.target_files:
                if fname and fname in referenced_files:
                    score = max(score, 0.7)

        if sig.metadata.get("critical_feature"):
            if sig.metadata["critical_feature"] in semantic_tags:
                score = max(score, 0.6)

    return score
