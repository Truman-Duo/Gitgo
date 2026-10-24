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
import subprocess
import sys
import tempfile
from pathlib import Path
from .file_operations import metadata_mutation_error


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
    try:
        workspace, path, root, relative = _read_target(args)
    except FileNotFoundError:
        return _read_failure("FILE_NOT_FOUND", str(args.get("path", "")))
    label = str(path.relative_to(workspace)).replace("\\", "/") if path.is_relative_to(workspace) else str(path)
    offset = max(0, int(args.get("offset", 0) or 0))
    limit = max(1, min(int(args.get("limit", 2000) or 2000), 5000))
    try:
        # Reading the workspace itself is a directory request. Validate that
        # acquired root directly: opening its parent is unnecessary and may
        # be correctly denied by the native sandbox's workspace-only access.
        if path == workspace:
            from backend.core.sandbox_read import ReadRoot
            with ReadRoot(workspace):
                return _read_failure("IS_DIRECTORY", label)
        from backend.core.sandbox_read_plan import open_planned_read
        with open_planned_read(root, relative) as opened:
            raw = opened.read_bytes()
    except FileNotFoundError:
        return _read_failure("FILE_NOT_FOUND", label)
    except IsADirectoryError:
        return _read_failure("IS_DIRECTORY", label)
    except OSError as exc:
        return _read_failure("READ_ERROR", label, detail=str(exc))
    if b"\x00" in raw[:8192]:
        return _read_failure("BINARY_FILE", label)
    text = raw.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    selected = lines[offset:offset + limit]
    result = {
        "path": label,
        "content": "\n".join(selected),
        "offset": offset,
        "returned_lines": len(selected),
        "total_lines": len(lines),
        "truncated": offset + limit < len(lines),
        "next_offset": offset + len(selected) if offset + limit < len(lines) else None,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }
    if args.get("bind_dependency") is True:
        try:
            relative = path.resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            return _read_failure("VALIDATION_INPUT_CHANGED", str(path), detail="Only workspace inputs can be pinned for promotion")
        result["input_dependency"] = {"path": relative, "sha256": result["sha256"]}
    if "_lesson_read_inventory" in args:
        inventory = args["_lesson_read_inventory"]
        result["lesson_recall"] = {key: inventory[key] for key in ("state", "error_type") if key in inventory}
    from backend.core.knowledge.prewrite import read_advisories
    try:
        result["lesson_advisories"] = read_advisories(workspace, path, raw.decode("utf-8", errors="replace"),
                                                     args, read_range=(offset, len(selected)))
    except Exception as exc:
        result["warnings"] = [{"code": "LESSON_RECALL_UNAVAILABLE",
                               "message": f"File read succeeded; lesson recall unavailable: {exc}"}]
    return result


def _read_failure(code: str, path: str, *, detail: str = "") -> dict:
    """Expected target failures are tool results, not runner failures."""
    from backend.core.errors import error_payload
    return {**error_payload(code, details={"path": path, **({"detail": detail} if detail else {})}),
            "path": path, "effect_state": "not_committed", "execution_state": "completed"}


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
    from .workspace_search import search
    return search(args, workspace, root, _IGNORED_DIRS, listing=True)


def search_text(args: dict) -> dict:
    workspace = _workspace(args)
    root = _confined_path(workspace, args.get("path", "."), must_exist=True,
                          allowed_roots=_allowed_roots(args))
    from .workspace_search import search
    return search(args, workspace, root, _IGNORED_DIRS)


def edit_file(args: dict) -> dict:
    from .file_mutation import commit_file_mutation
    candidate = project_file_mutation("edit_file", args)
    if isinstance(candidate, dict):
        return {**candidate, "effect_state": "not_committed", "execution_state": "not_started"}
    return commit_file_mutation(candidate, args)


def write_file(args: dict) -> dict:
    from .file_mutation import commit_file_mutation
    candidate = project_file_mutation("write_file", args)
    if isinstance(candidate, dict):
        return {**candidate, "effect_state": "not_committed", "execution_state": "not_started"}
    return commit_file_mutation(candidate, args)


def project_file_mutation(operation: str, args: dict):
    """Build a complete candidate without changing target files or directories."""
    if operation == "edit_file":
        return _project_edit(args)
    if operation == "write_file":
        return _project_write(args)
    if operation == "delete_file":
        return _project_delete(args)
    raise ValueError("unsupported file mutation")


def _project_edit(args: dict):
    workspace = _workspace(args)
    denied = metadata_mutation_error(workspace, args.get("path", ""))
    if denied:
        return denied
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
    from .file_mutation import FileMutation
    return FileMutation("edit_file", workspace, path, current_hash, content, updated,
                        count if replace_all else 1)


def _project_write(args: dict):
    workspace = _workspace(args)
    denied = metadata_mutation_error(workspace, args.get("path", ""))
    if denied:
        return denied
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
    from .file_mutation import FileMutation
    return FileMutation("write_file", workspace, path, before_hash, before_content, content)


