"""Workspace-confined implementations for the general Agent tool set.

These functions are subprocess-safe: inputs and outputs are JSON compatible,
and no function captures daemon state.  The private ``_workspace`` argument is
injected by the host and is intentionally absent from model-visible schemas.
"""

from __future__ import annotations

import hashlib
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


_IGNORED_DIRS = {
    ".git", ".gitgo", ".venv", "venv", "node_modules", "__pycache__",
    "build", "dist", "out", "target",
}
_MAX_OUTPUT = 200_000
_DENIED_EXECUTABLES = {
    "curl", "wget", "ssh", "scp", "sftp", "ftp", "nc", "ncat", "telnet",
    "rm", "rmdir", "del", "erase", "format", "diskpart", "shutdown",
}
_DENIED_GIT_SUBCOMMANDS = {
    "push", "commit", "reset", "clean", "checkout", "switch", "merge",
    "rebase", "cherry-pick", "restore", "branch", "tag", "remote",
}


def _text_diff(path: str, before: str, after: str) -> str:
    """Return a bounded unified diff for the shared Trace/UI projection."""
    rendered = "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    ))
    if len(rendered) <= 100_000:
        return rendered
    return rendered[:100_000] + "\n[diff truncated by Host]\n"


def read_file(args: dict) -> dict:
    workspace = _workspace(args)
    path = _confined_path(workspace, args.get("path", ""), must_exist=True,
                          allowed_roots=_allowed_roots(args))
    if path.is_dir():
        return {"error": "IS_DIRECTORY", "path": _rel(workspace, path)}
    offset = max(0, int(args.get("offset", 0) or 0))
    limit = max(1, min(int(args.get("limit", 2000) or 2000), 5000))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {"error": "READ_ERROR", "detail": str(exc), "path": _rel(workspace, path)}
    if b"\x00" in raw[:8192]:
        return {"error": "BINARY_FILE", "path": _rel(workspace, path)}
    text = raw.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    selected = lines[offset:offset + limit]
    return {
        "path": _rel(workspace, path),
        "content": "\n".join(selected),
        "offset": offset,
        "returned_lines": len(selected),
        "total_lines": len(lines),
        "truncated": offset + limit < len(lines),
        "next_offset": offset + len(selected) if offset + limit < len(lines) else None,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }


def list_files(args: dict) -> dict:
    workspace = _workspace(args)
    root = _confined_path(workspace, args.get("path", "."), must_exist=False,
                          allowed_roots=_allowed_roots(args))
    if not root.exists():
        return {
            "files": [], "count": 0, "truncated": False, "exists": False,
            "path": _rel(workspace, root),
        }
    if not root.is_dir():
        return {"error": "NOT_A_DIRECTORY", "path": _rel(workspace, root)}
    pattern = str(args.get("pattern", "**/*") or "**/*")
    max_results = max(1, min(int(args.get("max_results", 500) or 500), 5000))
    include_hidden = bool(args.get("include_hidden", False))
    files = []
    try:
        candidates = root.glob(pattern)
        for path in candidates:
            if not path.is_file():
                continue
            rel_parts = path.relative_to(root).parts
            if any(part in _IGNORED_DIRS for part in rel_parts):
                continue
            if not include_hidden and any(part.startswith(".") for part in rel_parts):
                continue
            files.append({
                "path": _rel(workspace, path),
                "size": path.stat().st_size,
                "modified_ns": path.stat().st_mtime_ns,
            })
            if len(files) >= max_results:
                break
    except (OSError, ValueError) as exc:
        return {"error": "LIST_ERROR", "detail": str(exc)}
    files.sort(key=lambda item: item["path"])
    return {"files": files, "count": len(files), "truncated": len(files) >= max_results}


