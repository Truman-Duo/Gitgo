"""ToolPipeline —— 五步工具执行管道。

替代 ToolDispatcher：prepare → validate → before → execute → after。

使用 EventBus 发射事件——不直接知道 SignalBus。任何订阅者（治理、转录、遥测）
都可以独立监听，不入侵管道代码。

保留 RingGate 权限检查 + HistoryManager 审计日志。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from backend.core.loop.gate import RingGate

if TYPE_CHECKING:
    from backend.core.loop.agent_tool import AgentTool
    from backend.core.loop.event_bus import EventBus
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.models import AgentProcess


@dataclass
class ToolResult:
    """工具执行结果。包含 Dashboard 可直接消费的全量字段。"""
    id: str = ""
    tool_name: str = ""
    execution_id: str = ""
    call_index: int = 0
    allowed: bool = True
    is_error: bool = False
    data: dict | None = None
    formatted: str = ""
    error: str = ""
    duration_ms: float = 0.0
    truncated: bool = False
    persist_path: str | None = None
    artifacts: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)


class ToolPipeline:
    """五步管道：prepare_args → validate_schema → before → execute → after。

    与 ToolDispatcher 的关系：ToolPipeline 是 ToolDispatcher 的升级版。
    保留 RingGate.check() 和 HistoryManager 审计，新增 validate + before/after 钩子。
    """

    def __init__(self, gate: RingGate | None = None):
        self._gate = gate or RingGate()

    def execute(
        self,
        tool_call: dict,
        tool: "AgentTool",
        ctx: "ExecutionContext",
        execution_id: str,
        call_index: int = 0,
    ) -> ToolResult:
        """执行单次工具调用。"""
        tool_name = tool_call.get("name", tool.name)
        raw_args = tool_call.get("args", {})

        start = time.time()

        # Step 1: prepare_args
        ctx.event_bus.emit(
            __import__("backend.core.loop.event_bus", fromlist=["ToolEvent"]).ToolEvent(
                "ToolPrepareStarted", execution_id, tool_name, call_index,
            )
        )
        try:
            args = tool.prepare_args(raw_args) if tool.prepare_args else raw_args
        except Exception as exc:
            return self._error_result(
                tool_name, execution_id, call_index, start,
                f"prepare_args failed: {exc}",
            )

        # Step 2: validate_schema
        ctx.event_bus.emit(
            __import__("backend.core.loop.event_bus", fromlist=["ToolEvent"]).ToolEvent(
                "ToolValidateStarted", execution_id, tool_name, call_index,
            )
        )
        try:
            _validate_args(args, tool.parameters)
        except ValueError as exc:
            return self._error_result(
                tool_name, execution_id, call_index, start, str(exc),
            )

        # Step 3: before_hooks → RingGate
        ctx.event_bus.emit(
            __import__("backend.core.loop.event_bus", fromlist=["ToolEvent"]).ToolEvent(
                "ToolBeforeHooksStarted", execution_id, tool_name, call_index,
                data={"args": args},
            )
        )
        gate_result = self._gate.check(ctx.process, tool_name)
        if not gate_result.allowed:
            return self._error_result(
                tool_name, execution_id, call_index, start,
                gate_result.error or "blocked by gate",
            )

        # Step 4: execute
        ctx.event_bus.emit(
            __import__("backend.core.loop.event_bus", fromlist=["ToolEvent"]).ToolEvent(
                "ToolExecuteStarted", execution_id, tool_name, call_index,
            )
        )
        try:
            # v0.45: isolated=True → 子进程隔离执行
            if getattr(tool, "isolated", False):
                result_data = self._execute_isolated(tool_name, args, tool.timeout)
            else:
                result_data = tool.execute(args)
        except Exception as exc:
            # v0.45: classify error for CRASH vs BUSINESS distinction
            from backend.core.loop.error_taxonomy import classify_tool_error, ErrorNature
            classified = classify_tool_error(exc, tool_name=tool_name)
            label = classified.format_for_llm()
            return self._error_result(
                tool_name, execution_id, call_index, start,
                f"{label} | {exc}",
                diagnostics={
                    "nature": classified.nature.value,
                    "code": classified.code,
                    "source": classified.source.value,
                },
            )

        # Step 5: after_hooks + format_result
        duration = (time.time() - start) * 1000

        # 截断
        formatted, truncated, persist_path = _maybe_truncate(
            tool_name, result_data, ctx.workspace_path,
        )

        ctx.event_bus.emit(
            __import__("backend.core.loop.event_bus", fromlist=["ToolEvent"]).ToolEvent(
                "ToolResultReady", execution_id, tool_name, call_index,
                data={
                    "truncated": truncated, "duration_ms": duration,
                    "formatted": formatted,
                    "allowed": True, "is_error": False, "error": "",
                },
            )
        )

        return ToolResult(
            id=f"{execution_id}_{call_index}",
            tool_name=tool_name,
            execution_id=execution_id,
            call_index=call_index,
            allowed=True,
            data=result_data,
            formatted=formatted,
            duration_ms=duration,
            truncated=truncated,
            persist_path=persist_path,
        )

    def _execute_isolated(self, tool_name: str, args: dict,
                          timeout: float = 60.0) -> dict:
        """通过 ProcessToolRunner 在子进程中执行工具。

        子进程崩溃→异常传播到 Step 4 的 catch 块→被 classify_tool_error 捕获。
        子进程超时→SubprocessResult.timed_out=True→抛出 TimeoutError。
        """
        from backend.core.loop.process_tool_runner import ProcessToolRunner
        runner = ProcessToolRunner(timeout=timeout)
        result = runner.run(tool_name, args)
        if not result.success:
            if result.timed_out:
                raise TimeoutError(
                    f"Tool '{tool_name}' timed out after {timeout}s"
                )
            raise RuntimeError(result.error or f"Tool '{tool_name}' failed")
        return result.data or {}

    def _error_result(self, tool_name, execution_id, call_index, start, error,
                       diagnostics: dict | None = None):
        duration = (time.time() - start) * 1000
        return ToolResult(
            id=f"{execution_id}_{call_index}",
            tool_name=tool_name,
            execution_id=execution_id,
            call_index=call_index,
            allowed=False,
            is_error=True,
            error=error,
            formatted=f"[工具 {tool_name} 错误: {error}]",
            duration_ms=duration,
            diagnostics=diagnostics or {},
        )


def _validate_args(args: dict, schema: dict) -> None:
    """用 JSON Schema 做运行时类型验证。

    只验证 required 字段存在 + 类型匹配。不做 full JSON Schema 验证（保持轻量）。
    """
    required = schema.get("required", [])
    properties = schema.get("properties", {})

    for field_name in required:
        if field_name not in args:
            raise ValueError(
                f"缺少必需参数 '{field_name}'。"
                f"Schema 要求: {required}"
            )

    for field_name, value in args.items():
        if field_name in properties:
            expected = properties[field_name].get("type", "")
            actual = _typeof(value)
            if expected and actual != expected:
                raise ValueError(
                    f"参数 '{field_name}' 类型错误: 期望 {expected}, "
                    f"收到 {actual} (值: {str(value)[:60]})"
                )


def _typeof(value) -> str:
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _maybe_truncate(tool_name: str, result_data: dict | None,
                    workspace_path: str) -> tuple[str, bool, str | None]:
    """截断工具结果。超限时持久化完整 JSON，返回预览。"""
    from backend.core.dispatch.truncation import (
        MAX_OUTPUT_CHARS, PREVIEW_CHARS, format_tool_result as trunc_format,
    )
    if result_data is None:
        return f"[工具 {tool_name} 完成，无输出]", False, None

    output = json.dumps(result_data, ensure_ascii=False, indent=2)
    if len(output) <= MAX_OUTPUT_CHARS:
        return f"[工具 {tool_name} 结果]\n{output}", False, None

    formatted = trunc_format(tool_name, result_data, workspace_path)
    # extract persist_path from formatted
    truncated = True
    persist_path = None
    if "完整内容已保存至" in formatted:
        import re
        m = re.search(r"保存至\s+(\S+\.json)", formatted)
        if m:
            persist_path = m.group(1)
    return formatted, truncated, persist_path
