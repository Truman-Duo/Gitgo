"""CommitMixin — workspace/formal commit 交互"""
import re
from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QInputDialog,
                               QLabel, QMessageBox, QPushButton, QTextEdit,
                               QVBoxLayout)
from backend.core import SyncSession, build_commit_template, validate_commit_message
from backend.core.i18n import _tr
from themes import get_theme
from backend.core.submitter import submit_commit_message
from ..widgets import WorkspaceCommitBox, FormalCommitBox


class CommitMixin:
    """Workspace / Formal commit 刷新、点击、合并、提交、编辑、dissolve"""

    # ── Workspace Box ───────────────────────────────────────

    def _refresh_workspace_boxes(self):
        commits = self.state.session.step_load_commits()
        self._clear_box_layout(self.state.ws_box_layout)

        if not commits:
            label = QLabel(_tr("commit.no_new", "  无新 commit"))
            label.setStyleSheet("color: gray; padding: 8px;")
            self.state.ws_box_layout.addWidget(label)
            self._log(_tr("commit.no_new_log", "未检测到新 commit"))
            if hasattr(self.state, 'ws_hdr'):
                self.state.ws_hdr.setVisible(False)
        else:
            if hasattr(self.state, 'ws_hdr'):
                self.state.ws_hdr.setVisible(True)
            self.state.ws_container.setUpdatesEnabled(False)
            try:
                for i, c in enumerate(commits):
                    commit_type = c.type or "chore"
                    summary = c.subject[:60]
                    meta = f"{c.hash[:8]}"
                    box = WorkspaceCommitBox(i, commit_type, summary, meta, self.state.ws_container)
                    box.clicked.connect(self._on_workspace_box_clicked)
                    self.state.ws_box_layout.addWidget(box)
                self._log(_tr("commit.found_count", "发现 {n} 个 workspace commit").format(n=len(commits)))
                self._update_workspace_box_styles()
            finally:
                self.state.ws_container.setUpdatesEnabled(True)

        self.state.ws_box_layout.addStretch()
        QTimer.singleShot(0, self._refresh_commit_lines)

    def _on_workspace_box_clicked(self, index: int):
        import sys
        modifiers = QApplication.keyboardModifiers()
        mod = "Ctrl" if modifiers == Qt.ControlModifier else ("Shift" if modifiers == Qt.ShiftModifier else "click")
        print("[LOG] WorkspaceBox.click idx=" + str(index) + " mod=" + mod, file=sys.stderr, flush=True)
        if modifiers == Qt.ControlModifier:
            self.state.session.step_toggle_workspace_selection(index, "toggle")
        elif modifiers == Qt.ShiftModifier and self.state.session.selected_workspace:
            self.state.session.step_toggle_workspace_selection(index, "range")
        else:
            self.state.session.step_toggle_workspace_selection(index, "single")
        self._update_workspace_box_styles()
        self._refresh_button_states()

    def _update_workspace_box_styles(self):
        for i in range(self.state.ws_box_layout.count()):
            item = self.state.ws_box_layout.itemAt(i)
            w = item.widget()
            if isinstance(w, WorkspaceCommitBox):
                w.selected = w._idx in self.state.session.selected_workspace

    # ── Formal Box ───────────────────────────────────────

    def _refresh_formal_boxes(self):
        self._clear_box_layout(self.state.fm_box_layout)
        if not self.state.session.formal_commits:
            label = QLabel(_tr("commit.no_formal", "  暂无正式 commit"))
            label.setStyleSheet("color: gray; padding: 8px;")
            self.state.fm_box_layout.addWidget(label)
            if hasattr(self.state, 'fm_hdr'):
                self.state.fm_hdr.setVisible(False)
        else:
            if hasattr(self.state, 'fm_hdr'):
                self.state.fm_hdr.setVisible(True)
            self.state.fm_container.setUpdatesEnabled(False)
            try:
                for i, fc in enumerate(self.state.session.formal_commits):
                    header = fc.message.split("\n")[0]
                    synced_str = _tr("commit.status_synced", "已同步") if fc.synced else _tr("commit.status_unsynced", "未同步")
                    pushed_str = _tr("commit.status_pushed", " 已推送") if fc.pushed else ""
                    box = FormalCommitBox(i, header, f"{synced_str}{pushed_str}", self.state.fm_container)
                    box.set_synced(fc.synced)
                    box.set_pushed(fc.pushed)
                    if getattr(fc, 'is_incoming', False):
                        box.set_incoming(True)
                    box.clicked.connect(self._on_formal_box_clicked)
                    box.double_clicked.connect(self._on_formal_box_double_clicked)
                    box.context_menu.connect(self._on_formal_box_context_menu)
                    self.state.fm_box_layout.addWidget(box)
                self._update_formal_box_styles()
            finally:
                self.state.fm_container.setUpdatesEnabled(True)
        self.state.fm_box_layout.addStretch()
        QTimer.singleShot(0, self._refresh_commit_lines)

    def _on_formal_box_clicked(self, index: int):
        import sys
        print("[LOG] FormalBox.click idx=" + str(index) + " current=" + str(self.state.selected_formal), file=sys.stderr, flush=True)
        if self.state.selected_formal == index:
            self.state.selected_formal = None
        else:
            self.state.selected_formal = index
        self._update_formal_box_styles()
        self._refresh_button_states()

    def _update_formal_box_styles(self):
        for i in range(self.state.fm_box_layout.count()):
            item = self.state.fm_box_layout.itemAt(i)
            w = item.widget()
            if isinstance(w, FormalCommitBox):
                w.set_selected(w._idx == self.state.selected_formal)

    # ── Commit 连接线 ───────────────────────────────────────

    def _refresh_commit_lines(self):
        """计算 CommitCanvas 贝塞尔连接线坐标 — 含虚线支持"""
        canvas = getattr(self.state, 'commit_canvas', None)
        if canvas is None:
            return
        import shiboken6
        if not shiboken6.isValid(canvas):
            return
        from ..commit_box import WorkspaceCommitBox, FormalCommitBox
        lines = []
        t = get_theme()

        for fm_idx, fc in enumerate(self.state.session.formal_commits):
            if not fc.source_indices:
                continue
            fm_box = None
            for i in range(self.state.fm_box_layout.count()):
                item = self.state.fm_box_layout.itemAt(i)
                w = item.widget()
                if isinstance(w, FormalCommitBox) and w._idx == fm_idx:
                    fm_box = w
                    break
            if not fm_box or not fm_box.isVisible():
                continue

            fm_center = fm_box.mapTo(canvas, fm_box.rect().center()).y()

            # 颜色 + 线型：根据关联 workspace commit 类型决定虚线
            if getattr(fc, 'is_incoming', False):
                color, dashed = t.amber, True
            elif fc.pushed:
                color, dashed = t.success_txt, False
            elif fc.synced:
                color, dashed = t.success, False
            else:
                # 检查关联的 workspace commit 类型决定是否虚线
                all_light = True
                for ws_idx in fc.source_indices:
                    if ws_idx < len(self.state.session.commits):
                        ct = self.state.session.commits[ws_idx].type
                        if ct not in ("docs", "chore", "style"):
                            all_light = False
                            break
                color, dashed = (t.teal, True) if all_light else (t.blue, False)

            for ws_idx in fc.source_indices:
                ws_box = None
                for i in range(self.state.ws_box_layout.count()):
                    item = self.state.ws_box_layout.itemAt(i)
                    w = item.widget()
                    if isinstance(w, WorkspaceCommitBox) and w._idx == ws_idx:
                        ws_box = w
                        break
                if not ws_box or not ws_box.isVisible():
                    continue
                ws_center = ws_box.mapTo(canvas, ws_box.rect().center()).y()
                lines.append((ws_center, fm_center, color, dashed))

        canvas.set_lines(lines)

    def _delete_selected_formal(self):
        if self.state.selected_formal is None:
            return
        idx = self.state.selected_formal
        fc = self.state.session.formal_commits[idx]
        reply = QMessageBox.question(
            self,
            _tr("dialog.confirm_delete", "确认删除"),
            _tr("commit.confirm_delete", "删除正式 commit「{msg}」？\n（仅移除本地记录，不影响备份仓库）").format(msg=fc.message.split(chr(10))[0][:50]),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.state.session.step_delete_formal(idx)
            self.state.selected_formal = None
            self._refresh_formal_boxes()
            self._refresh_button_states()
            self._log(_tr("commit.deleted", "已删除正式 commit"))

    # ── Formal box 双击 / 右键菜单 ───────────────────────

    def _on_formal_box_double_clicked(self, index: int, action: str):
        if action == "message":
            self._edit_formal_message(index)

    def _on_formal_box_context_menu(self, index: int, action: str):
        if action == "edit_message":
            self._edit_formal_message(index)
        elif action == "edit_number":
            self._edit_formal_number(index)
        elif action == "dissolve":
            self._dissolve_formal_commit(index)
        elif action == "clear_sources":
            self._clear_formal_sources(index)

    def _edit_formal_message(self, index: int):
        fc = self.state.session.formal_commits[index]
        dlg = QDialog(self)
        dlg.setWindowTitle(_tr("commit.edit_title", "编辑正式 Commit Message"))
        dlg.setMinimumSize(550, 350)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(
            _tr("commit.edit_hint", "请编辑正式 commit message（首行格式: [PREFIX-N] type: subject）：")))
        editor = QTextEdit()
        editor.setPlainText(fc.message)
        layout.addWidget(editor)
        btn_row = QHBoxLayout()
        ok_btn = QPushButton(_tr("settings.ok", "确认"))
        cancel_btn = QPushButton(_tr("settings.cancel", "取消"))
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)

        if dlg.exec() == QDialog.Accepted:
            msg = editor.toPlainText().strip()
            if self.state.session.step_edit_formal_message(index, msg):
                self._refresh_formal_boxes()
                self._log(_tr("commit.message_updated", "正式 Commit 消息已更新"))
            else:
                QMessageBox.warning(self, _tr("commit.format_error_title", "格式错误"),
                                    _tr("commit.format_error", "格式校验失败"))

    def _edit_formal_number(self, index: int):
        fc = self.state.session.formal_commits[index]
        new_num, ok = QInputDialog.getInt(
            self,
            _tr("commit.edit_number_title", "编辑编号"),
            _tr("commit.edit_number_hint", "输入新的编号（正整数）："),
            value=fc.number,
            min=1,
            max=99999,
        )
        if not ok:
            return
        if self.state.session.step_edit_formal_number(index, new_num):
            self._refresh_formal_boxes()
            self._log(_tr("commit.number_updated", "编号已更新为 [{prefix}-{n}]").format(prefix=self.state.session.formal_commits[index].prefix, n=new_num))
        else:
            QMessageBox.warning(
                self, _tr("commit.duplicate_number_title", "编号冲突"),
                _tr("commit.duplicate_number", "编号 {n} 已被其他正式 Commit 使用").format(n=new_num))

    def _dissolve_formal_commit(self, index: int):
        fc = self.state.session.formal_commits[index]
        if not fc.source_indices:
            QMessageBox.information(
                self, _tr("dialog.hint", "提示"),
                _tr("commit.no_sources", "该 Formal Commit 没有关联的 Workspace commits，无法 Dissolve"))
            return
        reply = QMessageBox.question(
            self, _tr("dialog.confirm_dissolve", "确认 Dissolve"),
            _tr("commit.confirm_dissolve", "恢复 {n} 个 Workspace commits 并删除此 Formal Commit？").format(
                n=len(fc.source_indices)),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.state.session.step_dissolve_formal(index)
        self.state.selected_formal = None
        self._refresh_formal_boxes()
        self._refresh_workspace_boxes()
        self._refresh_button_states()
        self._log(_tr("commit.dissolved", "已 Dissolve，Workspace commits 已恢复"))

    def _clear_formal_sources(self, index: int):
        fc = self.state.session.formal_commits[index]
        if not fc.source_indices:
            QMessageBox.information(
                self, _tr("dialog.hint", "提示"),
                _tr("commit.no_sources", "该 Formal Commit 没有关联的 Workspace commits"))
            return
        count = len(fc.source_indices)
        # 标记封存，禁用关联的 workspace 卡片
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        from ..commit_box import WorkspaceCommitBox
        for ws_idx in fc.source_indices:
            for i in range(self.state.ws_box_layout.count()):
                item = self.state.ws_box_layout.itemAt(i)
                w = item.widget()
                if isinstance(w, WorkspaceCommitBox) and w._idx == ws_idx:
                    effect = QGraphicsOpacityEffect(w)
                    effect.setOpacity(0.3)
                    w.setGraphicsEffect(effect)
                    w.setEnabled(False)
                    break
        self.state.session.step_clear_formal_sources(index)
        self._refresh_commit_lines()
        self._log(_tr("commit.sources_cleared", "已清除 {n} 个来源引用").format(n=count))

    # ── 提交 commit message ─────────────────────────────────

    def _submit_commit_message(self):
        import sys
        msg = self.state.msg_box.toPlainText().strip()
        merging = getattr(self.state, '_merging', False)
        print("[LOG] CommitMixin._submit_commit merging=" + str(merging) + " msg_len=" + str(len(msg)), file=sys.stderr, flush=True)
        if not msg:
            return

        merging = getattr(self.state, '_merging', False)
        if merging:
            # 合并模式：从 msg_box 读取编辑后的消息，执行 merge
            err = validate_commit_message(msg)
            if err:
                QMessageBox.warning(self, _tr("commit.format_error_title", "格式错误"), err)
                return
            fc = self.state.session.step_create_formal_commit(
                selected_indices=self.state._merge_indices, message=msg,
            )
            if fc is None:
                return
            self.state._merging = False

            for i in range(self.state.ws_box_layout.count()):
                item = self.state.ws_box_layout.itemAt(i)
                w = item.widget()
                if isinstance(w, WorkspaceCommitBox) and w._idx in self.state._merge_indices:
                    w.set_merged()
                    w.selected = False
            self.state._merge_indices = set()

            self._refresh_formal_boxes()
            self.state.selected_formal = len(self.state.session.formal_commits) - 1
            self._on_formal_box_clicked(self.state.selected_formal)
            self.state.msg_box.clear()
            self._log(
                _tr("commit.created", "正式 Commit 已创建: [{prefix}-{number}]").format(
                    prefix=fc.prefix, number=fc.number
                )
            )
        else:
            # 直接提交模式
            fc = submit_commit_message(self.state.session, self.state.project, msg)
            if fc is None:
                QMessageBox.warning(
                    self,
                    _tr("commit.format_error_title", "格式错误"),
                    _tr("commit.format_error", "首行格式必须为 [PREFIX-N] type: subject"),
                )
                return

            self._refresh_formal_boxes()
            self.state.selected_formal = len(self.state.session.formal_commits) - 1
            self._on_formal_box_clicked(self.state.selected_formal)
            self.state.msg_box.clear()
            self._log(
                _tr("commit.created", "正式 Commit 已创建: [{prefix}-{number}]").format(
                    prefix=fc.prefix, number=fc.number
                )
            )

    # ── 合并 ─────────────────────────────────────────────

    def _merge_selected(self):
        import sys
        print("[LOG] CommitMixin._merge_selected count=" + str(len(self.state.session.selected_workspace)), file=sys.stderr, flush=True)
        if len(self.state.session.selected_workspace) < 1:
            return

        selected_indices = sorted(self.state.session.selected_workspace)
        selected_commits = [self.state.session.commits[i] for i in selected_indices]
        template = build_commit_template(selected_commits, self.state.project)

        # 模板直接填入下方 msg_box，不弹窗
        self.state.msg_box.setPlainText(template)
        self.state.msg_box.setFocus()
        self.state._merging = True
        self.state._merge_indices = set(self.state.session.selected_workspace)