def search_text(args: dict) -> dict:
    workspace = _workspace(args)
    root = _confined_path(workspace, args.get("path", "."), must_exist=True,
                          allowed_roots=_allowed_roots(args))
    pattern = str(args.get("pattern", ""))
    if not pattern:
        return {"error": "MISSING_PATTERN"}
    literal = bool(args.get("literal", False))
    case_sensitive = bool(args.get("case_sensitive", False))
    include = list(args.get("include", []) or [])
    max_results = max(1, min(int(args.get("max_results", 200) or 200), 2000))
    rg = shutil.which("rg")
    if rg:
        command = [rg, "--json", "--line-number", "--no-heading"]
        if literal:
            command.append("--fixed-strings")
        if not case_sensitive:
            command.append("--ignore-case")
        for item in include:
            command.extend(["--glob", str(item)])
        for ignored in sorted(_IGNORED_DIRS):
            command.extend(["--glob", f"!{ignored}/**"])
        command.extend([pattern, str(root)])
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=30,
                creationflags=_hidden_window_flags(),
            )
            if completed.returncode not in (0, 1):
                return {"error": "SEARCH_ERROR", "detail": completed.stderr[:1000]}
            matches = []
            for line in completed.stdout.splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("type") != "match":
                    continue
                data = item.get("data", {})
                absolute = Path(data.get("path", {}).get("text", ""))
                matches.append({
                    "file": _rel(workspace, absolute),
                    "line": int(data.get("line_number", 0)),
                    "text": data.get("lines", {}).get("text", "").rstrip("\r\n")[:500],
                })
                if len(matches) >= max_results:
                    break
            return {"matches": matches, "count": len(matches), "truncated": len(matches) >= max_results}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"error": "SEARCH_ERROR", "detail": str(exc)}
    return _search_text_fallback(workspace, root, pattern, literal, case_sensitive, include, max_results)


def edit_file(args: dict) -> dict:
    workspace = _workspace(args)
    path = _confined_path(workspace, args.get("path", ""), must_exist=True,
                          allowed_roots=_allowed_roots(args))
    old = str(args.get("old_string", ""))
    new = str(args.get("new_string", ""))
    if not old:
        return {"error": "MISSING_OLD_STRING"}
    try:
        raw = path.read_bytes()
        current_hash = hashlib.sha256(raw).hexdigest()
        expected = str(args.get("expected_sha256", ""))
        if expected and expected != current_hash:
            return {
                "error": "FILE_CHANGED", "path": _rel(workspace, path),
                "expected_sha256": expected, "actual_sha256": current_hash,
            }
        content = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": "READ_ERROR", "detail": str(exc)}
    count = content.count(old)
    replace_all = bool(args.get("replace_all", False))
    if count == 0:
        from backend.core.errors import error_payload
        relative = _rel(workspace, path)
        return {
            **error_payload(
                "STRING_NOT_FOUND",
                details={"path": relative, "actual_sha256": current_hash},
                next_actions=[
                    {
                        "action": "read_file",
                        "arguments": {"path": relative},
                        "effect": "Refresh the exact current content and SHA-256.",
                    },
                    {
                        "action": "edit_file",
                        "effect": "Retry with the smallest current unique old_string; do not resend the whole file as the anchor.",
                    },
                    {
                        "action": "write_file",
                        "arguments": {
                            "path": relative,
                            "create_only": False,
                            "expected_sha256": current_hash,
                        },
                        "effect": "Use only when a full-file replacement is genuinely intended; include the complete content.",
                    },
                ],
            ),
            "path": relative,
            "actual_sha256": current_hash,
        }
    if count > 1 and not replace_all:
        return {
            "error": "STRING_NOT_UNIQUE", "path": _rel(workspace, path),
            "occurrences": count,
        }
    updated = content.replace(old, new, -1 if replace_all else 1)
    _atomic_write(path, updated.encode("utf-8"))
    updated_raw = path.read_bytes()
    relative = _rel(workspace, path)
    return {
        "path": relative,
        "replacements": count if replace_all else 1,
        "before_sha256": current_hash,
        "after_sha256": hashlib.sha256(updated_raw).hexdigest(),
        "size": len(updated_raw),
        "diff": _text_diff(relative, content, updated),
    }


