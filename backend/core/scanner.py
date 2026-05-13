"""FileScanner — 文件扫描与树结构构建（不依赖 Qt）"""

import os
from pathlib import Path
from typing import Optional

from backend.core.config import ProjectConfig
from backend.core import get_exclude_patterns


class FileTreeEntry:
    """文件树节点"""
    __slots__ = ("name", "rel_path", "is_dir", "children", "status")

    def __init__(self, name: str, rel_path: str, is_dir: bool = False):
        self.name = name
        self.rel_path = rel_path
        self.is_dir = is_dir
        self.children: list[FileTreeEntry] = []
        self.status: str = ""  # "new" | "modified" | "same" | "renamed" | ""


class FileScanner:
    """扫描 workspace 目录，构建文件树"""

    def __init__(self, project: ProjectConfig):
        self.project = project
        self.ws_path = Path(project.workspace_path or Path.cwd()).resolve()

    def scan_tree(self) -> list[FileTreeEntry]:
        """扫描 workspace 目录，返回文件树根节点列表"""
        exclude = get_exclude_patterns(self.project, self.ws_path)
        entries = self._walk(self.ws_path, exclude)
        return self._build_tree(entries)

    def _walk(self, root: Path, exclude: list[str]) -> list[dict]:
        """遍历目录，返回相对路径列表"""
        results = []
        try:
            for entry in os.scandir(root):
                name = entry.name
                if self._is_excluded(name, exclude):
                    continue
                rel = entry.path[len(str(self.ws_path)) + 1:].replace("\\", "/")
                if entry.is_dir(follow_symlinks=False):
                    results.append({"name": name, "rel_path": rel, "is_dir": True,
                                    "children": self._walk(Path(entry.path), exclude)})
                elif entry.is_file(follow_symlinks=False):
                    results.append({"name": name, "rel_path": rel, "is_dir": False,
                                    "children": []})
        except PermissionError:
            pass
        results.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return results

    @staticmethod
    def _is_excluded(name: str, exclude: list[str]) -> bool:
        if name.startswith("."):
            return True
        for pat in exclude:
            pat = pat.strip().rstrip("/")
            if pat.endswith("/**") and name == pat[:-3]:
                return True
            if pat.startswith("**/") and name == pat[3:]:
                return True
            if pat == name:
                return True
            if pat.endswith("/*") and name == pat[:-2]:
                return True
            if "*" in pat or "?" in pat:
                from fnmatch import fnmatch
                if fnmatch(name, pat):
                    return True
        return False

    @staticmethod
    def _build_tree(entries: list[dict]) -> list[FileTreeEntry]:
        result = []
        for e in entries:
            node = FileTreeEntry(e["name"], e["rel_path"], e["is_dir"])
            node.children = FileScanner._build_tree(e.get("children", []))
            result.append(node)
        return result
