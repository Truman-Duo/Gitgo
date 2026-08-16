"""收尾阶段 — Accept 两步确认状态机 + step_create_release。"""

from __future__ import annotations

from backend.models import IncomingChange

from backend.core.sync_session.models import SessionStage


class FinalizeMixin:
    def step_start_accept_confirm(self, change: IncomingChange):
        """第一步：显示 Bridge，等待二次确认"""
        if self.stage != SessionStage.TRIAL_REVIEWING:
            self.on_log(f"[WARN] step_start_accept_confirm 需要 TRIAL_REVIEWING 阶段，当前为 {self.stage}")
            return
        self._pending_accept = change
        self.stage = SessionStage.INCOMING_CONFIRMING
        self.on_stage_changed(self.stage)

    def step_confirm_accept(self) -> IncomingChange | None:
        """第二步：用户确认，返回待处理的 change"""
        if self.stage != SessionStage.INCOMING_CONFIRMING:
            self.on_log(f"[WARN] step_confirm_accept 需要 INCOMING_CONFIRMING 阶段，当前为 {self.stage}")
            return None
        change = self._pending_accept
        self._pending_accept = None
        self.stage = SessionStage.IDLE
        self.on_stage_changed(self.stage)
        return change

    def step_cancel_accept(self):
        """用户取消，回到 REVIEWING"""
        if self.stage != SessionStage.INCOMING_CONFIRMING:
            self.on_log(f"[WARN] step_cancel_accept 需要 INCOMING_CONFIRMING 阶段，当前为 {self.stage}")
            return
        self._pending_accept = None
        self.stage = SessionStage.TRIAL_REVIEWING
        self.on_stage_changed(self.stage)

    def step_create_release(self, tag: str = "", name: str = "",
                            body: str = "") -> tuple[bool, str]:
        """在远程仓库创建 Release（GitHub/GitLab）。

        若无显式参数，则从最新 pushed formal commit 自动生成 tag/name/body。
        返回 (success, message)。
        """
        from backend.remote import create_connector

        release_node = self.project.release
        if not release_node or not release_node.remote:
            self.on_log("未配置远程仓库")
            return False, "未配置远程仓库"

        remote = release_node.remote
        connector = create_connector(remote)
        if not connector:
            self.on_log(f"不支持的远程仓库类型: {remote.kind}")
            return False, f"不支持的远程仓库类型: {remote.kind}"

        if not connector.is_configured():
            self.on_log(f"未配置 {remote.kind} 访问令牌")
            return False, f"未配置 {remote.kind} 访问令牌"

        # 自动从最新 pushed formal commit 生成参数
        if not tag or not body:
            pushed = [fc for fc in self.formal_commits if fc.pushed]
            if pushed:
                latest = pushed[-1]
                auto_tag = f"{latest.prefix}-{latest.number}"
                if not tag:
                    tag = auto_tag
                if not name:
                    name = auto_tag
                if not body:
                    body = latest.message
            elif not tag:
                self.on_log("没有可用的 pushed formal commit，且未指定 --tag")
                return False, "缺少 tag 参数"

        self.on_log(f"创建 Release: {tag}")
        ok, msg = connector.create_release(tag, name, body)
        if ok:
            self.on_log(f"Release 已创建: {msg}")
            from backend.core.history import HistoryManager
            HistoryManager.add_operation(
                self.project.name, "release", "success",
                {"tag": tag, "name": name},
                correlation_id=self._correlation_id,
            )
        else:
            self.on_log(f"Release 创建失败: {msg}")
        return ok, msg
