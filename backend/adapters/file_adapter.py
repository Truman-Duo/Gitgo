"""FileAdapter ABC — 文件系统操作抽象接口"""

from __future__ import annotations

import hashlib
import os as os_module
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO, Iterator, Optional


class FileAdapter(ABC):
    """抽象文件系统操作。

    每个 adapter 实例绑定到一个根目录，所有 path 参数均相对于该根目录。
    """

    @abstractmethod
    def exists(self, path: str) -> bool:
        ...

    @abstractmethod
    def is_file(self, path: str) -> bool:
        ...

    @abstractmethod
    def is_dir(self, path: str) -> bool:
        ...

    @abstractmethod
    def is_symlink(self, path: str) -> bool:
        ...

    @abstractmethod
    def walk(self, top: str = "") -> Iterator[tuple[str, list[str], list[str]]]:
        """遍历目录树，类似 os.walk。

        每次 yield (rel_dirpath, dirnames, filenames)。
        调用方可原地修改 dirnames 来剪枝子目录。
        """
        ...

    @abstractmethod
    def read_bytes(self, path: str) -> bytes:
        ...

    @abstractmethod
    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        ...

    @abstractmethod
    def write_bytes(self, path: str, data: bytes) -> None:
        ...

    @abstractmethod
    def write_text(self, path: str, data: str, encoding: str = "utf-8") -> None:
        ...

    @abstractmethod
    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        ...

    @abstractmethod
    def hash_file(self, path: str) -> str:
        """返回 SHA-256 十六进制摘要。"""

    @abstractmethod
    def is_binary(self, path: str) -> bool:
        """读取文件前 1 KiB，检测是否包含 null 字节。"""

    @abstractmethod
    def copy_within(self, src: str, dst: str) -> None:
        """在同一个节点内复制文件。本地实现用 shutil.copy2。"""

    @abstractmethod
    def stat(self, path: str) -> os_module.stat_result:
        ...


def _hash_file(filepath: str | Path, normalize_eol: bool = False) -> str:
    """计算文件的 SHA-256 哈希（8MB 分块读取避免内存爆炸）。

    normalize_eol=True 时先将 \\r\\n 替换为 \\n 再计算哈希，
    避免 Windows/Linux 换行符差异导致误报。
    """
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(8192 * 1024)
            if not chunk:
                break
            if normalize_eol:
                chunk = chunk.replace(b"\r\n", b"\n")
            h.update(chunk)
    return h.hexdigest()


def _is_binary_file(filepath: str | Path) -> bool:
    """检测文件是否包含 null 字节（二进制特征）。"""
    with open(filepath, "rb") as f:
        chunk = f.read(1024)
        return b"\0" in chunk
