"""LocalGitRunner — 本地 git 命令实现（subprocess 封装）"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

from backend.adapters.git_runner import CompletedProcess, GitRunner


class LocalGitRunner(GitRunner):
    """本地 git 适配器。

    对 subprocess.run(["git", "-C", repo, ...]) 的薄封装。
    """

    def __init__(self, repo_path: str | Path):
        # ``Path.resolve()`` calls the Windows filesystem even with
        # ``strict=False``.  For an SMB/UNC repository that means merely
        # constructing an adapter can block on DNS/network I/O or raise
        # WinError 64 while the share is temporarily offline.  Adapter
        # construction is also used by project-list/config projections, where
        # it must remain side-effect free.  Git itself is the correct boundary
        # at which reachability is tested.
        raw_path = os.fspath(repo_path)
        if os.name == "nt":
            self._repo = Path(os.path.abspath(raw_path))
        else:
            self._repo = Path(raw_path).resolve()

    def _build_cmd(self, *args: str) -> list[str]:
        return ["git", "-C", str(self._repo), *args]

    def run(
        self,
        args: Sequence[str],
        capture_output: bool = True,
        timeout: Optional[float] = None,
        text: bool = True,
        encoding: str = "utf-8",
    ) -> CompletedProcess:
        # Windows local reads/writes use the same pinned-config boundary as Agent
        # worktree plumbing. A raw fallback could execute hooks, filters, textconv,
        # fsmonitor or signature programs inside the trusted process boundary.
        from backend.core.loop.git_execution import CONTENT_READ_COMMANDS, GitBoundaryError
        if (os.name == "nt" or sys.platform == 'linux') and args:
            if args[0] not in {"init", "--help", "--version"} and not os.path.lexists(self._repo / ".git"):
                message = "Selected directory is not a Git repository"
                return CompletedProcess(self._build_cmd(*args), 128,
                                        "" if text else b"", message if text else message.encode())
            try:
                return ProtectedLocalGitRunner(self._repo).run(args, capture_output, timeout, text, encoding)
            except GitBoundaryError as exc:
                if args[0] in CONTENT_READ_COMMANDS:
                    raise
                message = str(exc)
                return CompletedProcess(self._build_cmd(*args), 126, "" if text else b"",
                                        message if text else message.encode("utf8"))
        cmd = self._build_cmd(*args)
        try:
            kwargs = dict(
                capture_output=capture_output,
                timeout=timeout,
                text=text,
                encoding=encoding,
            )
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(cmd, **kwargs)
        except subprocess.TimeoutExpired as e:
            return CompletedProcess(cmd, -1, str(e), str(e))
        return CompletedProcess(cmd, result.returncode, result.stdout, result.stderr)

    def add_all(self, timeout: float = 120.0) -> bool:
        r = self.run(["add", "-A"], timeout=timeout)
        return r.returncode == 0

    def commit(self, message: str, timeout: float = 30.0) -> tuple[bool, str]:
        r = self.run(["commit", "-m", message], timeout=timeout)
        return r.returncode == 0, r.stderr

    def rev_parse(self, ref: str = "HEAD", timeout: float = 15.0) -> Optional[str]:
        r = self.run(["rev-parse", ref], timeout=timeout)
        if r.returncode == 0:
            return r.stdout.strip()
        return None

    def log(
        self,
        fmt: str = "%H|||%s|||%b",
        since_hash: Optional[str] = None,
        reverse: bool = True,
        max_count: Optional[int] = None,
        grep: Optional[str] = None,
        timeout: float = 30.0,
    ) -> list[str]:
        args = ["log", f"--format={fmt}"]
        if reverse:
            args.append("--reverse")
        if since_hash:
            args.append(f"{since_hash}..HEAD")
        if max_count is not None:
            args.append(f"--max-count={max_count}")
        if grep is not None:
            args.append(f"--grep={grep}")
        r = self.run(args, timeout=timeout)
        if r.returncode == 0:
            return [line for line in r.stdout.splitlines() if line.strip()]
        return []

    def push(self, remote: str = "origin", timeout: float = 60.0) -> tuple[bool, str]:
        r = self.run(["push", remote], timeout=timeout)
        return r.returncode == 0, r.stderr

    def diff(
        self,
        args: Optional[Sequence[str]] = None,
        timeout: float = 15.0,
    ) -> str:
        cmd_args = ["diff", "--no-ext-diff", "--no-textconv"]
        if args:
            cmd_args.extend(args)
        r = self.run(cmd_args, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""

    def is_git_repo(self) -> bool:
        return (self._repo / ".git").exists()

    def fetch(self, remote: str = "origin", timeout: float = 60.0) -> tuple[bool, str]:
        r = self.run(["fetch", remote], timeout=timeout)
        return r.returncode == 0, r.stderr

    def cherry_pick(self, commit_hash: str, timeout: float = 30.0) -> tuple[bool, str]:
        r = self.run(["cherry-pick", commit_hash], timeout=timeout)
        return r.returncode == 0, r.stderr


class ProtectedLocalGitRunner(LocalGitRunner):
    """Trusted local plumbing using the existing fixed-configuration boundary."""
    def run(self, args, capture_output=True, timeout=None, text=True, encoding="utf-8", *, input_bytes=None, transport_identity=None):
        from backend.core.loop.git_execution import protected_git, protected_git_config
        boundary = protected_git_config if args and args[0] in {"init", "remote", "--help", "--version"} else protected_git
        with boundary(self._repo, ["git", *args], **({"transport_identity": transport_identity} if transport_identity is not None else {})) as (command, environment):
            options = {"cwd": self._repo, "env": environment, "capture_output": capture_output,
                       "timeout": timeout or 120, "text": text,
                       "close_fds": True, "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0}
            if input_bytes is not None:
                if text or not isinstance(input_bytes, bytes):
                    raise ValueError("Protected Git input requires binary mode")
                options["input"] = input_bytes
            else:
                options["stdin"] = subprocess.DEVNULL
            if text:
                options["encoding"] = encoding
            if os.name == 'posix' and hasattr(command, 'pass_fds'):
                options['pass_fds'] = command.pass_fds
            try:
                result = subprocess.run(command, **options)
            except subprocess.TimeoutExpired:
                message = "Protected Git operation timed out; no raw command was retried"
                return CompletedProcess(command, -1, "" if text else b"", message if text else message.encode())
        return CompletedProcess(command, result.returncode, result.stdout, result.stderr)
