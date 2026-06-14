"""TrialMixin — trial 检查 / 三叉决策 / bridge / 确认"""
from PySide6.QtCore import QThread, QTimer
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QTextEdit,
                               QVBoxLayout, QWidget)
from backend.core.i18n import _tr
from themes import get_theme
from backend.models import TrialAction
from ..workers import TrialCheckWorker, TriageWorker


class TrialMixin:
    """Trial 仓库检查、详情加载、三叉决策、确认流程"""

    # ── Trial check ──────────────────────────────────────

    def _check_trial(self):
        self._log(_tr("trial.check_start", "Checking trial..."))

        self.state.trial_worker = TrialCheckWorker(self.state.session)
        self.state.trial_thread = QThread()
        self.state.trial_worker.moveToThread(self.state.trial_thread)
        self.state.trial_thread.started.connect(self.state.trial_worker.run)
        self.state.trial_worker.finished.connect(self._on_trial_check_finished)
        self.state.trial_worker.finished.connect(self.state.trial_thread.quit)
        self.state.trial_worker.finished.connect(self.state.trial_worker.deleteLater)
        self.state.trial_thread.finished.connect(self.state.trial_thread.deleteLater)
        self.state.trial_thread.start()

    def _on_trial_check_finished(self, changes: list, summary: str):
        self._refresh_trial_boxes()
        self.state.trial_status.setText(summary)
        self._log(summary)

    # ── Trial box 列表 ───────────────────────────────────

    def _refresh_trial_boxes(self):
        self._clear_box_layout(self.state.trial_box_layout)
        pending = [ic for ic in (self.state.session.incoming_changes or []) if ic.triage == TrialAction.PENDING]
        # 更新 Tab 圆点
        if hasattr(self.state, 'incoming_dot'):
            self.state.incoming_dot.setVisible(len(pending) > 0)

        if not pending:
            label = QLabel(_tr("trial.no_changes", "  无待处理变更"))
            label.setStyleSheet("color: gray; padding: 8px;")
            self.state.trial_box_layout.addWidget(label)
        else:
            from ..incoming_card import IncomingChangeCard
            for i, ic in enumerate(self.state.session.incoming_changes):
                if ic.triage != TrialAction.PENDING:
                    continue
                card = IncomingChangeCard(
                    i,
                    ic.hash[:12],
                    ic.message[:60],
                    f"{ic.author} · {ic.timestamp[:10]}",
                )
                card.selected.connect(self._on_trial_box_clicked)
                self.state.trial_box_layout.addWidget(card)
        self.state.trial_box_layout.addStretch()

    def _on_trial_box_clicked(self, index: int):
        self.state.selected_incoming = index
        self._refresh_trial_boxes()
        ic = self.state.session.incoming_changes[index]
        self.state.trial_detail_title.setText(f"{ic.hash[:12]}  {ic.message.split(chr(10))[0][:60]}")
        self.state.trial_detail_meta.setText(
            _tr("trial.detail_meta", "{author} · {date}").format(
                author=ic.author, date=ic.timestamp[:10]))
        self._clear_box_layout(self.state.trial_files_widget.layout())
        tfl = self.state.trial_files_widget.layout()
        if ic.body:
            for line in ic.body.split("\n"):
                line = line.strip()
                if line and (line.startswith("+++") or line.startswith("---")
                             or line.startswith("diff --git")):
                    continue
                if line and not line.startswith("@@"):
                    lbl = QLabel(line)
                    lbl.setStyleSheet(f"font-size:10px;font-family:'Courier New',monospace;color:{get_theme().txt2};")
                    tfl.addWidget(lbl)
        else:
            no_files = QLabel(_tr("trial.no_file_list", "（无详细文件列表）"))
            no_files.setStyleSheet(f"font-size:10px;color:{get_theme().txt3};")
            tfl.addWidget(no_files)

    # ── 确认流程 + Bridge 可视化 ──────────────────────────

    def _build_bridge_widget(self, source_label: str, target_label: str,
                             source_color: str, target_color: str) -> QWidget:
        t = get_theme()
        w = QWidget()
        w.setStyleSheet(f"background:{t.bg2};border-radius:6px;padding:8px;")
        lo = QHBoxLayout(w)
        lo.setContentsMargins(16, 12, 16, 12)
        lo.setSpacing(0)

        src = QLabel(f"<span style='font-size:20px;color:{source_color};'>●</span>"
                     f" <span style='font-size:12px;font-weight:500;color:{t.txt};'>{source_label}</span>")
        lo.addWidget(src)
        lo.addStretch()

        arrow = QLabel("<span style='font-size:16px;color:#888780;'>"
                       " ──────────────────────→ </span>")
        lo.addWidget(arrow)
        lo.addStretch()

        tgt = QLabel(f"<span style='font-size:20px;color:{target_color};'>●</span>"
                     f" <span style='font-size:12px;font-weight:500;color:{t.txt};'>{target_label}</span>")
        lo.addWidget(tgt)
        return w

    def _confirm_and_execute(self, action: str):
        import sys
        print("[LOG] Trial.action action=" + str(action) + " target=" + str(self.state.selected_incoming), file=sys.stderr, flush=True)
        if self.state.selected_incoming is None:
            QMessageBox.information(
                self, _tr("dialog.hint", "提示"),
                _tr("trial.select_first", "请先在左侧列表中选择一条 incoming commit"))
            return
        if action == "accept":
            self._on_accept_clicked()
        else:
            self._do_triage(self.state.selected_incoming, action)

    # ── Accept 两阶段流程 ────────────────────────────

    def _on_accept_clicked(self):
        """第一阶段：显示 bridge + 确认框"""
        ic = self.state.session.incoming_changes[self.state.selected_incoming]
        self.state.bridge_from.setText(f"trial · {ic.hash[:7]}")
        default_msg = f"[incoming] {ic.message[:50]} — cherry-pick {ic.hash[:7]}"
        self.state.confirm_msg_box.setPlainText(default_msg)
        self.state.trial_action_widget.setVisible(False)
        self.state.bridge_widget.setVisible(True)
        self.state.confirm_widget.setVisible(True)
        # 连接确认/取消按钮
        try:
            self.state.confirm_ok_btn.clicked.disconnect()
            self.state.confirm_cancel_btn.clicked.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.state.confirm_ok_btn.clicked.connect(self._on_confirm_accept)
        self.state.confirm_cancel_btn.clicked.connect(self._on_cancel_accept)

    def _on_confirm_accept(self):
        """第二阶段：真正执行"""
        msg = self.state.confirm_msg_box.toPlainText().strip()
        if not msg:
            return
        ic = self.state.session.incoming_changes[self.state.selected_incoming]
        self._do_triage(self.state.selected_incoming, "accept")
        self._push_incoming_to_workshop(ic, msg)
        # 追加 Incoming 区段到文件树（B-1）
        if hasattr(self, 'add_incoming_section'):
            self.add_incoming_section(ic)
        # 重置 UI
        self.state.bridge_widget.setVisible(False)
        self.state.confirm_widget.setVisible(False)
        self.state.trial_action_widget.setVisible(True)

    def _on_cancel_accept(self):
        self.state.bridge_widget.setVisible(False)
        self.state.confirm_widget.setVisible(False)
        self.state.trial_action_widget.setVisible(True)

    def _push_incoming_to_workshop(self, ic, msg: str):
        """Accept 成功后把 incoming commit 写入 Workshop fm_column"""
        self.state.session.step_add_incoming_formal(msg)
        self._refresh_formal_boxes()
        self._refresh_commit_lines()
        self.state.tab_bar.setCurrentIndex(0)
        last_idx = len(self.state.session.formal_commits) - 1
        QTimer.singleShot(100, lambda: self._on_formal_box_clicked(last_idx))

    # ── 三叉决策 ──────────────────────────────────────────

    def _do_triage(self, index: int, action: str):
        if index < 0 or index >= len(self.state.session.incoming_changes):
            return
        self.state._last_triage_action = action
        self.state._last_triaged_change = index
        change = self.state.session.incoming_changes[index]
        self._log(_tr("trial.triaging", "处理: {hash} → {action}").format(hash=change.hash[:12], action=action))
        self.state.triage_worker = TriageWorker(self.state.session, index, action)
        self.state.triage_thread = QThread()
        self.state.triage_worker.moveToThread(self.state.triage_thread)
        self.state.triage_thread.started.connect(self.state.triage_worker.run)
        self.state.triage_worker.finished.connect(self._on_triage_finished)
        self.state.triage_worker.finished.connect(self.state.triage_thread.quit)
        self.state.triage_worker.finished.connect(self.state.triage_worker.deleteLater)
        self.state.triage_thread.finished.connect(self.state.triage_thread.deleteLater)
        self.state.triage_thread.start()

    def _on_triage_finished(self, success: bool, msg: str):
        self._log(msg)
        if success:
            self._refresh_trial_boxes()
            self._refresh_formal_boxes()
            if self.state._last_triage_action in ("accept", "promote"):
                self.state.tab_bar.setCurrentIndex(0)
                if self.state._last_triage_action == "accept":
                    self._log(_tr("trial.accept_auto", "Accept 完成，已自动切换到 Commit Workshop"))
                else:
                    self._log(_tr("trial.promote_auto", "Promote 完成，已自动切换到 Commit Workshop"))
        self._refresh_button_states()

    def _undo_last_triage(self):
        """撤销最近一次 triage 决策，恢复 IncomingChange 为 PENDING 状态"""
        idx = getattr(self.state, '_last_triaged_change', None)
        if idx is None:
            self._log(_tr("trial.no_undo", "没有可撤销的决策"))
            return
        changes = self.state.session.incoming_changes
        if idx < 0 or idx >= len(changes):
            self._log(_tr("trial.no_undo", "没有可撤销的决策"))
            return
        from backend.models import TrialAction
        change = changes[idx]
        old_action = change.triage
        change.triage = TrialAction.PENDING
        self.state._last_triage_action = ""
        self.state._last_triaged_change = None
        self._refresh_trial_boxes()
        self._refresh_formal_boxes()
        self._log(_tr("trial.undone",
                      "已撤销: {action} → pending").format(
                          action=old_action.value))