def write_file(args: dict) -> dict:
    workspace = _workspace(args)
    path = _confined_path(workspace, args.get("path", ""), must_exist=False,
                          allowed_roots=_allowed_roots(args))
    content = str(args.get("content", ""))
    create_only = bool(args.get("create_only", True))
    expected = str(args.get("expected_sha256", ""))
    existed = path.exists()
    before_hash = ""
    before_content = ""
    if existed:
        before_raw = path.read_bytes()
        before_hash = hashlib.sha256(before_raw).hexdigest()
        before_content = before_raw.decode("utf-8", errors="replace")
        if create_only:
            from backend.core.errors import error_payload
            relative = _rel(workspace, path)
            return {
                **error_payload(
                    "FILE_EXISTS",
                    details={"path": relative, "actual_sha256": before_hash},
                    next_actions=[{
                        "action": "write_file",
                        "arguments": {
                            "path": relative,
                            "create_only": False,
                            "expected_sha256": before_hash,
                        },
                        "effect": "Replace this exact file version with the complete intended content. Do not delete it first.",
                    }],
                ),
                "path": relative,
                "actual_sha256": before_hash,
            }
        if not expected:
            from backend.core.errors import error_payload
            relative = _rel(workspace, path)
            return {
                **error_payload(
                    "EXPECTED_HASH_REQUIRED",
                    details={"path": relative, "actual_sha256": before_hash},
                    next_actions=[{
                        "action": "write_file",
                        "arguments": {
                            "path": relative,
                            "create_only": False,
                            "expected_sha256": before_hash,
                        },
                        "effect": "Retry the replacement against this exact file version.",
                    }],
                ),
                "path": relative,
                "actual_sha256": before_hash,
            }
        if expected != before_hash:
            from backend.core.errors import error_payload
            relative = _rel(workspace, path)
            return {
                **error_payload(
                    "FILE_CHANGED",
                    details={
                        "path": relative,
                        "expected_sha256": expected,
                        "actual_sha256": before_hash,
                    },
                    next_actions=[{
                        "action": "read_file",
                        "arguments": {"path": relative},
                        "effect": "Refresh content before recomputing the replacement.",
                    }],
                ),
                "path": relative,
                "expected_sha256": expected,
                "actual_sha256": before_hash,
            }
    _atomic_write(path, content.encode("utf-8"))
    after = path.read_bytes()
    relative = _rel(workspace, path)
    return {
        "path": relative,
        "action": "updated" if existed else "created",
        "before_sha256": before_hash,
        "after_sha256": hashlib.sha256(after).hexdigest(),
        "size": len(after),
        "diff": _text_diff(relative, before_content, content),
    }


def delete_file(args: dict) -> dict:
    """Recoverably remove one version-pinned workspace file.

    Deletion is deliberately a first-class operation instead of a shell
    convention.  The file is moved under ``.gitgo/trash`` so a later undo or
    explicit restore can recover it, while the user-visible workspace no
    longer contains the artifact.
    """
    workspace = _workspace(args)
    path = _confined_path(workspace, args.get("path", ""), must_exist=True)
    relative = _rel(workspace, path)
    relative_path = Path(relative)
    if not relative_path.parts or relative_path.parts[0].casefold() == ".gitgo":
        return {"error": "PROTECTED_PATH", "path": relative}
    if path.is_dir():
        return {"error": "IS_DIRECTORY", "path": relative}
    raw = path.read_bytes()
    current_hash = hashlib.sha256(raw).hexdigest()
    expected = str(args.get("expected_sha256", "")).strip()
    if not expected:
        return {
            "error": "EXPECTED_HASH_REQUIRED", "path": relative,
            "actual_sha256": current_hash,
        }
    if expected != current_hash:
        return {
            "error": "FILE_CHANGED", "path": relative,
            "expected_sha256": expected, "actual_sha256": current_hash,
        }
    trash_id = f"{time.time_ns()}-{current_hash[:12]}"
    trash_path = workspace / ".gitgo" / "trash" / "files" / trash_id / relative_path
    trash_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(trash_path))
    before_text = raw.decode("utf-8", errors="replace")
    return {
        "path": relative,
        "action": "deleted",
        "recoverable": True,
        "trash_ref": _rel(workspace, trash_path),
        "before_sha256": current_hash,
        "size": len(raw),
        "diff": _text_diff(relative, before_text, ""),
    }