def delete_file(args: dict) -> dict:
    """Commit a version-pinned deletion through the shared file service."""
    from .file_mutation import commit_file_mutation
    candidate = project_file_mutation("delete_file", args)
    if isinstance(candidate, dict):
        return {**candidate, "effect_state": "not_committed", "execution_state": "not_started"}
    return commit_file_mutation(candidate, args)


def _project_delete(args: dict):
    """Freeze the deletion input; projection never creates trash or locks."""
    workspace = _workspace(args)
    denied = metadata_mutation_error(workspace, args.get("path", ""))
    if denied:
        return denied
    path = _confined_path(workspace, args.get("path", ""), must_exist=False)
    relative = _rel(workspace, path)
    relative_path = Path(relative)
    if not relative_path.parts:
        return {"error": "PROTECTED_PATH", "path": relative}
    if path.is_dir():
        return {"error": "IS_DIRECTORY", "path": relative}
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"error": "FILE_NOT_FOUND", "path": relative}
    except OSError as exc:
        return {"error": "READ_ERROR", "path": relative, "detail": str(exc)}
    current_hash = hashlib.sha256(raw).hexdigest()
    expected = str(args.get("expected_sha256", "")).strip()
    if not expected:
        return {"error": "EXPECTED_HASH_REQUIRED", "path": relative, "actual_sha256": current_hash}
    if expected != current_hash:
        return {"error": "FILE_CHANGED", "path": relative,
                "expected_sha256": expected, "actual_sha256": current_hash}
    from .file_mutation import FileMutation
    return FileMutation("delete_file", workspace, path, current_hash,
                        raw.decode("utf-8", errors="replace"), "")


def apply_patch(args: dict) -> dict:
    from .patch_projection import project_patch
    from .file_mutation_batch import commit_file_batch
    candidate = project_patch(args)
    if isinstance(candidate, dict):
        return candidate
    result = commit_file_batch(candidate, args)
    patch = str(args.get("patch", ""))
    result["diff"] = patch[:100_000] + ("\n[diff truncated by Host]\n" if len(patch) > 100_000 else "")
    return result


def exec_command(args: dict) -> dict:
    from backend.core.process_control import attach_kill_job, close_job, creation_flags, spawn_managed
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
    env = _safe_shell_environment()
    env["GITGO_AGENT_TOOL"] = "1"
    try:
        started = spawn_managed(
            argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, creationflags=creation_flags(),
            start_new_session=sys.platform != "win32",
        )
    except (OSError, ValueError) as exc:
        return {"error": "COMMAND_ERROR", "detail": str(exc), "argv": argv}
    job_handle = attach_kill_job(started)
    try:
        from backend.core.process_output import collect_output
        stdout, stderr, truncated = collect_output(started, timeout=timeout, limit=_MAX_OUTPUT,
            terminate=lambda: _kill_tree(started, job_handle))
    except subprocess.TimeoutExpired:
        _kill_tree(started, job_handle)
        return {"error": "COMMAND_TIMEOUT", "timeout": timeout, "argv": argv,
                "cleanup": getattr(started, "_gitgo_cleanup", {})}
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
        "cleanup": getattr(started, "_gitgo_cleanup", {}),
        "success": started.returncode == 0,
        "truncated": truncated,
    }


def _managed_python_argv(argv: list[str], cwd: Path) -> list[str]:
    from backend.core.child_process import workspace_python_command
    return workspace_python_command(argv, cwd)


def shell_script(args: dict) -> dict:
    """Execute one approved native shell program in the workspace.

    Approval is enforced by ToolPipeline before this isolated handler starts.
    The runner still confines cwd, strips likely credentials from the inherited
    environment, bounds input/output/time, and owns the spawned process tree.
    ProcessToolRunner applies native OS isolation before loading this handler;
    the exact script remains the unit the user reviewed and approved.
    """
    from backend.core.process_control import attach_kill_job, close_job, creation_flags, spawn_managed

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
    shell = str(args.get("shell") or "auto").lower()
    if shell == "auto":
        shell = "powershell" if sys.platform == "win32" else "bash"
    if shell not in {"bash", "powershell"}:
        return {"error": "SHELL_PROFILE_INVALID", "detail": "Choose bash, powershell or auto; scripts are never translated"}
    if (sys.platform == "win32" and shell != "powershell") or (sys.platform != "win32" and shell != "bash"):
        return {"error": "SANDBOX_CAPABILITY_UNSUPPORTED", "detail": "This native sandbox supports PowerShell on Windows and Bash on Linux; select that shell explicitly and use its syntax."}
    resolution = {}
    if sys.platform == "win32":
        # MSYS Bash requires a shared global object namespace, incompatible
        # with AppContainer. Use a Windows-native engine without weakening it.
        from backend.core.executable_identity import windows_system_executable
        engine = windows_system_executable("System32/WindowsPowerShell/v1.0/powershell.exe")
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
        resolution = _resolve_bash()
        if not resolution.get("path"):
            return {"error": resolution.get("error", "BASH_UNAVAILABLE"),
                    "detail": resolution.get("detail", "Install Bash in the sandbox runtime.")}
        bash = Path(resolution["path"])
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
        if resolution.get("identity"):
            from backend.core.executable_identity import assert_identity_unchanged
            try:
                assert_identity_unchanged(resolution["identity"])
            except (OSError, ValueError) as exc:
                return {"error": "BASH_IDENTITY_UNVERIFIED", "detail": str(exc)}
        started = spawn_managed(
            shell_argv,
            cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=env,
            creationflags=creation_flags(), start_new_session=sys.platform != "win32",
        )
        job_handle = attach_kill_job(started)
        from backend.core.process_output import collect_output
        stdout, stderr, truncated = collect_output(started, timeout=timeout, limit=_MAX_OUTPUT,
            terminate=lambda: _kill_tree(started, job_handle))
    except subprocess.TimeoutExpired:
        if started is not None:
            _kill_tree(started, job_handle)
            job_handle = None
        return {"error": "SHELL_TIMEOUT", "timeout": timeout,
                "cleanup": getattr(started, "_gitgo_cleanup", {})}
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
        "shell": shell,
        "cleanup": getattr(started, "_gitgo_cleanup", {}),
        "stdout": stdout[-_MAX_OUTPUT:],
        "stderr": stderr[-_MAX_OUTPUT:],
        "exit_code": started.returncode,
        "success": started.returncode == 0,
        "truncated": truncated or stdout_truncated or stderr_truncated,
    }


