"""gitgo Tool Runner —— 子进程工具执行入口点。

由 ProcessToolRunner 通过 subprocess 调用：
    python -m backend.core.tools.runner

协议：
- stdin:  {"tool_name": "...", "args": {...}}
- stdout: {"success": true, "data": {...}} 或 {"success": false, "error": "..."}
- stderr: 诊断日志（不解析）

工具注册表：
    只读取发行代码 backend.core.tools.registrations 的显式原生执行声明。
    完整注册失败则拒绝执行；环境变量不能选取注册模块。
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Callable
from dataclasses import dataclass
from backend.core.loop.execution_contract import ExecutionContract, ExecutionType
from backend.core.protocol_io import dump_protocol_json, write_utf8_line


# ── Tool Registry ──────────────────────────────────────────

@dataclass(frozen=True)
class HandlerBinding:
    function: Callable
    execution_contract: ExecutionContract


_TOOL_REGISTRY: dict[str, HandlerBinding] = {}


def register(name: str, fn: Callable, execution_contract: ExecutionContract) -> None:
    """Child roles cannot register an ordinary Host process path."""
    if not callable(fn) or not isinstance(execution_contract, ExecutionContract):
        raise ValueError("Handler requires a Host execution contract")
    if execution_contract.kind != ExecutionType.NATIVE_PROCESS:
        raise ValueError("Child handlers require native process execution")
    if name in _TOOL_REGISTRY:
        raise ValueError("Duplicate child handler registration")
    _TOOL_REGISTRY[name] = HandlerBinding(fn, execution_contract)


def unregister(name: str) -> None:
    _TOOL_REGISTRY.pop(name, None)


def handler_bindings() -> dict[str, HandlerBinding]:
    """Build a closed trusted registry atomically; no environment-selected modules."""
    from backend.core.tools.registrations import register_all
    bindings = {}
    def collect(name, fn, contract):
        if name in bindings or not callable(fn) or not isinstance(contract, ExecutionContract):
            raise ValueError("Invalid or duplicate child handler registration")
        if contract.kind != ExecutionType.NATIVE_PROCESS:
            raise ValueError("Child handlers require native process execution")
        bindings[name] = HandlerBinding(fn, contract)
    register_all(collect)
    return bindings


def _auto_import_registrations() -> None:
    # Do not swallow a partial registration failure and continue with a subset.
    bindings = handler_bindings()
    _TOOL_REGISTRY.clear()
    _TOOL_REGISTRY.update(bindings)


# ── Main Entry Point ───────────────────────────────────────

def main() -> None:
    """从 stdin 读取 tool_name + args → 执行 → stdout 输出 result。

    由 ProcessToolRunner 通过 subprocess 调用。
    所有异常都被捕获并返回 error——不会让子进程崩溃传播到 daemon。
    """
    try:
        buffer = getattr(sys.stdin, "buffer", None)
        raw = buffer.read().decode("utf-8") if buffer is not None else sys.stdin.read()
        request = json.loads(raw)
    except (json.JSONDecodeError, Exception) as exc:
        _emit_error(f"invalid stdin JSON: {exc}")
        return

    sandbox_workspace = request.get("_native_sandbox_workspace")
    if sandbox_workspace:
        from backend.core.sandbox import SandboxDenied, prepare_child_environment
        try:
            prepare_child_environment(str(sandbox_workspace))
        except SandboxDenied as exc:
            _emit_success(exc.result())
            return
        except OSError as exc:
            _emit_success(SandboxDenied("SANDBOX_LAUNCH_DENIED",
                f"Cannot prepare private sandbox home: {exc}").result())
            return

    try:
        _auto_import_registrations()
    except Exception:
        _emit_error("trusted handler registration failed")
        return
    tool_name = request.get("tool_name", "")
    args = request.get("args", {})

    binding = _TOOL_REGISTRY.get(tool_name)
    if binding is None:
        _emit_error(f"unknown tool: {tool_name}")
        return

    try:
        result = binding.function(args)
        if not isinstance(result, dict):
            result = {"result": result}
        _emit_success(result)
    except Exception as exc:
        tb = traceback.format_exc()
        sys.stderr.write(tb)
        _emit_error(f"{type(exc).__name__}: {exc}")


def _emit_success(data: dict) -> None:
    write_utf8_line(
        sys.stdout,
        dump_protocol_json({"success": True, "data": data}, default=str),
    )


def _emit_error(message: str) -> None:
    write_utf8_line(
        sys.stdout,
        dump_protocol_json({"success": False, "error": message}),
    )


if __name__ == "__main__":
    main()
