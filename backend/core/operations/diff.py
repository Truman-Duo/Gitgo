"""diff_summary — 变更文件轻量统计摘要（供 AI context 使用）。

不含行级 diff 内容，仅返回新增/删除行数 + 顶层类名/函数名。
"""
from __future__ import annotations

import re
from typing import Optional


def get_diff_summary(commit_hash: str, git_runner,
                     parent_hash: Optional[str] = None) -> list[dict]:
    """返回指定 commit 的变更文件轻量统计摘要。

    Args:
        commit_hash: 目标 commit hash
        git_runner: GitRunner 实例（需实现 run() 方法）
        parent_hash: 父 commit（默认 commit_hash^）

    Returns:
        [{"path": "src/main.py", "added": 15, "removed": 3,
          "status": "modified", "top_level_symbols": ["ClassName", "func_name"]}]
    """
    if parent_hash is None:
        parent_hash = f"{commit_hash}^"

    # Step 1: 获取变更文件列表及状态
    files_info: list[dict] = []
    r = git_runner.run(
        ["diff-tree", "--no-commit-id", "--name-status", "-r", commit_hash]
    )
    if r.returncode != 0 or not r.stdout:
        return files_info

    for line in r.stdout.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status_code = parts[0]
        path = parts[-1]
        status_map = {"A": "new", "M": "modified", "D": "deleted",
                      "R": "renamed"}
        status = status_map.get(status_code[0], "modified")

        # Step 2: 行数统计
        added, removed = 0, 0
        stat_r = git_runner.run(
            ["diff", "--numstat", f"{parent_hash}..{commit_hash}", "--", path]
        )
        if stat_r.returncode == 0 and stat_r.stdout:
            sp = stat_r.stdout.strip().split("\t")
            if len(sp) >= 2:
                try:
                    added = int(sp[0]) if sp[0] != "-" else 0
                    removed = int(sp[1]) if sp[1] != "-" else 0
                except ValueError:
                    pass

        # Step 3: 顶层符号提取（class/def 名称，最多 10 个）
        symbols: list[str] = []
        diff_r = git_runner.run(
            ["diff", f"{parent_hash}..{commit_hash}", "--", path]
        )
        if diff_r.returncode == 0 and diff_r.stdout:
            for dl in diff_r.stdout.split("\n"):
                m = re.match(r'^[+-]\s*(?:class|def)\s+(\w+)', dl)
                if m and m.group(1) not in symbols:
                    symbols.append(m.group(1))
                    if len(symbols) >= 10:
                        break

        files_info.append({
            "path": path,
            "added": added,
            "removed": removed,
            "status": status,
            "top_level_symbols": symbols,
        })

    return files_info
