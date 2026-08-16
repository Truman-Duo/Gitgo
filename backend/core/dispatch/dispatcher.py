"""ToolDispatcher — RingGate-enforced tool execution with audit trail."""

from dataclasses import dataclass
import time
from typing import Callable

from backend.core.loop.gate import RingGate
from backend.core.loop.models import AgentProcess, ProcessStatus

# 工具执行后需要捕获工作区 diff 的文件修改类工具。
_FILE_MODIFYING_TOOLS = {"write_file", "edit", "bash", "shell", "execute", "apply_patch"}


def _referenced_files(args: dict) -> list[str]:
    """从工具参数中提取涉及的文件路径（与 executor._extract_referenced_files 同思路）。"""
    files = []
    for key in ("file", "path", "files", "target", "source"):
        val = args.get(key)
        if isinstance(val, str) and val:
            files.append(val)
        elif isinstance(val, list):
            files.extend([v for v in val if isinstance(v, str)])
    return files


@dataclass
class DispatchResult:
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
                 history_writer: Callable | None = None,
                 git_runner=None):
        self._gate = gate
        self._executors = tool_executors
        self._history = history_writer
        self._git_runner = git_runner

    def dispatch(self, process: AgentProcess, tool_name: str,
                 args: dict) -> DispatchResult:
        start = time.time()

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

        try:
            result_data = executor(args)
            duration = (time.time() - start) * 1000
            process.steps_used += 1

            diff = self._capture_diff(tool_name, args)
            if diff and isinstance(result_data, dict):
                result_data["diff"] = diff

            self._log(process, tool_name, args,
                      allowed=True, duration_ms=duration, diff=diff)

            steps_remaining = process.max_steps - process.steps_used
            if steps_remaining <= 0:
                process.status = ProcessStatus.KILLED

            return DispatchResult(
                allowed=True, data=result_data,
                duration_ms=duration, steps_remaining=steps_remaining,
            )
        except Exception as exc:
            duration = (time.time() - start) * 1000
            self._log(process, tool_name, args,
                      allowed=True, error=str(exc), duration_ms=duration)
            return DispatchResult(
                allowed=True, error=str(exc),
                data={"message": f"Tool error: {exc}", "is_error": True},
                duration_ms=duration,
            )

    def _capture_diff(self, tool_name: str, args: dict) -> str:
        """对文件修改类工具捕获 `git diff --unified=3` 原始输出。"""
        if self._git_runner is None or tool_name not in _FILE_MODIFYING_TOOLS:
            return ""
        paths = _referenced_files(args)
        cmd = ["diff", "--unified=3"]
        if paths:
            cmd += ["--"] + paths
        try:
            r = self._git_runner.run(cmd)
            if r.stdout and r.stdout.strip():
                return r.stdout
        except Exception:
            pass
        return ""

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
