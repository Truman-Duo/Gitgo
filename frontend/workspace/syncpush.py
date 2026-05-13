"""SyncPushMixin — sync / push 流程"""
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QMessageBox
from backend.core.i18n import _tr
from ..workers import SyncWorker, PushWorker


class SyncPushMixin:
    """同步 + 推送工作流"""

    # ── Sync ─────────────────────────────────────────────

    def _start_sync(self):
        if self.state.selected_formal is None:
            return

        selected_entries = [e for e in self.state.session.entries if e.selected]
        if not selected_entries:
            QMessageBox.warning(
                self,
                _tr("dialog.hint", "提示"),
                _tr("exec.no_files_selected", "没有选中任何文件，请在文件列表中勾选需要同步的文件"),
            )
            return

        self.state.progress_bar.setValue(0)
        self.state.progress_label.setText(_tr("exec.syncing", "正在同步到备份仓库..."))
        self._log(_tr("exec.syncing_log", "开始同步..."))

        self.state.sync_worker = SyncWorker(self.state.session, self.state.selected_formal)
        self.state.sync_thread = QThread()
        self.state.sync_worker.moveToThread(self.state.sync_thread)
        self.state.sync_thread.started.connect(self.state.sync_worker.run)
        self.state.sync_worker.progress.connect(self._on_sync_progress)
        self.state.sync_worker.finished.connect(self._on_sync_finished)
        self.state.sync_worker.finished.connect(self.state.sync_thread.quit)
        self.state.sync_worker.finished.connect(self.state.sync_worker.deleteLater)
        self.state.sync_thread.finished.connect(self.state.sync_thread.deleteLater)
        self.state.sync_thread.start()

    def _on_sync_progress(self, current: int, total: int, msg: str):
        if total > 0:
            self.state.progress_bar.setValue(int(current / total * 100))
        if msg:
            self.state.progress_label.setText(msg)
            self._log(msg)

    def _on_sync_finished(self, success: bool, msg: str):
        self.state.progress_bar.setValue(100 if success else 0)
        self._log(msg)

        if success and self.state.selected_formal is not None:
            self._refresh_formal_boxes()
            self.state.selected_formal = len(self.state.session.formal_commits) - 1
            self._on_formal_box_clicked(self.state.selected_formal)

            if any(fc.synced for fc in self.state.session.formal_commits):
                self._refresh_button_states()
            self.state.progress_label.setText(_tr("exec.sync_success", "同步成功！现在可以 Push 到 GitHub"))
            QMessageBox.information(
                self,
                _tr("exec.sync_success_title", "同步成功"),
                _tr("exec.sync_success_msg", "同步完成！\n现在可以点击「Push 到 GitHub」推送远程。"),
            )
        else:
            self.state.progress_label.setText(_tr("exec.sync_failed", "同步失败，请检查日志"))
            self._refresh_button_states()
            QMessageBox.critical(
                self,
                _tr("exec.sync_failed_title", "同步失败"),
                _tr("exec.sync_failed_msg", "同步过程中出现错误，请检查日志"),
            )

    # ── Push ─────────────────────────────────────────────

    def _start_push(self):
        target = None
        for i, fc in enumerate(self.state.session.formal_commits):
            if fc.synced and not fc.pushed:
                target = i
                break
        if target is None:
            QMessageBox.information(
                self,
                _tr("dialog.hint", "提示"),
                _tr("exec.no_pending_push", "没有待 push 的正式 commit"),
            )
            return

        self.state.progress_bar.setValue(0)
        self.state.progress_label.setText(_tr("exec.pushing", "正在 push 到远程..."))
        self._log(_tr("exec.pushing_log", "开始 push..."))

        self.state.push_worker = PushWorker(self.state.session)
        self.state.push_thread = QThread()
        self.state.push_worker.moveToThread(self.state.push_thread)
        self.state.push_thread.started.connect(self.state.push_worker.run)
        self.state.push_worker.progress.connect(self._on_push_progress)
        self.state.push_worker.security_warning.connect(self._on_push_security_warning)
        self.state.push_worker.finished.connect(self._on_push_finished)
        self.state.push_worker.finished.connect(self.state.push_thread.quit)
        self.state.push_worker.finished.connect(self.state.push_worker.deleteLater)
        self.state.push_thread.finished.connect(self.state.push_thread.deleteLater)
        self.state.push_thread.start()

    def _on_push_security_warning(self, warnings: list[dict]):
        self.state.push_thread.quit()
        self.state.push_worker.deleteLater()
        self.state.push_thread.deleteLater()

        lines = [_tr("security.sensitive_found", "发现以下敏感信息："), ""]
        for w in warnings:
            severity_icon = {"critical": "🔴", "high": "🟡", "medium": "🟢", "low": "⚪"}
            icon = severity_icon.get(w["severity"], "⚪")
            lines.append(
                f"  {icon} [{w['severity'].upper()}] {w['label']}"
            )
            lines.append(_tr("security.file_line", "      文件: {file}:{line}").format(file=w['file'], line=w['line']))
            lines.append(_tr("security.match_pattern", "      匹配: {match}").format(match=w['match']))
            lines.append("")
        msg = "\n".join(lines)

        reply = QMessageBox.warning(
            self,
            _tr("security.title", "安全检查"),
            _tr("security.confirm_push", "{msg}\n仍然推送到远程仓库？").format(msg=msg),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.Yes:
            self._log(_tr("security.force_push", "用户选择忽略安全警告，继续 push..."))
            self._force_push()
        else:
            self._log(_tr("security.cancelled", "用户取消 push"))
            self.state.progress_label.setText(_tr("exec.push_cancelled", "Push 已取消"))
            self._refresh_button_states()

    def _force_push(self):
        self.state.progress_bar.setValue(0)
        self.state.progress_label.setText(_tr("exec.force_pushing", "正在强制 push..."))
        self._log(_tr("exec.force_pushing_log", "强制 push 中..."))

        self.state.push_worker = PushWorker(self.state.session, skip_scan=True)
        self.state.push_thread = QThread()
        self.state.push_worker.moveToThread(self.state.push_thread)
        self.state.push_thread.started.connect(self.state.push_worker.run)
        self.state.push_worker.progress.connect(self._on_push_progress)
        self.state.push_worker.finished.connect(self._on_push_finished)
        self.state.push_worker.finished.connect(self.state.push_thread.quit)
        self.state.push_worker.finished.connect(self.state.push_worker.deleteLater)
        self.state.push_thread.finished.connect(self.state.push_thread.deleteLater)
        self.state.push_thread.start()

    def _on_push_progress(self, current: int, total: int, msg: str):
        if msg:
            self.state.progress_label.setText(msg)
            self._log(msg)

    def _on_push_finished(self, success: bool, msg: str):
        self._log(msg)
        if self.state.main_window:
            self.state.main_window._set_state("idle")

        if success:
            self._refresh_formal_boxes()
            if self.state.selected_formal is not None:
                self._on_formal_box_clicked(self.state.selected_formal)
            QMessageBox.information(
                self,
                _tr("exec.push_success", "Push 成功"),
                _tr("exec.push_success_msg", "已推送到远程 GitHub！"),
            )
        else:
            QMessageBox.critical(
                self,
                _tr("exec.push_failed", "Push 失败"),
                _tr("exec.push_failed_msg", "请检查网络连接或远程仓库权限"),
            )

        self._refresh_button_states()
