"""Jira 关联插件 — 演示 commit selection 钩子：推荐合并引用同一 Jira issue 的 commit。

启用方式：在 commit_format.plugins 中添加 "jira-link"
"""
from __future__ import annotations

import re
from collections import defaultdict

from backend.core.plugin import SyncPlugin

# 匹配常见 Jira issue key 格式：PROJECT-1234
_JIRA_KEY_RE = re.compile(r'\b([A-Z]{2,10}-\d{1,7})\b')


def _extract_jira_keys(commits: list[dict]) -> dict[str, list[int]]:
    """提取每个 commit 的 Jira key → 该 key 出现的 commit 索引列表。"""
    key_to_indices: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(commits):
        text = f"{c.get('subject', '')}\n{c.get('body', '')}"
        keys = set(_JIRA_KEY_RE.findall(text))
        for k in keys:
            key_to_indices[k].append(i)
    return dict(key_to_indices)


class JiraLinkPlugin(SyncPlugin):
    name = "jira-link"
    version = "0.1.0"

    def on_commit_select(self, commits: list[dict]) -> list[int] | None:
        """推荐选中引用同一 Jira issue 的全部 commit。"""
        key_to_indices = _extract_jira_keys(commits)

        # 找到出现次数最多的 Jira key
        if not key_to_indices:
            return None

        best_key = max(key_to_indices, key=lambda k: len(key_to_indices[k]))
        indices = key_to_indices[best_key]

        if len(indices) < 2:
            return None  # 只有一个 commit 引用了该 key，无需合并推荐

        return indices


plugin_class = JiraLinkPlugin
