"""自动合并推荐插件 — 按 type 分组推荐合并连续的同类型 commit。"""

from backend.core.plugin import SyncPlugin


class AutoMergePlugin(SyncPlugin):
    name = "auto-merge"
    version = "0.1.0"

    def on_commit_select(self, commits: list[dict]) -> list[int] | None:
        """推荐合并连续的同类型 commit。

        策略：按 type 分组，对每组只保留第一个 commit 的索引（表示推荐合并）。
        例如 feat→feat→fix→fix  → 推荐索引 [0, 2]。
        """
        if not commits:
            return None

        # 找出连续同类型分组中的首 commit 索引
        recommended: list[int] = []
        prev_type = None
        for i, c in enumerate(commits):
            ct = c.get("type", "chore")
            if ct != prev_type:
                recommended.append(i)
                prev_type = ct

        # 如果本来就全选或索引基本覆盖全部，不干预
        if len(recommended) >= len(commits) * 0.8:
            return None

        return recommended


plugin_class = AutoMergePlugin
