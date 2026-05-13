"""SSHFileAdapter — paramiko SFTP 实现"""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
from pathlib import Path, PurePosixPath
from typing import Iterator

from backend.adapters.file_adapter import FileAdapter


class SSHFileAdapter(FileAdapter):
    """基于 paramiko SFTP 的远程文件系统适配器。

    延迟连接（首次操作时自动建立），使用完需调用 close()。
    """

    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str = "",
        key_path: str = "",
        root: str = "/",
    ):
        self._host = host
        self._port = port
        self._username = username
        self._key_path = key_path
        self._root = root.rstrip("/") or "/"
        self._ssh = None
        self._sftp = None

    # ── 连接管理 ───────────────────────────────────────────────

    def _connect(self):
        if self._sftp is not None:
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
        self._sftp = self._ssh.open_sftp()

    def close(self):
        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._ssh:
            try:
                self._ssh.close()
            except Exception:
                pass
            self._ssh = None

    def __del__(self):
        self.close()

    # ── 路径解析 ───────────────────────────────────────────────

    def _resolve(self, path: str) -> str:
        p = path.replace("\\", "/")
        if not p or p == ".":
            return self._root
        # 相对路径拼接
        return str(PurePosixPath(self._root) / p)

    # ── 文件查询 ───────────────────────────────────────────────

    def exists(self, path: str) -> bool:
        self._connect()
        try:
            self._sftp.stat(self._resolve(path))
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def is_file(self, path: str) -> bool:
        self._connect()
        try:
            st = self._sftp.stat(self._resolve(path))
            return stat_module.S_ISREG(st.st_mode)
        except OSError:
            return False

    def is_dir(self, path: str) -> bool:
        self._connect()
        try:
            st = self._sftp.stat(self._resolve(path))
            return stat_module.S_ISDIR(st.st_mode)
        except OSError:
            return False

    def is_symlink(self, path: str) -> bool:
        self._connect()
        try:
            st = self._sftp.lstat(self._resolve(path))
            return stat_module.S_ISLNK(st.st_mode)
        except OSError:
            return False

    def stat(self, path: str) -> os.stat_result:
        self._connect()
        try:
            s = self._sftp.stat(self._resolve(path))
        except FileNotFoundError as e:
            raise OSError(2, f"No such file: {path}") from e
        return os.stat_result((
            s.st_mode,
            0,     # ino
            0,     # dev
            0,     # nlink
            getattr(s, 'st_uid', 0),
            getattr(s, 'st_gid', 0),
            s.st_size,
            int(getattr(s, 'st_atime', 0)),
            int(getattr(s, 'st_mtime', 0)),
            0,     # ctime
        ))

    # ── 目录遍历 ───────────────────────────────────────────────

    def walk(self, top: str = "") -> Iterator[tuple[str, list[str], list[str]]]:
        self._connect()
        top_resolved = self._resolve(top)

        dirs: list[str] = []
        files: list[str] = []

        try:
            entries = self._sftp.listdir_attr(top_resolved)
        except OSError:
            return

        for attr in entries:
            if attr.filename in (".", ".."):
                continue
            if stat_module.S_ISDIR(attr.st_mode):
                dirs.append(attr.filename)
            else:
                files.append(attr.filename)

        # 计算相对路径（不含 root 前缀）
        rel = ""
        if top_resolved != self._root:
            rel = str(PurePosixPath(top_resolved).relative_to(self._root))

        yield rel, dirs, files

        # 递归子目录
        for d in dirs:
            sub = f"{top}/{d}" if top else d
            yield from self.walk(sub)

    # ── 文件读写 ───────────────────────────────────────────────

    def read_bytes(self, path: str) -> bytes:
        self._connect()
        with self._sftp.open(self._resolve(path), "rb") as f:
            return f.read()

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    def write_bytes(self, path: str, data: bytes) -> None:
        self._connect()
        with self._sftp.open(self._resolve(path), "wb") as f:
            f.write(data)

    def write_text(self, path: str, data: str, encoding: str = "utf-8") -> None:
        self.write_bytes(path, data.encode(encoding))

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        self._connect()
        target = self._resolve(path)
        if parents:
            parts = PurePosixPath(target).relative_to(self._root).parts
            current = self._root
            for part in parts:
                current = str(PurePosixPath(current) / part)
                try:
                    self._sftp.mkdir(current)
                except OSError:
                    if not exist_ok:
                        raise
        else:
            try:
                self._sftp.mkdir(target)
            except OSError:
                if not exist_ok:
                    raise

    def hash_file(self, path: str) -> str:
        data = self.read_bytes(path)
        return hashlib.sha256(data).hexdigest()

    def is_binary(self, path: str) -> bool:
        data = self.read_bytes(path)
        return b"\0" in data[:1024]

    def copy_within(self, src: str, dst: str) -> None:
        data = self.read_bytes(src)
        self.write_bytes(dst, data)