def _find_bash() -> Path | None:
    resolved = _resolve_bash()
    return Path(resolved["path"]) if resolved.get("path") else None


def _resolve_bash() -> dict:
    configured = str(os.environ.get("GITGO_BASH_PATH") or "").strip()
    if sys.platform == "win32":
        from backend.core.terminal_launcher import git_install_roots
        from backend.core.executable_identity import verify_git_bash
        roots = git_install_roots()
        if configured:
            candidate = Path(configured)
            if not candidate.is_absolute():
                return {"error": "BASH_IDENTITY_UNVERIFIED", "detail": "GITGO_BASH_PATH must be absolute; no implicit fallback"}
            candidate = candidate.resolve(strict=False)
            root = candidate.parent.parent
            if candidate.parent.name.lower() == "bin" and root.name.lower() == "usr":
                root = root.parent
            roots = [root]
        failures = []
        for root in roots:
            if not configured and not (root / "git-bash.exe").exists():
                continue
            identity = verify_git_bash(root)
            if not identity["verified"]:
                failures.append(identity["message"])
                continue
            checked = {Path(file["path"]) for file in identity["files"]}
            if configured and candidate not in checked - {(root / "git-bash.exe").resolve()}:
                return {"error": "BASH_IDENTITY_UNVERIFIED", "detail": "Configured Bash is not a verified engine/wrapper; no implicit fallback"}
            return {"path": str(root / "bin/bash.exe"), "identity": identity}
        return {"error": "BASH_IDENTITY_UNVERIFIED" if failures or configured else "BASH_UNAVAILABLE",
                "detail": "; ".join(failures) or "No verified Git for Windows Bash found; no implicit WSL or project executable fallback"}
    from backend.core.tools.execution_profile import verify_posix_bash
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            return {"error": "BASH_IDENTITY_UNVERIFIED", "detail": "Configured Bash must be absolute; no implicit fallback"}
        return verify_posix_bash(candidate)
    for candidate in (Path("/bin/bash"), Path("/usr/bin/bash")):
        if candidate.is_file():
            return verify_posix_bash(candidate)
    return {"error": "BASH_UNAVAILABLE", "detail": "No verified system Bash was found"}


def _safe_shell_environment() -> dict[str, str]:
    from backend.core.tools.execution_profile import safe_environment
    return safe_environment()


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


def _workspace(args: dict) -> Path:
    raw = str(args.get("_workspace", "") or Path.cwd())
    workspace = Path(raw).resolve(strict=False)
    if not workspace.exists() or not workspace.is_dir():
        raise ValueError(f"invalid workspace: {workspace}")
    return workspace


def _read_target(args: dict) -> tuple[Path, Path, Path, str]:
    """Select names only; ReadRoot enforces lookup using the opened objects.

    Do not resolve symlinks here and erase the evidence needed by the kernel.
    _workspace/_allowed_roots are Host inputs, never model authority. Existing
    file grants select that one name under its parent, not sibling operations.
    """
    from backend.core.sandbox_read import ReadBoundaryError, relative_parts
    workspace = Path(str(args.get("_workspace") or Path.cwd()))
    raw = args.get("path", "")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ReadBoundaryError("A file path is required")
    requested = Path(raw)
    if ".." in requested.parts or (requested.anchor and not requested.is_absolute()):
        raise ReadBoundaryError("File path escapes the read boundary")
    path = requested if requested.is_absolute() else workspace / requested
    roots = [workspace, *(Path(str(item)) for item in args.get("_allowed_roots", []))]
    for root in roots:
        if path.is_relative_to(root):
            if path == root:
                return workspace, path, root.parent, root.name
            relative = str(path.relative_to(root))
            relative_parts(relative)
            return workspace, path, root, relative
    raise ReadBoundaryError("File path escapes the approved resource scope")


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


from backend.core.filesystem import atomic_replace_bytes as _atomic_write


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
