"""SSHGitRunner — paramiko exec 实现"""

from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import Optional, Sequence

from backend.adapters.git_runner import CompletedProcess, GitRunner


class SSHGitRunner(GitRunner):
    """基于 paramiko SSH 的远程 git 适配器。

    每个命令通过 SSH exec 执行，延迟连接（首次操作自动建立）。
    """

    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str = "",
        key_path: str = "",
        repo_path: str = "",
    ):
        self._host = host
        self._port = port
        self._username = username
        self._key_path = key_path
        self._repo = repo_path.rstrip("/") or "/"
        self._ssh = None

    # ── 连接管理 ───────────────────────────────────────────────

    def _connect(self):
        if self._ssh is not None:
            return
        import paramiko

        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = {}
        if self._key_path:
            kwargs["key_filename"] = self._key_path
        self._ssh.connect(
            self._host, port=self._port, username=self._username, **kwargs
        )

    def close(self):
        if self._ssh:
            try:
                self._ssh.close()
            except Exception:
                pass
            self._ssh = None

    def __del__(self):
        self.close()

    # ── 核心 ───────────────────────────────────────────────────

    def _quote(self, s: str) -> str:
        return shlex.quote(s)

    def run(
        self,
        args: Sequence[str],
        capture_output: bool = True,
        timeout: Optional[float] = None,
        text: bool = True,
        encoding: str = "utf-8",
    ) -> CompletedProcess:
        """执行远程 git 命令。自动前置 `cd repo && git`。"""
        self._connect()

        # 构建远程命令: cd /repo && git <args>
        cmd_parts = ["cd", self._quote(self._repo), "&&", "git"]
        cmd_parts.extend(self._quote(a) for a in args)
        cmd_str = " ".join(cmd_parts)

        try:
            stdin, stdout, stderr = self._ssh.exec_command(
                cmd_str,
                timeout=timeout,
            )
            # 读取完整输出
            out_bytes = stdout.read()
            err_bytes = stderr.read()
            exit_status = stdout.channel.recv_exit_status()
        except Exception as e:
            # 将 paramiko 异常转换为标准异常
            msg = str(e)
            if "timeout" in msg.lower():
                return CompletedProcess(list(args), -1, "", f"Timeout: {msg}")
            return CompletedProcess(list(args), -1, "", f"SSH error: {msg}")

        out_str = out_bytes.decode(encoding) if text else out_bytes
        err_str = err_bytes.decode(encoding) if text else err_bytes
        return CompletedProcess(list(args), exit_status, out_str, err_str)

    # ── 高层方法 ───────────────────────────────────────────────

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

    def fetch(self, remote: str = "origin", timeout: float = 60.0) -> tuple[bool, str]:
        r = self.run(["fetch", remote], timeout=timeout)
        return r.returncode == 0, r.stderr

    def cherry_pick(self, commit_hash: str, timeout: float = 30.0) -> tuple[bool, str]:
        r = self.run(["cherry-pick", commit_hash], timeout=timeout)
        return r.returncode == 0, r.stderr

    def is_git_repo(self) -> bool:
        # 远程检查 .git 目录
        self._connect()
        try:
            import paramiko
            sftp = self._ssh.open_sftp()
            try:
                sftp.stat(f"{self._repo}/.git")
                return True
            except FileNotFoundError:
                return False
            except OSError:
                return False
            finally:
                sftp.close()
        except Exception:
            return False