def apply_patch(args: dict) -> dict:
    workspace = _workspace(args)
    patch = str(args.get("patch", ""))
    if not patch.strip():
        return {"error": "EMPTY_PATCH"}
    touched = _patch_paths(patch)
    if not touched:
        return {"error": "INVALID_PATCH", "detail": "no file headers found"}
    for rel in touched:
        _confined_path(workspace, rel, must_exist=False,
                       allowed_roots=_allowed_roots(args))
    command = ["git", "apply", "--whitespace=nowarn"]
    try:
        check = subprocess.run(
            command + ["--check", "-"], cwd=str(workspace), input=patch,
            capture_output=True, text=True, timeout=30,
            creationflags=_hidden_window_flags(),
        )
        if check.returncode != 0:
            from backend.core.errors import error_payload
            diagnostic = check.stderr[:4000]
            return {
                **error_payload(
                    "PATCH_CHECK_FAILED",
                    message=(
                        "The unified diff failed preflight validation; no file was changed. "
                        + diagnostic
                    ),
                    details={"files": touched, "stderr": diagnostic},
                    next_actions=[
                        {
                            "action": "repair_unified_diff",
                            "effect": "Every fragment needs ---/+++ file headers and a valid @@ -old,+new @@ hunk header.",
                        },
                        {
                            "action": "edit_file",
                            "effect": "For a small change, reread the file and use bounded exact-string edits instead of resending a large patch.",
                        },
                    ],
                ),
                "files": touched,
            }
        applied = subprocess.run(
            command + ["-"], cwd=str(workspace), input=patch,
            capture_output=True, text=True, timeout=30,
            creationflags=_hidden_window_flags(),
        )
        if applied.returncode != 0:
            return {"error": "PATCH_APPLY_FAILED", "detail": applied.stderr[:4000], "files": touched}
    except FileNotFoundError:
        return {"error": "GIT_NOT_FOUND"}
    except subprocess.TimeoutExpired:
        return {"error": "PATCH_TIMEOUT"}
    return {
        "files": touched, "count": len(touched), "applied": True,
        "diff": patch[:100_000] + ("\n[diff truncated by Host]\n" if len(patch) > 100_000 else ""),
    }


def exec_command(args: dict) -> dict:
    from backend.core.process_control import attach_kill_job, close_job, creation_flags
    workspace = _workspace(args)
    cwd = _confined_path(workspace, args.get("cwd", "."), must_exist=True,
                         allowed_roots=_allowed_roots(args))
    if not cwd.is_dir():
        return {"error": "NOT_A_DIRECTORY", "cwd": _rel(workspace, cwd)}
    argv = args.get("argv")
    command = args.get("command")
    if argv is None and isinstance(command, str):
        if bool(args.get("shell", False)) and not bool(args.get("_allow_shell", False)):
            return {"error": "SHELL_MODE_NOT_AUTHORIZED"}
        argv = shlex.split(command, posix=sys.platform != "win32")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        return {"error": "ARGV_REQUIRED"}
    denial = _command_denial(argv, workspace)
    if denial:
        return {"error": "COMMAND_DENIED", "detail": denial, "argv": argv}
    if len(argv) == 1 and argv[0].casefold() == "pwd":
        relative_cwd = _rel(workspace, cwd) or "."
        return {
            "argv": argv,
            "cwd": relative_cwd,
            "stdout": relative_cwd + "\n",
            "stderr": "",
            "exit_code": 0,
            "success": True,
            "truncated": False,
            "host_shortcut": "workspace_relative_cwd",
        }
    # Agent commands run inside the same managed runtime as Gitgo.  On Windows
    # that runtime is often portable and intentionally has no global `python`
    # on PATH.  Resolve conventional Python launcher names deterministically
    # instead of making the model discover a host-specific absolute path.
    if argv[0].casefold() in {"python", "python3", "py"}:
        argv = _managed_python_argv(argv, cwd)
    timeout = max(1, min(int(args.get("timeout", 120) or 120), 1800))
    env = os.environ.copy()
    env["GITGO_AGENT_TOOL"] = "1"
    try:
        started = subprocess.Popen(
            argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, creationflags=creation_flags(),
            start_new_session=sys.platform != "win32",
        )
    except (OSError, ValueError) as exc:
        return {"error": "COMMAND_ERROR", "detail": str(exc), "argv": argv}
    job_handle = attach_kill_job(started)
    try:
        stdout, stderr = started.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(started, job_handle)
        return {"error": "COMMAND_TIMEOUT", "timeout": timeout, "argv": argv}
    except (OSError, ValueError) as exc:
        _kill_tree(started, job_handle)
        return {"error": "COMMAND_ERROR", "detail": str(exc), "argv": argv}
    close_job(job_handle)
    stdout = stdout[-_MAX_OUTPUT:]
    stderr = stderr[-_MAX_OUTPUT:]
    return {
        "argv": argv,
        "cwd": _rel(workspace, cwd),
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": started.returncode,
        "success": started.returncode == 0,
        "truncated": len(stdout) >= _MAX_OUTPUT or len(stderr) >= _MAX_OUTPUT,
    }


