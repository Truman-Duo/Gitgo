"""Slack 通知插件 — 演示 sync/push 完成后的通知钩子。

启用方式：在 commit_format.plugins 中添加 "slack-notify"
"""
from __future__ import annotations

from backend.core.plugin import SyncPlugin


def _notify(title: str, body: str):
    """发送通知。实际使用时替换为 Slack Webhook / HTTP 请求。"""
    print(f"[slack-notify] {title}: {body}")


class SlackNotifyPlugin(SyncPlugin):
    name = "slack-notify"
    version = "0.1.0"

    def on_sync_complete(self, result: dict) -> None:
        if result.get("success"):
            commit_hash = result.get("commit_hash", "")[:12]
            files = result.get("files_count", 0)
            _notify("Sync 完成",
                    f"commit {commit_hash}, {files} 个文件已同步到备份仓库")

    def on_push_complete(self, result: dict) -> None:
        if result.get("success"):
            remote = result.get("remote", "unknown")
            _notify("Push 完成", f"推送到 {remote} 成功")


plugin_class = SlackNotifyPlugin
