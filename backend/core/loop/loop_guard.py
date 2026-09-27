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
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from backend.core.loop.operation_policy import is_effectful_mutation

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
    reason_code: str = ""
    degraded: bool = False


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
        self._last_failure_key = ""
        self._max_repeat_success = 2                       # Reasonix 标准
        self._max_repeat_failure = 3                       # doom_loop 标准
        # v0.45: storm break — track a failed strategy, not merely a tool.
        # Generic tools such as exec_command can legitimately return the same
        # error code for materially different commands.  Folding those calls
        # together makes recovery itself look like a retry storm.
        self._tool_error_counts: dict[tuple[str, str, str], int] = {}
        self._storm_break_threshold = 3                     # Reasonix 标准
        from backend.core.loop.task_gate import TaskGate
        self._task_gate = TaskGate()

    def check(
        self,
        process: "AgentProcess",
        response: str,
        session: "AgentSession",
        signal_bus=None,
        signals=None,
    ) -> GuardResult:
        """综合检查：完成判定 → 重复检测 → doom_loop → budget → plain_text。"""

        # 1. Completion. Answer turns are conversational: a complete non-empty
        # provider response is itself the terminal claim. Action/supervisor
        # turns retain their explicit evidence-bearing completion protocols.
        answer_complete = (
            getattr(process, "task_kind", "") == "answer"
            and bool(response.strip())
        )
        if _is_completion(response) or answer_complete:
            if (
                getattr(process, "task_kind", "") == "action"
                and getattr(process, "completion_claim", None) is None
            ):
                from backend.core.loop.completion_protocol import CompletionClaim
                process.completion_claim = CompletionClaim.from_response(
                    response, step=process.steps_used,
                )
            if signal_bus is not None and signals is not None:
                from backend.core.loop.harness.completion import CompletionGuard
                completion_result = signal_bus.dispatch(
                    signals, process, context="completion",
                )
                if completion_result.blocked and completion_result.missing_tools:
                    return GuardResult(
                        blocked=True,
                        reason_code="required_tools",
                        nudge_text=(
                            f"[完成前需先调用以下工具] "
                            f"{', '.join(completion_result.missing_tools)}"
                        ),
                    )
                if completion_result.warnings:
                    return GuardResult(
                        blocked=True,
                        reason_code="governance_resolution",
                        nudge_text=(
                            f"[完成检查] 以下历史纠正指令未被处理: "
                            f"{'; '.join(completion_result.warnings)}"
                        ),
                    )

            from backend.core.loop.completion_protocol import HostCompletionEvaluator
            host_result = HostCompletionEvaluator.evaluate(process, response, signals)
            if not host_result.allowed:
                gate_status = HostCompletionEvaluator.outstanding_gates(process, signals)
                exception = dict(
                    getattr(process, "runtime_preferences", {}).get(
                        "completion_exception", {}
                    ) or {}
                )
                accepted = set(exception.get("gate_ids") or [])
                gates = list(gate_status.get("gates") or [])
                current = {str(item.get("gate_id") or "") for item in gates}
                if (
                    gates
                    and all(
                        item.get("degradable_by_explicit_user_decision") is True
                        for item in gates
                    )
                    and current.issubset(accepted)
                ):
                    return GuardResult(
                        is_complete=True,
                        degraded=True,
                        reason_code="user_accepted_completion_exception",
                    )
                process.completion_rejections.append({
                    "step": process.steps_used,
                    "reasons": list(host_result.reasons),
                })
                return GuardResult(
                    blocked=True,
                    reason_code="host_completion",
                    nudge_text="[Host 完成硬门] " + "; ".join(host_result.reasons),
                )

            # TaskGate state must live for the whole execution, not one check.
            decision = self._task_gate.decide(process, response)
            if decision.need_reentry:
                return GuardResult(
                    blocked=True,
                    nudge_text=decision.nudge_text,
                    need_reentry=True,
                    reason_code="task_reentry",
                )

            return GuardResult(is_complete=True)

        # 2. 重复成功写入检测
        if any(count > self._max_repeat_success
               for count in self._recent_successes.values()):
            return GuardResult(
                blocked=True,
                reason_code="repeat_success",
                nudge_text=(
                    "[系统] 检测到相同写操作已重复成功。"
                    "请停止重复写入并检查当前状态。"
                ),
            )

        # 3. doom_loop 检测（同 tool + 同 args 连续失败 ≥ 3 次）。
        # Only Host-observed failed tool results belong here.  ``_step_history``
        # also contains model turns; treating three similar final answers as
        # failed tool calls used to turn already-finished work into a false
        # NUDGE_ESCALATION.
        if any(
            count >= self._max_repeat_failure
            for count in self._recent_failures.values()
        ):
            return GuardResult(
                blocked=True,
                reason_code="doom_loop",
                nudge_text=(
                    "[系统] 检测到同一工具+参数连续失败。"
                    "请停止当前操作，用其他工具重新检查状态后再尝试。"
                ),
            )

        # 3. 纯文本死循环检测
        if _repeated_plain_text(session, response):
            return GuardResult(
                blocked=True,
                reason_code="repeated_plain_text",
                nudge_text=(
                    "[系统] 检测到连续相同响应。请调用工具推进任务，"
                    "或回复 TASK_COMPLETE 结束。"
                ),
            )

        # 4. 预算停滞检测
        if _check_budget_stagnation(session):
            return GuardResult(
                blocked=True,
                reason_code="budget_stagnation",
                nudge_text=(
                    "[系统提示] 最近几轮对话无实质进展（无工具调用或完成信号）。"
                    "请调用工具或回复 TASK_COMPLETE 结束任务。"
                ),
            )

        return GuardResult()

    def record_tool_result(
        self, tool_name: str, args: dict, is_error: bool,
        effect: str = "workspace_write",
    ) -> None:
        """在每轮工具调用后更新成功/失败追踪。

        executor 在收集 ToolResult 后调用，供 LoopGuard 在 check() 时消费。
        """
        canonical = _canonicalize_args(tool_name, args)
        if is_error:
            key = f"{tool_name}:{canonical}"
            if key != self._last_failure_key:
                self._recent_failures.clear()
            self._recent_failures[key] = self._recent_failures.get(key, 0) + 1
            self._last_failure_key = key
            # 成功记录在失败时清零（连续失败才需要检测）
            self._recent_successes.pop(key, None)
        elif is_effectful_mutation(effect):
            key = f"{tool_name}:{canonical}"
            self._recent_successes[key] = self._recent_successes.get(key, 0) + 1
            # 检测重复成功风暴
            if self._recent_successes.get(key, 0) > self._max_repeat_success:
                # 达到上限——下轮 check() 时会触发 blocked
                pass
        if not is_error:
            # Any successful tool result breaks a consecutive-failure run.
            self._recent_failures.clear()
            self._last_failure_key = ""
            if is_effectful_mutation(effect):
                # A committed mutation changes the state against which earlier
                # business failures were observed.  Keeping coarse
                # (tool,error) counters across that boundary can reject a now
                # valid recovery call before execution (for example:
                # FILE_EXISTS -> recoverable delete -> create).  Exact
                # invocation receipts remain in history; only stale preflight
                # storm evidence is cleared.
                self._tool_error_counts.clear()
            else:
                for error_key in list(self._tool_error_counts):
                    if error_key[0] == tool_name:
                        self._tool_error_counts.pop(error_key, None)

    # ── v0.45: Storm Break (工具错误螺旋检测) ──────────────

    def record_tool_error(
        self,
        tool_name: str,
        error_code: str,
        args: dict | None = None,
    ) -> tuple[str, str, str]:
        """记录一次工具错误。

        按 (tool_name, error_code, canonical_args) 聚合。错误消息可能包含
        易变文本，不能作为身份；调用参数则代表模型采用的实际策略，
        必须参与身份，否则不同命令/路径会被错误地合并成一次重试风暴。

        返回稳定 key，供一个 Provider 批次内按相同调用去重。
        """
        key = self.storm_key(tool_name, error_code, args)
        self._tool_error_counts[key] = self._tool_error_counts.get(key, 0) + 1
        # 不同 key 的旧记录在 check_storm_break 中清理
        return key

    def reset_tool_errors(self, tool_names: set[str] | None = None) -> None:
        """Forget obsolete tool errors after a real capability transition.

        A missing-capability failure belongs to the capability surface that
        produced it.  Once the Host grants a lease (or mounts another scoped
        capability), carrying that failure into the new surface can block the
        first valid call as a false storm.  Callers may reset only newly
        available tools; ``None`` is reserved for a complete epoch change.
        """
        if tool_names is None:
            self._tool_error_counts.clear()
            return
        normalized = {str(name) for name in tool_names}
        for key in list(self._tool_error_counts):
            if key[0] in normalized:
                self._tool_error_counts.pop(key, None)

    def check_storm_break(
        self,
        tool_name: str,
        error_code: str = "",
        args: dict | None = None,
    ) -> str:
        """检查是否触发工具错误螺旋。

        同一 (tool_name, error_code, canonical_args) 连续达到阈值
        → 返回 nudge 文本；否则返回空字符串。

        不重置计数器——LLM 换策略后新工具调用成功时由 record_tool_result
        自动清理对应成功记录，错误计数在下次不同错误出现时清理。
        """
        if not error_code:
            return ""
        key = self.storm_key(tool_name, error_code, args)
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

    @staticmethod
    def storm_key(
        tool_name: str,
        error_code: str,
        args: dict | None,
    ) -> tuple[str, str, str]:
        """Return the canonical identity of one failed tool strategy."""
        return (
            str(tool_name),
            str(error_code),
            _canonicalize_args(str(tool_name), dict(args or {})),
        )


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
    """Only protocol-shaped markers may request host completion evaluation."""
    return bool(
        re.search(r"(?m)^\s*TASK_COMPLETE\s*$", response)
        or re.search(r"(?m)^\s*FINAL_ANSWER\s*:", response)
    )


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
        bool((session.provider_state.get(m.get("provider_state_id"), {}) or {}).get("tool_calls"))
        or _is_completion(m.get("content", ""))
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