def _managed_python_argv(argv: list[str], cwd: Path) -> list[str]:
    """Make managed/embedded Python behave like a normal project interpreter.

    The Windows embeddable distribution uses ``pythonXY._pth`` and therefore
    starts with ``safe_path=True``: neither a script's directory nor the current
    working directory is importable.  Agents used to burn many model turns
    diagnosing ``ModuleNotFoundError`` for a sibling test module.  The Host
    knows the workspace cwd exactly, so it supplies that deterministic runtime
    fact while preserving argv semantics and avoiding a shell.
    """
    launcher = str(argv[0]).casefold()
    remaining = [str(item) for item in argv[1:]]
    if (
        launcher == "py"
        and remaining
        and re.fullmatch(r"-\d+(?:\.\d+)?", remaining[0])
    ):
        remaining = remaining[1:]

    interpreter_flags: list[str] = []
    while remaining:
        item = remaining[0]
        if item == "-X" and len(remaining) >= 2:
            interpreter_flags.extend(remaining[:2])
            remaining = remaining[2:]
            continue
        if item in {"-B", "-E", "-I", "-O", "-OO", "-s", "-S", "-u"}:
            interpreter_flags.append(item)
            remaining = remaining[1:]
            continue
        break

    from backend.core.child_process import python_command

    if not remaining:
        return python_command(interpreter_flags)

    bootstrap = f"import sys;sys.path.insert(0,{str(cwd)!r})\n"
    mode = remaining[0]
    if mode == "-c" and len(remaining) >= 2:
        return python_command([
            *interpreter_flags, "-c", bootstrap + remaining[1], *remaining[2:],
        ])
    if mode == "-m" and len(remaining) >= 2:
        module = remaining[1]
        module_args = remaining[2:]
        code = (
            bootstrap
            + "import runpy;"
            + f"sys.argv={[module, *module_args]!r};"
            + f"runpy.run_module({module!r},run_name='__main__',alter_sys=True)"
        )
        return python_command([*interpreter_flags, "-c", code])
    if not mode.startswith("-"):
        script = str((cwd / mode).resolve(strict=False)) if not Path(mode).is_absolute() else mode
        script_args = remaining[1:]
        code = (
            bootstrap
            + "import runpy;"
            + f"sys.argv={[script, *script_args]!r};"
            + f"runpy.run_path({script!r},run_name='__main__')"
        )
        return python_command([*interpreter_flags, "-c", code])
    return python_command([*interpreter_flags, *remaining])


