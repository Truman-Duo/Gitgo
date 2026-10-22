"""gitgo Tool Registrations —— 子进程安全的基础工具集。

由 backend.core.tools.runner 在子进程启动时自动导入。
所有工具必须是纯函数（不捕获闭包变量），参数和返回值均可 JSON 序列化。

工具命名规范：<domain>_<action>，如 file_read, git_status。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from backend.core.loop.execution_contract import NATIVE_PROCESS, NATIVE_FILE_READ, NATIVE_WORKSPACE_PRODUCER


def register_all(register) -> None:
    """注册所有子进程安全的基础工具。

    由 runner._auto_import_registrations() 调用。
    register 是一个 Callable(name, fn, execution_contract)，将工具加入 runner 的注册表。
    """
    register("file_read", file_read, NATIVE_FILE_READ)
    register("file_write", file_write, NATIVE_PROCESS)
    register("file_edit", file_edit, NATIVE_PROCESS)
    register("file_delete", file_delete, NATIVE_PROCESS)
    register("file_list", file_list, NATIVE_PROCESS)
    register("file_search", file_search, NATIVE_PROCESS)
    register("git_status", git_status, NATIVE_PROCESS)
    register("git_diff", git_diff, NATIVE_PROCESS)
    register("git_log", git_log, NATIVE_PROCESS)
    register("git_branch", git_branch, NATIVE_PROCESS)
    register("run_command", run_command, NATIVE_PROCESS)

    # Canonical Agent development tools.  Legacy names above remain available
    # for compatibility, but new capability profiles use these confined,
    # receipt-friendly implementations.
    from backend.core.tools import workspace_tools
    register("read_file", workspace_tools.read_file, NATIVE_FILE_READ)
    register("list_files", workspace_tools.list_files, NATIVE_PROCESS)
    register("search_text", workspace_tools.search_text, NATIVE_PROCESS)
    register("edit_file", file_edit, NATIVE_PROCESS)
    register("write_file", file_write, NATIVE_PROCESS)
    register("delete_file", file_delete, NATIVE_PROCESS)
    register("apply_patch", _patch_requires_service, NATIVE_PROCESS)
    register("exec_command", workspace_tools.exec_command, NATIVE_WORKSPACE_PRODUCER)
    register("shell_script", workspace_tools.shell_script, NATIVE_WORKSPACE_PRODUCER)
    register("dependency_feedback", workspace_tools.dependency_feedback, NATIVE_PROCESS)
    register("rebuild_dependency_graph", workspace_tools.rebuild_dependency_graph, NATIVE_PROCESS)
    from backend.core.tools.document_tools import render_document, document_open
    register("document_open", document_open, NATIVE_PROCESS)
    register("document_create", _document_requires_service, NATIVE_PROCESS)
    register("document_render", render_document, NATIVE_PROCESS)

    # Existing governance tools are also available to the isolated runner.  The
    # daemon keeps the original public names, so profiles and old transcripts do
    # not break while their execution boundary becomes cancellable.
    from backend.core.loop import tool_wrappers
    register("contract_detect_drift", tool_wrappers.contract_detect_drift, NATIVE_PROCESS)
    register("contract_get_impact", tool_wrappers.contract_get_impact, NATIVE_PROCESS)
    register("contract_get_changed_symbols", tool_wrappers.contract_get_changed_symbols, NATIVE_PROCESS)
    register("lesson_search", tool_wrappers.lesson_search, NATIVE_PROCESS)
    register("lesson_discard", tool_wrappers.lesson_discard, NATIVE_PROCESS)
    register("lesson_verify", tool_wrappers.lesson_verify, NATIVE_PROCESS)
    register("lesson_promote", tool_wrappers.lesson_promote, NATIVE_PROCESS)
    register("lesson_list", tool_wrappers.lesson_list, NATIVE_PROCESS)
    register("privacy_scan", tool_wrappers.privacy_scan, NATIVE_PROCESS)
    register("run_test", _run_test_isolated, NATIVE_PROCESS)
    from backend.core.tools.dynamic_tools import (
        execute_authored_python, execute_authored_privileged_python, execute_composite,
    )
    register("dynamic_composite", execute_composite, NATIVE_PROCESS)
    register("authored_python", execute_authored_python, NATIVE_PROCESS)
    register("authored_privileged_python", execute_authored_privileged_python, NATIVE_WORKSPACE_PRODUCER)
    from backend.core.tools.authored_bridge import execute_authored_host_python
    register("authored_host_python", execute_authored_host_python, NATIVE_PROCESS)


# ── File Tools ─────────────────────────────────────────────

def file_read(args: dict) -> dict:
    """读取文件内容。

    args: path (str, 相对于 workspace 或绝对路径), encoding (str, 默认 utf-8),
          lines (int, 可选, 只返回前 N 行)
    """
    path = args.get("path", "")
    encoding = args.get("encoding", "utf-8")
    max_lines = args.get("lines")

    from backend.core.sandbox_read import ReadBoundaryError
    from backend.core.tools.workspace_tools import _read_target
    try:
        _workspace, p, root, relative = _read_target(args)
    except ReadBoundaryError as exc:
        return {"error": "READ_ERROR", "path": path, "detail": str(exc)}
    try:
        from backend.core.sandbox_read_plan import open_planned_read
        with open_planned_read(root, relative) as opened:
            raw = opened.read_bytes()
        content = raw.decode(encoding)
        total_lines = content.count("\n") + 1
        if max_lines and max_lines > 0:
            lines = content.splitlines()
            content = "\n".join(lines[:max_lines])
            if len(lines) > max_lines:
                content += f"\n... ({len(lines) - max_lines} more lines)"
        return {
            "content": content,
            "path": str(p),
            "size": len(raw),
            "lines": total_lines,
            "truncated": max_lines is not None and total_lines > max_lines,
        }
    except FileNotFoundError:
        return {"error": "FILE_NOT_FOUND", "path": path}
    except IsADirectoryError:
        return {"error": "IS_DIRECTORY", "path": path}
    except UnicodeDecodeError:
        return {"error": "BINARY_FILE", "path": path}
    except OSError as e:
        return {"error": "READ_ERROR", "path": path, "detail": str(e)}


def _file_service_required(canonical: str) -> dict:
    # Native children do not own the private lesson inventory, writer locks or
    # commit authority. Keeping a native registration must not restore a write
    # path around the trusted catalog's file service.
    from backend.core.errors import error_payload
    return {**error_payload("FILE_SERVICE_REQUIRED", next_actions=[{
        "action": canonical, "effect": "Use the registered file-service tool through ToolPipeline; do not retry via the native runner or expose private state to the sandbox."}]),
        "effect_state": "not_committed", "execution_state": "not_started"}


def file_write(args: dict) -> dict:
    return _file_service_required("write_file")


def file_edit(args: dict) -> dict:
    return _file_service_required("edit_file")


def file_delete(args: dict) -> dict:
    return _file_service_required("delete_file")


def _document_requires_service(args: dict) -> dict:
    return _file_service_required("document_create")


def _patch_requires_service(args: dict) -> dict:
    return _file_service_required("apply_patch")


def file_list(args: dict) -> dict:
    """列出目录文件。

    args: path (str, 默认 "."), pattern (str, 可选 glob, 如 "*.py"),
          recursive (bool, 默认 True), max_results (int, 默认 100)
    """
    path = args.get("path", ".")
    pattern = args.get("pattern", "*")
    recursive = args.get("recursive", True)
    max_results = args.get("max_results", 100)

    p = _resolve_path(path)
    if not p.exists():
        return {"error": "DIR_NOT_FOUND", "path": str(p)}

    if not p.is_dir():
        return {"error": "NOT_A_DIRECTORY", "path": str(p)}

    glob_pattern = f"**/{pattern}" if recursive else pattern
    matches = []
    for f in p.glob(glob_pattern):
        if f.is_file():
            matches.append({
                "name": f.name,
                "path": str(f),
                "size": f.stat().st_size,
            })
            if len(matches) >= max_results:
                break

    return {
        "files": matches,
        "count": len(matches),
        "directory": str(p),
        "truncated": len(matches) >= max_results,
    }


def file_search(args: dict) -> dict:
    """在文件中搜索文本（grep）。

    args: pattern (str, 正则表达式), path (str, 目录, 默认 "."),
          glob (str, 文件过滤, 如 "*.py"), max_results (int, 默认 50)
    """
    import re

    pattern = args.get("pattern", "")
    path = args.get("path", ".")
    file_glob = args.get("glob", "*")
    max_results = args.get("max_results", 50)

    if not pattern:
        return {"error": "MISSING_PATTERN"}

    p = _resolve_path(path)
    if not p.exists():
        return {"error": "DIR_NOT_FOUND", "path": str(p)}

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return {"error": "INVALID_REGEX", "detail": str(e)}

    results = []
    search_dir = p if p.is_dir() else p.parent
    for f in search_dir.rglob(file_glob):
        if not f.is_file():
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    results.append({
                        "file": str(f),
                        "line": i,
                        "text": line.strip()[:200],
                    })
                    if len(results) >= max_results:
                        break
        except OSError:
            continue
        if len(results) >= max_results:
            break

    return {
        "matches": results,
        "count": len(results),
        "pattern": pattern,
        "truncated": len(results) >= max_results,
    }


# ── Git Tools ──────────────────────────────────────────────

def _git_cmd(args: list[str], cwd: str = ".", timeout: int = 30) -> dict:
    """执行 git 命令并返回结果。"""
    creationflags = 0x08000000 if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            ["git"] + args, cwd=cwd,
            capture_output=True, text=True,
            creationflags=creationflags, timeout=timeout,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"error": "GIT_TIMEOUT", "timeout": timeout}
    except FileNotFoundError:
        return {"error": "GIT_NOT_FOUND"}


def git_status(args: dict) -> dict:
    """git status。

    args: path (str, 默认 ".")
    """
    path = args.get("path", ".")
    result = _git_cmd(["status", "--porcelain"], cwd=path)
    if "error" in result:
        return result
    lines = [l for l in result["stdout"].splitlines() if l.strip()]
    return {
        "files": lines,
        "count": len(lines),
        "clean": len(lines) == 0,
    }


def git_diff(args: dict) -> dict:
    """git diff。

    args: path (str), staged (bool, 默认 False), file (str, 可选, 单个文件)
    """
    path = args.get("path", ".")
    staged = args.get("staged", False)
    file = args.get("file", "")

    cmd = ["diff"]
    if staged:
        cmd.append("--staged")
    if file:
        cmd.extend(["--", file])

    result = _git_cmd(cmd, cwd=path)
    if "error" in result:
        return result
    return {
        "diff": result["stdout"],
        "lines": result["stdout"].count("\n"),
        "empty": not result["stdout"].strip(),
    }


def git_log(args: dict) -> dict:
    """git log。

    args: path (str), count (int, 默认 10), format (str, 默认 "%h %s")
    """
    path = args.get("path", ".")
    count = args.get("count", 10)
    fmt = args.get("format", "%h %s")

    result = _git_cmd(
        ["log", f"-{count}", f"--format={fmt}"], cwd=path,
    )
    if "error" in result:
        return result
    entries = [l for l in result["stdout"].splitlines() if l.strip()]
    return {"entries": entries, "count": len(entries)}


def git_branch(args: dict) -> dict:
    """git branch。

    args: path (str)
    """
    path = args.get("path", ".")
    result = _git_cmd(["branch", "--list"], cwd=path)
    if "error" in result:
        return result
    branches = [l.strip("* ") for l in result["stdout"].splitlines() if l.strip()]
    current = [l.strip("* ") for l in result["stdout"].splitlines() if l.startswith("*")]
    return {
        "branches": branches,
        "current": current[0] if current else "",
        "count": len(branches),
    }


# ── Shell ──────────────────────────────────────────────────

def run_command(args: dict) -> dict:
    """执行 shell 命令。

    args: command (str), cwd (str, 默认 "."), timeout (int, 默认 30)
    """
    command = args.get("command", "")
    cwd = args.get("cwd", ".")
    timeout = args.get("timeout", 30)

    if not command:
        return {"error": "MISSING_COMMAND"}

    creationflags = 0x08000000 if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            command, shell=True, cwd=cwd,
            capture_output=True, text=True,
            creationflags=creationflags, timeout=timeout,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"error": "COMMAND_TIMEOUT", "timeout": timeout}
    except Exception as e:
        return {"error": "COMMAND_ERROR", "detail": str(e)}


def _run_test_isolated(args: dict) -> dict:
    from backend.core.tools.registered_test import execute_test_seeds
    workspace = args.get("_workspace") or args.get("workspace_path") or str(Path.cwd())
    public_args = {key: value for key, value in args.items() if not key.startswith("_")}
    return execute_test_seeds(workspace, public_args)




# ── Helpers ────────────────────────────────────────────────

def _resolve_path(raw: str) -> Path:
    """解析路径：相对路径基于当前工作目录。"""
    p = Path(raw)
    if p.is_absolute():
        return p
    return Path.cwd() / p
