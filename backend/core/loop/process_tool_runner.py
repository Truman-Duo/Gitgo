"""Native-only tool process entry.

Every child handler comes from the trusted typed registry and is launched via
sandbox_popen. Unsupported platforms/capabilities and unknown handlers fail
closed; there is no ordinary-user subprocess retry. JSON transport remains a
legacy tool protocol, not a trusted Host execution envelope.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.child_process import owned_child_cwd, tool_runner_command
from backend.core.protocol_io import dump_protocol_json
from backend.core.sandbox import SandboxDenied


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
    effect_state: str = ""


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
            timeout: float | None = None,
            cancellation_event: threading.Event | None = None) -> SubprocessResult:
        """在子进程中执行工具。

        Args:
            tool_name: 工具名（对应 runner.py 中的注册表）
            args: 工具参数
            timeout: 超时秒数，None 则使用实例默认值
        """
        effective_timeout = timeout if timeout is not None else self._timeout
        input_data = {"tool_name": tool_name, "args": args}
        start = time.monotonic()

        # A cancellation already observed by the Host must not launch any
        # code. Checking only after Popen permits avoidable side effects.
        if cancellation_event is not None and cancellation_event.is_set():
            return SubprocessResult(
                success=False, error=f"tool '{tool_name}' cancelled before execution",
                duration_ms=(time.monotonic() - start) * 1000,
                effect_state="not_committed",
            )

        try:
            from backend.core.process_control import attach_kill_job, close_job, creation_flags
            from backend.core.tools.runner import handler_bindings
            binding = handler_bindings().get(tool_name)
            if binding is None:
                raise SandboxDenied("SANDBOX_POLICY_INVALID", "Unknown Host-registered process handler.")
            # Every registered child handler is native; missing names or future
            # registrations can never fall through to ordinary subprocess.Popen.
            child_env = os.environ.copy()
            source_root = Path(__file__).resolve().parents[3]
            from backend.core.sandbox import SandboxPolicy, sandbox_environment, sandbox_popen
            workspace = args.get("_workspace") or args.get("workspace_path")
            if not workspace:
                raise SandboxDenied("SANDBOX_POLICY_INVALID", "Host execution workspace is required.")
            policy = SandboxPolicy(Path(str(workspace)),
                cpu_seconds=max(1, min(int(effective_timeout), 1800)))
            child_env = sandbox_environment(child_env)
            input_data["_native_sandbox_workspace"] = str(policy.workspace)
            input_data["args"] = {**args, "_workspace": str(policy.workspace)}
            proc = sandbox_popen(
                tool_runner_command(), policy,
                # DaemonClient starts ``python -m gitgo`` from the package's
                # parent directory.  Relying on inherited cwd therefore makes
                # the top-level ``backend`` module disappear only in real
                # daemon runs.  Anchor the isolated runner at the package root.
                cwd=str(owned_child_cwd(source_root)),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                env=child_env,
                creationflags=creation_flags(),
                start_new_session=sys.platform != "win32",
            )
            if not getattr(proc, "_gitgo_job_handle", None):
                proc._gitgo_job_handle = attach_kill_job(proc)
            from backend.core.sandbox_io import BoundedCommunication
            communication = BoundedCommunication(proc)

            try:
                payload = dump_protocol_json(input_data)
                stdout_str = ""
                stderr_str = ""
                first = True
                while True:
                    if cancellation_event is not None and cancellation_event.is_set():
                        self._kill_tree(proc)
                        return SubprocessResult(
                            success=False,
                            error=f"tool '{tool_name}' cancelled",
                            effect_state="ambiguous",
                            exit_code=-1,
                            duration_ms=(time.monotonic() - start) * 1000,
                        )
                    elapsed = time.monotonic() - start
                    if elapsed >= effective_timeout:
                        raise subprocess.TimeoutExpired(proc.args, effective_timeout)
                    try:
                        stdout_str, stderr_str = communication.communicate(
                            input=payload if first else None,
                            timeout=min(0.2, effective_timeout - elapsed),
                        )
                        break
                    except subprocess.TimeoutExpired:
                        first = False
                        continue
                duration_ms = (time.monotonic() - start) * 1000
                close_job(getattr(proc, "_gitgo_job_handle", None))
                proc._gitgo_job_handle = None

                if proc.returncode != 0:
                    denial = SandboxDenied("SANDBOX_EXECUTION_FAILED",
                        "Sandboxed runtime exited without a tool result. Inspect captured diagnostics.")
                    denial.effect_state = "ambiguous"
                    return SubprocessResult(success=True, data=denial.result(),
                        exit_code=proc.returncode, duration_ms=duration_ms,
                        stderr=stderr_str)

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
                duration_ms = (time.monotonic() - start) * 1000
                self._kill_tree(proc)
                return SubprocessResult(
                    success=False,
                    error=f"tool '{tool_name}' timed out after {effective_timeout}s",
                    exit_code=-1,
                    duration_ms=duration_ms,
                    timed_out=True,
                    effect_state="ambiguous",
                    stderr="",
                )

        except SandboxDenied as exc:
            spawned = locals().get("proc")
            if spawned is not None:
                self._kill_tree(spawned)
            return SubprocessResult(
                success=True, data=exc.result(),
                duration_ms=(time.monotonic() - start) * 1000,
            )
        except FileNotFoundError:
            duration_ms = (time.monotonic() - start) * 1000
            return SubprocessResult(
                success=False,
                error="runner module not found: backend.core.tools.runner",
                exit_code=-1,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            spawned = locals().get("proc")
            if spawned is not None and spawned.poll() is None:
                self._kill_tree(spawned)
            duration_ms = (time.monotonic() - start) * 1000
            return SubprocessResult(
                success=False,
                error=f"subprocess spawn failed: {exc}",
                exit_code=-1,
                duration_ms=duration_ms,
            )

        finally:
            spawned = locals().get("proc")
            if spawned is not None:
                from backend.core.process_control import close_job
                close_job(getattr(spawned, "_gitgo_job_handle", None))
                spawned._gitgo_job_handle = None
                if hasattr(spawned, "close_sandbox"):
                    spawned.close_sandbox()
                for stream in (spawned.stdin, spawned.stdout, spawned.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """强制杀死进程树。

        Windows: taskkill /F /T /PID
        Unix: os.killpg (需要进程组)
        """
        from backend.core.process_control import terminate_tree
        terminate_tree(proc, getattr(proc, "_gitgo_job_handle", None))
        proc._gitgo_job_handle = None
