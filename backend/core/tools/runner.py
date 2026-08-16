"""gitgo Tool Runner —— 子进程工具执行入口点。

由 ProcessToolRunner 通过 subprocess 调用：
    python -m backend.core.tools.runner

协议：
- stdin:  {"tool_name": "...", "args": {...}}
- stdout: {"success": true, "data": {...}} 或 {"success": false, "error": "..."}
- stderr: 诊断日志（不解析）

工具注册表：
    启动时自动导入 backend.core.tools.registrations 模块（如果存在）。
    daemon 启动时写入该模块，注册可在子进程中安全执行的工具。
    闭包工具（捕获 session/hash_cache）不能序列化→留在进程内执行。
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Callable


# ── Tool Registry ──────────────────────────────────────────

_TOOL_REGISTRY: dict[str, Callable] = {}


def register(name: str, fn: Callable) -> None:
    """注册工具到 runner 的全局注册表。"""
    _TOOL_REGISTRY[name] = fn


def unregister(name: str) -> None:
    """从注册表中移除工具。"""
    _TOOL_REGISTRY.pop(name, None)


def _auto_import_registrations() -> None:
    """自动导入 registrations 模块（如果存在）。

    支持两种方式：
    1. 标准导入：backend.core.tools.registrations
    2. 环境变量 GITGO_TOOL_REGISTRY_MODULE 指向自定义模块
    """
    registry_module = os.environ.get("GITGO_TOOL_REGISTRY_MODULE", "")
    candidates = []
    if registry_module:
        candidates.append(registry_module)
    candidates.append("backend.core.tools.registrations")

    for mod_name in candidates:
        try:
            mod = __import__(mod_name, fromlist=["register_all"])
            if hasattr(mod, "register_all"):
                mod.register_all(register)
        except ImportError:
            pass
        except Exception:
            pass


# ── Main Entry Point ───────────────────────────────────────

def main() -> None:
    """从 stdin 读取 tool_name + args → 执行 → stdout 输出 result。

    由 ProcessToolRunner 通过 subprocess 调用。
    所有异常都被捕获并返回 error——不会让子进程崩溃传播到 daemon。
    """
    _auto_import_registrations()

    try:
        raw = sys.stdin.read()
        request = json.loads(raw)
    except (json.JSONDecodeError, Exception) as exc:
        _emit_error(f"invalid stdin JSON: {exc}")
        return

    tool_name = request.get("tool_name", "")
    args = request.get("args", {})

    tool_fn = _TOOL_REGISTRY.get(tool_name)
    if tool_fn is None:
        _emit_error(f"unknown tool: {tool_name}")
        return

    try:
        result = tool_fn(args)
        if not isinstance(result, dict):
            result = {"result": result}
        _emit_success(result)
    except Exception as exc:
        tb = traceback.format_exc()
        sys.stderr.write(tb)
        _emit_error(f"{type(exc).__name__}: {exc}")


def _emit_success(data: dict) -> None:
    json.dump({"success": True, "data": data}, sys.stdout,
              ensure_ascii=False, default=str)
    sys.stdout.flush()


def _emit_error(message: str) -> None:
    json.dump({"success": False, "error": message}, sys.stdout,
              ensure_ascii=False)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
