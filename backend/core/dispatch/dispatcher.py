"""ToolDispatcher — RingGate-enforced tool execution with audit trail."""

from dataclasses import dataclass
import uuid
from backend.core.loop.lifecycle import transition
from typing import Callable

from backend.core.loop.gate import RingGate
from backend.core.loop.models import AgentProcess, ProcessStatus

@dataclass
class DispatchResult:
    allowed: bool
    data: dict | None = None
    error: str = ""
    duration_ms: float = 0.0
    steps_remaining: int = 0


class ToolDispatcher:
    """每次 tool dispatch 前执行 RingGate 检查，后记录审计 event。

    Legacy command transport adapter for the canonical ToolPipeline.
    tool_executors contains typed AgentTool registrations; the process registry
    limits availability and does not replace admission or execution authority.
    """

    def __init__(self, gate: RingGate,
                 tool_executors: dict[str, Callable],
                 history_writer: Callable | None = None):
        self._gate = gate
        self._executors = tool_executors
        self._history = history_writer

    def dispatch(self, process: AgentProcess, tool_name: str,
                 args: dict) -> DispatchResult:
        if process.status != ProcessStatus.RUNNING:
            return DispatchResult(
                allowed=False, error="PROCESS_NOT_RUNNING",
                data={"message": f"Process is {process.status.value}"},
            )

        gate_result = self._gate.check(process, tool_name)
        if not gate_result.allowed:
            self._log(process, tool_name, args,
                      allowed=False, error=gate_result.error, duration_ms=0)
            allowed_tools = (process.tool_registry.list_all()
                             if process.tool_registry else [])
            return DispatchResult(
                allowed=False, error=gate_result.error,
                data={"message": gate_result.message,
                      "allowed_tools": allowed_tools},
            )

        executor = self._executors.get(tool_name)
        if executor is None:
            return DispatchResult(
                allowed=False, error="TOOL_NOT_FOUND",
                data={"message": f"Tool '{tool_name}' not found in executor registry"},
            )

        from backend.core.loop.agent_tool import AgentTool
        if isinstance(executor, AgentTool):
            # Compatibility command transport must not become a second execution
            # authority. Typed registrations use the same admission, grants,
            # exact file service, native runner and receipts as model tool calls.
            return self._dispatch_registered(process, executor, args)

        self._log(process, tool_name, args, allowed=False,
            error="UNREGISTERED_EXECUTION_CONTRACT")
        return DispatchResult(allowed=False, error="UNREGISTERED_EXECUTION_CONTRACT",
            data={"message":"Tools require a typed execution registration."})

    def _dispatch_registered(self, process, tool, args):
        from backend.core.loop.execution_context import ExecutionContext
        from backend.core.loop.tool_pipeline import ToolPipeline
        workspace = str(process.worktree_path or process.workspace_root or "")
        if not workspace or process.session is None:
            return DispatchResult(allowed=False, error="EXECUTION_CONTEXT_UNAVAILABLE")
        manager = getattr(process, "_manager", None)
        ctx = ExecutionContext(process, process.session, workspace,
            cancellation=process.cancellation_event,
            storage=getattr(process, "runtime_storage", None) or getattr(manager, "storage", None),
            artifacts={"tool_catalog": self._executors})
        result = ToolPipeline(self._gate).execute(
            {"name": tool.name, "args": args}, tool, ctx, "dispatch-" + uuid.uuid4().hex)
        process.tool_receipts.append(result.receipt)
        process.session.append_tool_result(result.formatted, tool_name=tool.name,
            is_error=result.is_error, data=result.data, receipt=result.receipt)
        process.steps_used += 1
        remaining = max(0, process.max_steps - process.steps_used)
        if remaining == 0:
            transition(process, ProcessStatus.KILLED)
        self._log(process, tool.name, args, allowed=result.allowed,
            error=result.error, duration_ms=result.duration_ms)
        return DispatchResult(allowed=result.allowed, data=result.data,
            error=result.error, duration_ms=result.duration_ms, steps_remaining=remaining)

    def _log(self, process, tool_name, args, *,
             allowed=True, error="", duration_ms=0.0, diff=""):
        if self._history is None:
            return
        detail = {
            "tool_name": tool_name,
            "args_summary": str(args)[:200],
            "duration_ms": duration_ms,
            "process_id": process.process_id,
            "role": process.role,
            "ring_level": process.ring_level.value,
            "allowed": allowed,
            "error": error,
        }
        if diff:
            detail["diff"] = diff
        self._history(
            "system", "tool_executed",
            "success" if allowed else "denied",
            detail,
            correlation_id=process.process_id,
        )
