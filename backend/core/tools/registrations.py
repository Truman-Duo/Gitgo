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


def register_all(register) -> None:
    """注册所有子进程安全的基础工具。

    由 runner._auto_import_registrations() 调用。
    register 是一个 Callable(name, fn)，将工具加入 runner 的注册表。
    """
    register("file_read", file_read)
    register("file_write", file_write)
    register("file_edit", file_edit)
    register("file_delete", file_delete)
    register("file_list", file_list)
    register("file_search", file_search)
    register("git_status", git_status)
    register("git_diff", git_diff)
    register("git_log", git_log)
    register("git_branch", git_branch)
    register("run_command", run_command)


# ── File Tools ─────────────────────────────────────────────

def file_read(args: dict) -> dict:
    """读取文件内容。

    args: path (str, 相对于 workspace 或绝对路径), encoding (str, 默认 utf-8),
          lines (int, 可选, 只返回前 N 行)
    """
    path = args.get("path", "")
    encoding = args.get("encoding", "utf-8")
    max_lines = args.get("lines")

    p = _resolve_path(path)
    if not p.exists():
        return {"error": "FILE_NOT_FOUND", "path": path}
    if p.is_dir():
        return {"error": "IS_DIRECTORY", "path": path}

    try:
        content = p.read_text(encoding=encoding)
        total_lines = content.count("\n") + 1
        if max_lines and max_lines > 0:
            lines = content.splitlines()
            content = "\n".join(lines[:max_lines])
            if len(lines) > max_lines:
                content += f"\n... ({len(lines) - max_lines} more lines)"
        return {
            "content": content,
            "path": str(p),
            "size": p.stat().st_size,
            "lines": total_lines,
            "truncated": max_lines is not None and total_lines > max_lines,
        }
    except UnicodeDecodeError:
        return {"error": "BINARY_FILE", "path": path}
    except OSError as e:
        return {"error": "READ_ERROR", "path": path, "detail": str(e)}


def file_write(args: dict) -> dict:
    """写入文件内容。

    args: path (str), content (str), encoding (str, 默认 utf-8),
          mode (str, "w"=覆盖 "a"=追加, 默认 "w")
    """
    path = args.get("path", "")
    content = args.get("content", "")
    encoding = args.get("encoding", "utf-8")
    mode = args.get("mode", "w")

    if not path:
        return {"error": "MISSING_PATH"}
    if mode not in ("w", "a"):
        return {"error": "INVALID_MODE", "mode": mode, "allowed": ["w", "a"]}

    p = _resolve_path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding)
        return {
            "path": str(p),
            "size": p.stat().st_size,
            "action": "created" if mode == "w" else "appended",
        }
    except OSError as e:
        return {"error": "WRITE_ERROR", "path": path, "detail": str(e)}


def file_edit(args: dict) -> dict:
    """编辑文件——查找替换。

    args: path (str), old_string (str), new_string (str),
          replace_all (bool, 默认 False=只替换首次出现)
    """
    path = args.get("path", "")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    replace_all = args.get("replace_all", False)

    p = _resolve_path(path)
    if not p.exists():
        return {"error": "FILE_NOT_FOUND", "path": path}

    try:
        content = p.read_text(encoding="utf-8")
    except OSError as e:
        return {"error": "READ_ERROR", "path": path, "detail": str(e)}

    count = content.count(old)
    if count == 0:
        return {"error": "STRING_NOT_FOUND", "path": path, "count": 0}

    if replace_all:
        new_content = content.replace(old, new)
        replacements = count
    else:
        new_content = content.replace(old, new, 1)
        replacements = 1

    try:
        p.write_text(new_content, encoding="utf-8")
    except OSError as e:
        return {"error": "WRITE_ERROR", "path": path, "detail": str(e)}

    return {
        "path": str(p),
        "replacements": replacements,
        "total_occurrences": count,
        "replaced_all": replace_all,
    }


def file_delete(args: dict) -> dict:
    """删除文件。

    args: path (str)
    """
    path = args.get("path", "")
    p = _resolve_path(path)
    if not p.exists():
        return {"error": "FILE_NOT_FOUND", "path": path}
    try:
        size = p.stat().st_size
        p.unlink()
        return {"path": str(p), "action": "deleted", "size": size}
    except OSError as e:
        return {"error": "DELETE_ERROR", "path": path, "detail": str(e)}


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


# ── Helpers ────────────────────────────────────────────────

def _resolve_path(raw: str) -> Path:
    """解析路径：相对路径基于当前工作目录。"""
    p = Path(raw)
    if p.is_absolute():
        return p
    return Path.cwd() / p