def shell_script(args: dict) -> dict:
    """Execute one approved native shell program in the workspace.

    Approval is enforced by ToolPipeline before this isolated handler starts.
    The runner still confines cwd, strips likely credentials from the inherited
    environment, bounds input/output/time, and owns the spawned process tree.
    ProcessToolRunner applies native OS isolation before loading this handler;
    the exact script remains the unit the user reviewed and approved.
    """
    from backend.core.process_control import attach_kill_job, close_job, creation_flags

    workspace = _workspace(args)
    cwd = _confined_path(
        workspace, args.get("cwd", "."), must_exist=True,
        allowed_roots=_allowed_roots(args),
    )
    if not cwd.is_dir():
        return {"error": "NOT_A_DIRECTORY", "cwd": _rel(workspace, cwd)}
    script = str(args.get("script") or "")
    purpose = str(args.get("purpose") or "").strip()
    if not script.strip():
        return {"error": "SCRIPT_REQUIRED"}
    if not purpose:
        return {"error": "PURPOSE_REQUIRED"}
    if "\x00" in script or len(script.encode("utf-8")) > 100_000:
        return {"error": "SCRIPT_INVALID", "detail": "script exceeds the 100KB boundary or contains NUL"}
    if sys.platform == "win32":
        # MSYS Bash requires a shared global object namespace, incompatible
        # with AppContainer. Use a Windows-native engine without weakening it.
        engine = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        # Server images may not autoload even the built-in modules in an
        # AppContainer. Load known system manifests before executing the script.
        bootstrap = "$env:PSModulePath=$PSHOME+'\\Modules';"
        for module in ("Microsoft.PowerShell.Utility", "Microsoft.PowerShell.Management"):
            bootstrap += (f"Import-Module ($PSHOME+'\\Modules\\{module}\\{module}.psd1') "
                          "-ErrorAction Stop;")
        # PowerShell's provider location can fall back to the drive root in
        # AppContainer even when CreateProcess has the correct native cwd.
        literal_cwd = str(cwd).replace("'", "''")
        bootstrap += (f"$null=New-PSDrive -Name Gitgo -PSProvider FileSystem "
                      f"-Root '{literal_cwd}' -ErrorAction Stop;"
                      "Set-Location -LiteralPath 'Gitgo:\\' -ErrorAction Stop;")
        command = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
                   "try {" + bootstrap + "} catch { [Console]::Error.WriteLine($_);exit 1 };\n" + script)
        shell_argv = [str(engine), "-NoLogo", "-NoProfile", "-NonInteractive",
                      "-Command", command]
    else:
        bash = _find_bash()
        if bash is None:
            return {"error": "BASH_UNAVAILABLE", "detail": "Install Bash in the sandbox runtime."}
        shell_argv = [str(bash), "--noprofile", "--norc", "-c", script]
    timeout = max(1, min(int(args.get("timeout", 120) or 120), 1800))
    env = _safe_shell_environment()
    if sys.platform == "win32":
        # Do not discover modules through the real user's registry/profile or
        # inherit PowerShell 7 module paths into the Windows PowerShell engine.
        env["PSModulePath"] = str(engine.parent / "Modules")
    env["GITGO_AGENT_TOOL"] = "1"
    started = None
    job_handle = None
    try:
        started = subprocess.Popen(
            shell_argv,
            cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=env,
            creationflags=creation_flags(), start_new_session=sys.platform != "win32",
        )
        job_handle = attach_kill_job(started)
        stdout, stderr = started.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if started is not None:
            _kill_tree(started, job_handle)
            job_handle = None
        return {"error": "SHELL_TIMEOUT", "timeout": timeout}
    except (OSError, ValueError) as exc:
        if started is not None:
            _kill_tree(started, job_handle)
            job_handle = None
        return {"error": "SHELL_ERROR", "detail": str(exc)}
    finally:
        close_job(job_handle)
    stdout_truncated = len(stdout) > _MAX_OUTPUT
    stderr_truncated = len(stderr) > _MAX_OUTPUT
    return {
        "cwd": _rel(workspace, cwd),
        "purpose": purpose,
        "stdout": stdout[-_MAX_OUTPUT:],
        "stderr": stderr[-_MAX_OUTPUT:],
        "exit_code": started.returncode,
        "success": started.returncode == 0,
        "truncated": stdout_truncated or stderr_truncated,
    }


def _find_bash() -> Path | None:
    configured = str(os.environ.get("GITGO_BASH_PATH") or "").strip()
    if configured:
        candidate = Path(configured).resolve(strict=False)
        if candidate.is_file():
            return candidate
    if sys.platform == "win32":
        candidates: list[Path] = []
        git = shutil.which("git")
        if git:
            git_path = Path(git).resolve(strict=False)
            candidates.extend([
                git_path.parent.parent / "bin" / "bash.exe",
                git_path.parent.parent / "usr" / "bin" / "bash.exe",
            ])
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
            base = str(os.environ.get(variable) or "").strip()
            if not base:
                continue
            root = Path(base)
            candidates.extend([
                root / "Git" / "bin" / "bash.exe",
                root / "Programs" / "Git" / "bin" / "bash.exe",
            ])
        return next((candidate for candidate in candidates if candidate.is_file()), None)
    resolved = shutil.which("bash")
    return Path(resolved).resolve(strict=False) if resolved else None


def _safe_shell_environment() -> dict[str, str]:
    sensitive_fragments = (
        "API_KEY", "APIKEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD",
        "CREDENTIAL", "AUTHORIZATION", "PRIVATE_KEY", "ACCESS_KEY",
        "BEARER", "COOKIE",
    )
    return {
        key: value for key, value in os.environ.items()
        if not any(fragment in key.upper() for fragment in sensitive_fragments)
    }


def dependency_feedback(args: dict) -> dict:
    from backend.core.dependency_graph import record_dependency_feedback

    workspace = _workspace(args)
    return record_dependency_feedback(
        workspace,
        dependent=str(args.get("dependent", "")),
        dependency=str(args.get("dependency", "")),
        confirmed=bool(args.get("confirmed", True)),
        reason=str(args.get("reason", "")),
        source=str(args.get("source", "agent")),
    )


