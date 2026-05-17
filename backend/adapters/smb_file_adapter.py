"""SMBFileAdapter — SMB/CIFS 文件系统适配器（UNC 路径）"""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PureWindowsPath
from typing import Iterator

from backend.adapters.file_adapter import FileAdapter, _hash_file, _is_binary_file


class SMBFileAdapter(FileAdapter):
    """SMB/CIFS 网络共享文件系统适配器。

    通过 UNC 路径（\\\\server\\share）访问远程文件。
    Windows 原生支持 UNC 路径，无需额外依赖。
    非 Windows 系统需预先挂载 SMB 共享或安装 pysmb 库。

    UNC 路径格式: \\\\host\\share\\root_path
    """

    def __init__(self, host: str, share: str, root: str = "",
                 username: str = "", port: int = 445):
        self._host = host
        self._share = share
        self._port = port
        self._username = username
        self._subroot = root.replace("/", "\\").strip("\\")
        # UNC 路径: \\host\share[\subroot]
        self._unc = PureWindowsPath(f"\\\\{host}\\{share}")
        if self._subroot:
            self._unc = self._unc / self._subroot

    # ── 连接管理 ───────────────────────────────────────────────

    def _connect(self):
        """检查 UNC 路径可访问性。

        Windows 上 UNC 路径由操作系统自动处理认证。
        如需不同凭据，用户应先通过 net use 或凭据管理器配置。
        """
        pass

    @property
    def unc_path(self) -> str:
        return str(self._unc).rstrip("\\")

    # ── 路径解析 ───────────────────────────────────────────────

    def _resolve(self, path: str) -> Path:
        cleaned = path.replace("/", "\\").strip("\\")
        if cleaned:
            return Path(str(self._unc) + "\\" + cleaned)
        return Path(str(self._unc))

    # ── 文件查询 ───────────────────────────────────────────────

    def exists(self, path: str) -> bool:
        return self._resolve(path).exists()

    def is_file(self, path: str) -> bool:
        return self._resolve(path).is_file()

    def is_dir(self, path: str) -> bool:
        return self._resolve(path).is_dir()

    def is_symlink(self, path: str) -> bool:
        return self._resolve(path).is_symlink()

    def stat(self, path: str) -> os.stat_result:
        return self._resolve(path).stat()

    # ── 目录遍历 ───────────────────────────────────────────────

    def walk(self, top: str = "") -> Iterator[tuple[str, list[str], list[str]]]:
        walk_root = self._resolve(top) if top else Path(str(self._unc))
        for dirpath, dirnames, filenames in os.walk(walk_root):
            rel = os.path.relpath(dirpath, str(self._unc)).replace("\\", "/")
            if rel == ".":
                rel = ""
            yield rel, dirnames, filenames

    # ── 文件读写 ───────────────────────────────────────────────

    def read_bytes(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self._resolve(path).read_text(encoding=encoding)

    def write_bytes(self, path: str, data: bytes) -> None:
        self._resolve(path).write_bytes(data)

    def write_text(self, path: str, data: str, encoding: str = "utf-8") -> None:
        self._resolve(path).write_text(data, encoding=encoding)

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        self._resolve(path).mkdir(parents=parents, exist_ok=exist_ok)

    def hash_file(self, path: str) -> str:
        return _hash_file(str(self._resolve(path)))

    def is_binary(self, path: str) -> bool:
        return _is_binary_file(str(self._resolve(path)))

    def copy_within(self, src: str, dst: str) -> None:
        shutil.copy2(str(self._resolve(src)), str(self._resolve(dst)))
