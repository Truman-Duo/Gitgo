"""插件系统基类 — 所有钩子均为可选覆盖，默认无操作。

插件接收的数据均为 JSON 兼容的 dict/list 格式，
以保证与子进程协议（Phase 0.5 后续扩展）的兼容性。
"""

from __future__ import annotations

from typing import Any


class SyncPlugin:
    """插件基类 — 所有钩子均为可选覆盖，默认无操作。

    子类需设置 name 和 version 类属性。
    每个插件文件/包必须暴露全局变量 ``plugin_class``。
    """

    name: str = ""
    version: str = ""

    # ── 扫描阶段 ──

    def on_scan_complete(self, entries: list[dict]) -> list[dict] | None:
        """扫描对比完成后调用，可过滤/排序/标注文件条目。

        - ``entries``: FileEntry 的 dict 表示
          ``{"rel_path", "status", "old_path", "workspace_hash", "backup_hash", "selected"}``
        - 返回值：替换 entries（返回空列表 = 使用原始 entries）
        - 可修改 ``selected`` 默认值
        """
        return None

    # ── Commit 整合阶段 ──

    def on_commit_select(
        self, commits: list[dict]
    ) -> list[int] | None:
        """在 commit 选择界面打开前调用，推荐选中哪些 commit。

        - ``commits``: CommitInfo 的 dict 表示
          ``{"hash", "subject", "type", "scope", "body"}``
        - 返回值：建议选中的 commit **索引**列表，None/[] = 不干预
        """
        return None

    def on_commit_message(
        self, selected: list[dict], project_config: dict
    ) -> str | None:
        """在生成正式 commit message 前调用，提供建议 message。

        - ``selected``: 已选中的 CommitInfo dict 列表
        - ``project_config``: ProjectConfig 的 dict 表示
        - 返回值：建议的 commit message 字符串；None = 走默认流程
        """
        return None

    # ── Sync 阶段 ──

    def on_sync_start(
        self, entries: list[dict], message: str
    ) -> str | None:
        """sync 复制文件到 backup **之前**调用。

        - ``message``: 本次 sync 的 commit message
        - 返回非空字符串：**中断** sync 并以该消息提示用户
        - 返回 None：放行
        """
        return None

    def on_sync_complete(self, result: dict) -> None:
        """sync 完成后调用，不论成功或失败。

        - ``result``: ``{"success": bool, "commit_hash": str, "files_count": int}``
        """
        pass

    # ── Push 阶段 ──

    def on_push_start(self) -> str | None:
        """push 到远程 **之前**调用。返回非空字符串则中断。"""
        return None

    def on_push_complete(self, result: dict) -> None:
        """push 完成后调用。

        - ``result``: ``{"success": bool, "remote": str}``
        """
        pass

    # ── Trial / Triage 阶段 ──

    def on_triage_recommend(
        self, incoming_changes: list[dict], project_config: dict
    ) -> list[dict] | None:
        """对 trial incoming changes 推荐三叉决策。

        - ``incoming_changes``: IncomingChange 的 dict 列表
          ``{"index", "hash", "message", "author", "date", "body"}``
        - ``project_config``: ProjectConfig 的 dict 表示
        - 返回值: ``[{"index": 0, "action": "accept", "reason": "..."}, ...]``
        - 返回 None = 不干预
        """
        return None
