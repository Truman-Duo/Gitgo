"""ProcessToolRunner —— 子进程工具执行器。

通过 subprocess.Popen 在独立进程中执行工具，提供真正的进程隔离：
- 超时 → kill_tree（Windows: taskkill /F /T, Unix: os.killpg）
- 崩溃 → 子进程异常不会污染 daemon 进程
- stdin/stdout JSON 协议

与 ToolPipeline 的关系：ToolPipeline 的 Step 4 可选使用 ProcessToolRunner
替代 tool.execute(args) 内联调用，获得进程隔离保证。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class SubprocessResult:
    """子进程执行结果。"""
    success: bool
    data: dict | None = None
    error: str = ""
    exit_code: int = -1
    duration_ms: float = 0.0
    timed_out: bool = False
    stderr: str = ""


class ProcessToolRunner:
    """通过子进程执行工具，提供进程隔离 + 超时强杀。

    协议：
    - stdin → JSON: {"tool_name": "...", "args": {...}}
    - stdout → JSON: {"success": true, "data": {...}} 或 {"success": false, "error": "..."}
    - stderr → 诊断日志（不解析，超时等场景下返回）
    """

    def __init__(self, timeout: float = 60.0):
        self._timeout = timeout

    def run(self, tool_name: str, args: dict,
            timeout: float | None = None) -> SubprocessResult:
        """在子进程中执行工具。

        Args:
            tool_name: 工具名（对应 runner.py 中的注册表）
            args: 工具参数
            timeout: 超时秒数，None 则使用实例默认值
        """
        effective_timeout = timeout if timeout is not None else self._timeout
        input_data = {"tool_name": tool_name, "args": args}
        start = time.time()

        try:
            # Inherit parent env so GITGO_TOOL_REGISTRY_MODULE etc. propagate
            proc = subprocess.Popen(
                [sys.executable, "-m", "backend.core.tools.runner"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=os.environ.copy(),
            )

            try:
                stdout_str, stderr_str = proc.communicate(
                    input=json.dumps(input_data, ensure_ascii=False),
                    timeout=effective_timeout,
                )
                duration_ms = (time.time() - start) * 1000

                if proc.returncode != 0:
                    return SubprocessResult(
                        success=False,
                        error=f"subprocess exit {proc.returncode}: {stderr_str[:500]}",
                        exit_code=proc.returncode,
                        duration_ms=duration_ms,
                        stderr=stderr_str,
                    )

                result = json.loads(stdout_str)
                return SubprocessResult(
                    success=result.get("success", False),
                    data=result.get("data"),
                    error=result.get("error", ""),
                    exit_code=0,
                    duration_ms=duration_ms,
                    stderr=stderr_str,
                )

            except subprocess.TimeoutExpired:
                duration_ms = (time.time() - start) * 1000
                self._kill_tree(proc)
                return SubprocessResult(
                    success=False,
                    error=f"tool '{tool_name}' timed out after {effective_timeout}s",
                    exit_code=-1,
                    duration_ms=duration_ms,
                    timed_out=True,
                    stderr="",
                )

        except FileNotFoundError:
            duration_ms = (time.time() - start) * 1000
            return SubprocessResult(
                success=False,
                error="runner module not found: backend.core.tools.runner",
                exit_code=-1,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            duration_ms = (time.time() - start) * 1000
            return SubprocessResult(
                success=False,
                error=f"subprocess spawn failed: {exc}",
                exit_code=-1,
                duration_ms=duration_ms,
            )

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """强制杀死进程树。

        Windows: taskkill /F /T /PID
        Unix: os.killpg (需要进程组)
        """
        pid = proc.pid
        if pid is None:
            return

        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=10,
                )
            else:
                try:
                    os.killpg(pid, 9)  # SIGKILL to process group
                except ProcessLookupError:
                    pass
                except OSError:
                    os.kill(pid, 9)  # fallback: kill single process
        except Exception:
            # Best-effort kill — don't raise from kill_tree
            pass
