"""GitRunner ABC — git 命令抽象接口"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from typing import Optional, Sequence


class CompletedProcess:
    """轻量版 subprocess.CompletedProcess，兼容 paramiko exec 的返回。"""
    def __init__(self, args: Sequence[str], returncode: int,
                 stdout: str = "", stderr: str = ""):
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class GitRunner(ABC):
    """抽象 git 操作。

    每个 runner 实例绑定到一个仓库根目录，所有操作在该仓库上执行。
    """

    @abstractmethod
    def run(
        self,
        args: Sequence[str],
        capture_output: bool = True,
        timeout: Optional[float] = None,
        text: bool = True,
        encoding: str = "utf-8",
    ) -> CompletedProcess:
        """执行 git 命令。'git' 会自动前置。

        返回 CompletedProcess 兼容对象（含 stdout / stderr / returncode）。
        """

    @abstractmethod
    def add_all(self, timeout: float = 120.0) -> bool:
        """git add -A"""

    @abstractmethod
    def commit(self, message: str, timeout: float = 30.0) -> tuple[bool, str]:
        """git commit -m <message>，返回 (success, stderr_or_empty)"""

    @abstractmethod
    def rev_parse(self, ref: str = "HEAD", timeout: float = 15.0) -> Optional[str]:
        """git rev-parse <ref>，返回完整 SHA 或 None"""

    @abstractmethod
    def log(
        self,
        fmt: str = "%H|||%s|||%b",
        since_hash: Optional[str] = None,
        reverse: bool = True,
        max_count: Optional[int] = None,
        grep: Optional[str] = None,
        timeout: float = 30.0,
    ) -> list[str]:
        """git log，每行一条原始输出"""

    @abstractmethod
    def push(self, remote: str = "origin", timeout: float = 60.0) -> tuple[bool, str]:
        """git push <remote>，返回 (success, stderr)"""

    @abstractmethod
    def diff(
        self,
        args: Optional[Sequence[str]] = None,
        timeout: float = 15.0,
    ) -> str:
        """git diff [args...]，返回 stdout"""

    @abstractmethod
    def fetch(self, remote: str = "origin", timeout: float = 60.0) -> tuple[bool, str]:
        """git fetch <remote>，返回 (success, stderr)"""

    @abstractmethod
    def cherry_pick(self, commit_hash: str, timeout: float = 30.0) -> tuple[bool, str]:
        """git cherry-pick <commit_hash>，返回 (success, stderr)"""

    @abstractmethod
    def is_git_repo(self) -> bool:
        """检查仓库根目录下是否存在 .git"""