def rebuild_dependency_graph(args: dict) -> dict:
    from backend.core.dependency_graph import build_dependency_graph

    graph = build_dependency_graph(_workspace(args))
    active = sum(1 for edge in graph.edges.values() if not edge.dismissed)
    return {
        "version": 2,
        "files": len(graph.file_fingerprints),
        "edges": active,
        "dismissed_edges": len(graph.edges) - active,
    }


def _search_text_fallback(workspace, root, pattern, literal, case_sensitive, include, limit):
    flags = 0 if case_sensitive else re.I
    try:
        regex = re.compile(re.escape(pattern) if literal else pattern, flags)
    except re.error as exc:
        return {"error": "INVALID_REGEX", "detail": str(exc)}
    matches = []
    candidates = [root] if root.is_file() else root.rglob("*")
    for path in candidates:
        try:
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                continue
            parts = path.relative_to(root).parts
            if any(part in _IGNORED_DIRS for part in parts):
                continue
            if include and not any(path.match(item) for item in include):
                continue
            for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    matches.append({"file": _rel(workspace, path), "line": line_no, "text": line[:500]})
                    if len(matches) >= limit:
                        return {"matches": matches, "count": len(matches), "truncated": True}
        except (OSError, ValueError):
            continue
    return {"matches": matches, "count": len(matches), "truncated": False}


def _workspace(args: dict) -> Path:
    raw = str(args.get("_workspace", "") or Path.cwd())
    workspace = Path(raw).resolve(strict=False)
    if not workspace.exists() or not workspace.is_dir():
        raise ValueError(f"invalid workspace: {workspace}")
    return workspace


def _allowed_roots(args: dict) -> list[Path]:
    roots = []
    for raw in list(args.get("_allowed_roots") or []):
        value = str(raw or "").strip()
        if value:
            roots.append(Path(value).resolve(strict=False))
    return roots


def _confined_path(workspace: Path, raw: str, *, must_exist: bool,
                   allowed_roots: list[Path] | None = None) -> Path:
    if not str(raw).strip():
        raise ValueError("path is required")
    path = Path(str(raw))
    if not path.is_absolute():
        path = workspace / path
    resolved = path.resolve(strict=False)
    roots = [workspace, *(allowed_roots or [])]
    if not any(_is_within(resolved, root) for root in roots):
        raise PermissionError(f"path escapes approved resource scope: {raw}")
    if must_exist and not resolved.exists():
        raise FileNotFoundError(str(resolved))
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _patch_paths(patch: str) -> list[str]:
    paths = []
    for marker, raw in re.findall(r"^(---|\+\+\+)\s+([^\t\r\n]+)", patch, re.M):
        value = raw.strip()
        if value == "/dev/null":
            continue
        if value.startswith(("a/", "b/")):
            value = value[2:]
        value = value.replace("\\", "/")
        if value not in paths:
            paths.append(value)
    return paths


def _command_denial(argv: list[str], workspace: Path) -> str:
    executable = Path(argv[0]).name.lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    if executable in _DENIED_EXECUTABLES:
        return f"{executable} is outside the workspace-development command policy"
    if executable in {"cmd", "powershell", "pwsh", "bash", "sh", "zsh", "fish"}:
        return "nested shell interpreters require a separate approved capability"
    if executable == "git" and len(argv) > 1 and argv[1].lower() in _DENIED_GIT_SUBCOMMANDS:
        return f"git {argv[1]} must use the governed git workflow"
    if executable in {"npm", "pnpm", "yarn", "pip", "pip3", "uv", "cargo", "go"}:
        lowered = {item.lower() for item in argv[1:3]}
        if lowered & {"install", "add", "publish", "upload"}:
            return "dependency/network mutation requires a separately approved tool"
    for item in argv[1:]:
        if not item or item.startswith("-"):
            continue
        try:
            candidate = Path(item)
            if candidate.is_absolute():
                candidate.resolve(strict=False).relative_to(workspace)
        except ValueError:
            return f"absolute argument escapes workspace: {item}"
    return ""


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _kill_tree(proc: subprocess.Popen, job_handle=None) -> None:
    from backend.core.process_control import terminate_tree
    terminate_tree(proc, job_handle)


def _rel(workspace: Path, path: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return str(resolved.relative_to(workspace)).replace("\\", "/")
    except ValueError:
        # Explicitly approved external resources retain their absolute identity
        # so receipts and user-visible audit details cannot confuse them with a
        # file in the current project.
        return str(resolved)


def _hidden_window_flags() -> int:
    return 0x08000000 if sys.platform == "win32" else 0
