"""ToolDispatcher — RingGate-enforced tool execution with audit trail."""

from dataclasses import dataclass
import time
from typing import Callable

from backend.core.loop.gate import RingGate
from backend.core.loop.models import AgentProcess, ProcessStatus


@dataclass
class ToolResult:
    allowed: bool
    data: dict | None = None
    error: str = ""
    duration_ms: float = 0.0
    steps_remaining: int = 0


class ToolDispatcher:
    """每次 tool dispatch 前执行 RingGate 检查，后记录审计 event。

    不替代 MCP tool 注册——在 MCP tool 的实际执行之前作为独立调度层存在。
    tool_executors 是 {tool_name: callable} 映射，与 AgentProcess.tool_registry
    （ToolRegistry 对象，定义权限）是不同的概念。
    """

    def __init__(self, gate: RingGate,
                 tool_executors: dict[str, Callable],
                 history_writer: Callable | None = None):
        self._gate = gate
        self._executors = tool_executors
        self._history = history_writer

    def dispatch(self, process: AgentProcess, tool_name: str,
                 args: dict) -> ToolResult:
        start = time.time()

        if process.status != ProcessStatus.RUNNING:
            return ToolResult(
                allowed=False, error="PROCESS_NOT_RUNNING",
                data={"message": f"Process is {process.status.value}"},
            )

        gate_result = self._gate.check(process, tool_name)
        if not gate_result.allowed:
            self._log(process, tool_name, args,
                      allowed=False, error=gate_result.error, duration_ms=0)
            allowed_tools = (process.tool_registry.list_all()
                             if process.tool_registry else [])
            return ToolResult(
                allowed=False, error=gate_result.error,
                data={"message": gate_result.message,
                      "allowed_tools": allowed_tools},
            )

        executor = self._executors.get(tool_name)
        if executor is None:
            return ToolResult(
                allowed=False, error="TOOL_NOT_FOUND",
                data={"message": f"Tool '{tool_name}' not found in executor registry"},
            )

        try:
            result_data = executor(args)
            duration = (time.time() - start) * 1000
            process.steps_used += 1

            self._log(process, tool_name, args,
                      allowed=True, duration_ms=duration)

            steps_remaining = process.max_steps - process.steps_used
            if steps_remaining <= 0:
                process.status = ProcessStatus.KILLED

            return ToolResult(
                allowed=True, data=result_data,
                duration_ms=duration, steps_remaining=steps_remaining,
            )
        except Exception as exc:
            duration = (time.time() - start) * 1000
            self._log(process, tool_name, args,
                      allowed=True, error=str(exc), duration_ms=duration)
            return ToolResult(
                allowed=True, error=str(exc),
                data={"message": f"Tool error: {exc}", "is_error": True},
                duration_ms=duration,
            )

    def _log(self, process, tool_name, args, *,
             allowed=True, error="", duration_ms=0.0):
        if self._history is None:
            return
        self._history(
            "system", "tool_executed",
            "success" if allowed else "denied",
            {
                "tool_name": tool_name,
                "args_summary": str(args)[:200],
                "duration_ms": duration_ms,
                "process_id": process.process_id,
                "role": process.role,
                "ring_level": process.ring_level.value,
                "allowed": allowed,
                "error": error,
            },
            correlation_id=process.process_id,
        )
