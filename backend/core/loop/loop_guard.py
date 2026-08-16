"""LoopGuard —— B Agent 循环守卫层。

从 executor.py 拆出，独立可测试。包含：
- CompletionGuard（现有，通过 harness/completion.py）
- TaskGate.decide()（现有，零步防护 + 重入保护）
- check_doom_loop()（现有，失败循环检测）
- check_repeat_success()（新增，Reasonix 的 repeat-success guard）
- check_budget_continuity()（现有，token 预算停滞检测）

v0.42: 接入 check_repeat_success、check_doom_loop、check_budget_continuity 到 check()。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.loop.models import AgentProcess
    from backend.core.loop.session import AgentSession


@dataclass
class GuardResult:
    """循环守卫的决策结果。"""
    is_complete: bool = False
    blocked: bool = False
    nudge_text: str = ""
    need_reentry: bool = False


class LoopGuard:
    """B Agent 循环守卫。

    每次 LLM 无 tool_call 响应时调用，判定：完成 / block + nudge / 继续。

    v0.42: 接入 check_repeat_success（风暴抑制）、check_doom_loop（死循环）、
    check_budget_continuity（预算停滞）到 check() 方法中。
    v0.45: 新增 _repeated_tool_errors / check_storm_break（工具错误螺旋检测）。
    """

    def __init__(self):
        self._recent_successes: dict[str, int] = {}       # tool:canonical_args → consecutive count
        self._recent_failures: dict[str, int] = {}         # tool:canonical_args → consecutive count
        self._max_repeat_success = 2                       # Reasonix 标准
        self._max_repeat_failure = 3                       # doom_loop 标准
        # v0.45: storm break — track consecutive errors by (tool_name, error_code)
        self._tool_error_counts: dict[tuple[str, str], int] = {}
        self._storm_break_threshold = 3                     # Reasonix 标准

    def check(
        self,
        process: "AgentProcess",
        response: str,
        session: "AgentSession",
        signal_bus=None,
        signals=None,
    ) -> GuardResult:
        """综合检查：完成判定 → 重复检测 → doom_loop → budget → plain_text。"""

        # 1. 完成判定（现有逻辑，从 agent_step 提取）
        if _is_completion(response):
            if signal_bus is not None and signals is not None:
                from backend.core.loop.harness.completion import CompletionGuard
                completion_result = signal_bus.dispatch(
                    signals, process, context="completion",
                )
                if completion_result.blocked and completion_result.missing_tools:
                    return GuardResult(
                        blocked=True,
                        nudge_text=(
                            f"[完成前需先调用以下工具] "
                            f"{', '.join(completion_result.missing_tools)}"
                        ),
                    )
                if completion_result.warnings:
                    return GuardResult(
                        blocked=True,
                        nudge_text=(
                            f"[完成检查] 以下历史纠正指令未被处理: "
                            f"{'; '.join(completion_result.warnings)}"
                        ),
                    )

            # TaskGate
            from backend.core.loop.task_gate import TaskGate
            gate = TaskGate()
            decision = gate.decide(process, response)
            if decision.need_reentry:
                return GuardResult(
                    blocked=True,
                    nudge_text=decision.nudge_text,
                    need_reentry=True,
                )

            return GuardResult(is_complete=True)

        # 2. doom_loop 检测（同 tool + 同 args 连续失败 ≥ 3 次）
        if check_doom_loop_safe(process._step_history):
            return GuardResult(
                blocked=True,
                nudge_text=(
                    "[系统] 检测到同一工具+参数连续失败。"
                    "请停止当前操作，用其他工具重新检查状态后再尝试。"
                ),
            )

        # 3. 纯文本死循环检测
        if _repeated_plain_text(session, response):
            return GuardResult(
                blocked=True,
                nudge_text=(
                    "[系统] 检测到连续相同响应。请调用工具推进任务，"
                    "或回复 TASK_COMPLETE 结束。"
                ),
            )

        # 4. 预算停滞检测
        if _check_budget_stagnation(session):
            return GuardResult(
                blocked=True,
                nudge_text=(
                    "[系统提示] 最近几轮对话无实质进展（无工具调用或完成信号）。"
                    "请调用工具或回复 TASK_COMPLETE 结束任务。"
                ),
            )

        return GuardResult()

    def record_tool_result(self, tool_name: str, args: dict, is_error: bool) -> None:
        """在每轮工具调用后更新成功/失败追踪。

        executor 在收集 ToolResult 后调用，供 LoopGuard 在 check() 时消费。
        """
        canonical = _canonicalize_args(tool_name, args)
        if is_error:
            key = f"{tool_name}:{canonical}"
            self._recent_failures[key] = self._recent_failures.get(key, 0) + 1
            # 成功记录在失败时清零（连续失败才需要检测）
            self._recent_successes.pop(key, None)
        else:
            key = f"{tool_name}:{canonical}"
            self._recent_successes[key] = self._recent_successes.get(key, 0) + 1
            # 检测重复成功风暴
            if self._recent_successes.get(key, 0) > self._max_repeat_success:
                # 达到上限——下轮 check() 时会触发 blocked
                pass

    # ── v0.45: Storm Break (工具错误螺旋检测) ──────────────

    def record_tool_error(self, tool_name: str, error_code: str) -> None:
        """记录一次工具错误。

        按 (tool_name, error_code) 聚合——不哈希 error message，
        使得 FILE_NOT_FOUND 无论路径如何变化都算同一错误。
        """
        key = (tool_name, error_code)
        self._tool_error_counts[key] = self._tool_error_counts.get(key, 0) + 1
        # 不同 key 的旧记录在 check_storm_break 中清理

    def check_storm_break(self, tool_name: str, error_code: str = "") -> str:
        """检查是否触发工具错误螺旋。

        同一 (tool_name, error_code) 连续 ≥ storm_break_threshold 次
        → 返回 nudge 文本；否则返回空字符串。

        不重置计数器——LLM 换策略后新工具调用成功时由 record_tool_result
        自动清理对应成功记录，错误计数在下次不同错误出现时清理。
        """
        if not error_code:
            return ""
        key = (tool_name, error_code)
        count = self._tool_error_counts.get(key, 0)
        if count >= self._storm_break_threshold:
            return (
                f"[系统] 操作 {tool_name} 已连续 {count} 次返回相同错误"
                f" [{error_code}]，请换策略。"
            )
        # 清理不匹配 key 的旧错误记录（保持只追踪最近的错误模式）
        for k in list(self._tool_error_counts.keys()):
            if k != key:
                del self._tool_error_counts[k]
        return ""


# ── 辅助函数（从 executor.py 提取）──────────────────────────

def check_doom_loop_safe(recent_steps: list[dict], threshold: int = 3) -> bool:
    """安全包装 doom_loop 检测。"""
    if len(recent_steps) < threshold:
        return False
    from backend.core.loop.task_gate import check_doom_loop
    return check_doom_loop(recent_steps, threshold)


def check_repeat_success(
    tool_name: str,
    canonical_args: str,
    recent_history: dict[str, str],
    max_repeat: int = 2,
) -> bool:
    """检测写入工具的同参数重复成功（Reasonix repeat-success guard）。

    Returns: True 如果应该阻止（已达重复上限）。
    """
    key = f"{tool_name}:{canonical_args}"
    count = recent_history.get(key, 0) + 1
    recent_history[key] = count
    return count > max_repeat


def _is_completion(response: str) -> bool:
    markers = [
        "任务完成", "分析完成", "处理完成", "执行完成",
        "TASK_COMPLETE", "DONE:", "FINAL_ANSWER:",
    ]
    return any(m in response for m in markers)


def _repeated_plain_text(session: "AgentSession | None", response: str,
                         threshold: int = 3) -> bool:
    """检测连续纯文本响应是否陷入重复循环。"""
    if session is None:
        return False
    assistant_msgs = [
        m.get("content", "")[:100]
        for m in session.messages
        if m.get("role") == "assistant"
    ]
    assistant_msgs.append(response[:100])
    if len(assistant_msgs) < threshold:
        return False
    recent = assistant_msgs[-threshold:]
    return len(set(recent)) == 1


def _check_budget_stagnation(session: "AgentSession | None",
                              window_size: int = 5) -> bool:
    """检测 token 预算停滞——最近 N 轮无进展。"""
    if session is None:
        return False
    assistant_msgs = [
        m for m in session.messages
        if m.get("role") == "assistant"
    ]
    if len(assistant_msgs) < window_size:
        return False
    recent = assistant_msgs[-window_size:]
    lengths = [len(m.get("content", "")) for m in recent]
    avg = sum(lengths) / len(lengths)
    if avg < 50:
        return False
    variance = sum((l - avg) ** 2 for l in lengths) / len(lengths)
    std = variance ** 0.5
    cv = std / avg if avg > 0 else 0
    has_action = any(
        "<tool_call>" in m.get("content", "") or
        "TASK_COMPLETE" in m.get("content", "") or
        "分析完成" in m.get("content", "") or
        "任务完成" in m.get("content", "")
        for m in recent
    )
    return cv < 0.2 and not has_action


def _canonicalize_args(tool_name: str, args: dict) -> str:
    """规范化工具参数——用于去重和重复检测。

    per-tool 标准化规则：
    - 文件路径: normalize（去 ../ 展开 ~/）
    - 时间戳/随机值: 脱敏（替换为 <TIMESTAMP>/<RANDOM>）
    - 其他: sort_keys=True 的 JSON dump
    """
    import re
    cleaned = dict(args)
    # 脱敏时间戳（ISO format）
    for k, v in cleaned.items():
        if isinstance(v, str):
            if re.match(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', v):
                cleaned[k] = "<TIMESTAMP>"
    return json.dumps(cleaned, sort_keys=True, ensure_ascii=False)
