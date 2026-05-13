"""LocalGitRunner — 本地 git 命令实现（subprocess 封装）"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional, Sequence

from backend.adapters.git_runner import CompletedProcess, GitRunner


class LocalGitRunner(GitRunner):
    """本地 git 适配器。

    对 subprocess.run(["git", "-C", repo, ...]) 的薄封装。
    """

    def __init__(self, repo_path: str | Path):
        self._repo = Path(repo_path).resolve()

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
        cmd_args = ["diff"]
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
