"""LocalFileAdapter — 本地文件系统实现（Path / os.walk 封装）"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterator

from backend.adapters.file_adapter import FileAdapter, _hash_file, _is_binary_file


class LocalFileAdapter(FileAdapter):
    """本地文件系统适配器。

    对 Path / os.walk 等的薄封装，零性能损耗。
    """

    def __init__(self, root: str | Path):
        self._root = Path(root).resolve()

    # ── 路径解析 ───────────────────────────────────────────────

    def _resolve(self, path: str) -> Path:
        """将相对路径解析为绝对路径。"""
        return (self._root / path).resolve() if path else self._root

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
        walk_root = self._root / top if top else self._root
        for dirpath, dirnames, filenames in os.walk(walk_root):
            rel = os.path.relpath(dirpath, self._root).replace("\\", "/")
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
