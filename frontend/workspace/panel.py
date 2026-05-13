"""WorkspacePanel — 核心定义"""
from pathlib import Path
from PySide6.QtCore import QEvent, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QShowEvent
from PySide6.QtGui import QColor, QShortcut, QKeySequence
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSplitter, QTabWidget, QTextEdit,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)
from backend.core.config import Config, ConfigManager, ProjectConfig
from backend.core import SyncSession, get_file_diff, SessionStage
from backend.core.i18n import _tr
from themes import get_theme
from ..workers import ScanWorker
from ..project_edit_dialog import _ProjectEditDialog

from .builder import BuilderMixin
from .theme import ThemeMixin
from .commits import CommitMixin
from .syncpush import SyncPushMixin
from .trial import TrialMixin
from .remotes import RemotesMixin
from .history import HistoryMixin
from .panel_state import PanelState


class WorkspacePanel(BuilderMixin, CommitMixin, SyncPushMixin, TrialMixin, RemotesMixin, HistoryMixin, ThemeMixin, QWidget):
    """项目工作区 — Tab 驱动: Workshop / Incoming / Remotes / History

    所有跨 Mixin 共享状态存放于 self.state (PanelState)，
    各 Mixin 通过 self.state.xxx 访问，消除隐式契约。
    """

    back_requested = Signal()

    def __init__(self, config: Config, project: ProjectConfig, main_window=None):
        super().__init__()
        s = self.state = PanelState()
        s.config = config
        s.project = project
        s.main_window = main_window
        s.session = SyncSession(project, config)
        s.selected_formal = None
        s.selected_incoming = None
        s._last_triage_action = ""
        s._scan_done = False
        s._scanning = False
        s.session.on_log = lambda msg: self._log(msg)
        s.session.on_stage_changed = self._on_stage_changed
        self._init_ui()
        self._update_node_status(s.project)
        self._auto_load_file_tree()
        self._setup_shortcuts()

    # ── 信号映射表（集中文档，便于追踪）─────────────────────
    #
    # BuilderMixin._init_ui:
    #   tab_bar.currentChanged → _on_tab_changed
    #
    # WorkshopTabMixin._build_workshop_bottom_row:
    #   merge_btn.clicked → _merge_selected       (CommitMixin)
    #   sync_btn.clicked → _start_sync            (SyncPushMixin)
    #   push_btn.clicked → _start_push            (SyncPushMixin)
    #   delete_formal_btn.clicked → _delete_selected_formal (CommitMixin)
    #   msg_submit_btn.clicked → _submit_commit_message     (CommitMixin)
    #   commit_scroll.valueChanged → QTimer → _refresh_commit_lines (CommitMixin)
    #
    # ExplorerMixin._build_explorer_panel:
    #   file_tree.itemClicked → _on_tree_item_clicked → _show_diff_by_path
    #
    # CommitMixin (box 刷新时动态连接):
    #   WorkspaceCommitBox.clicked → _on_workspace_box_clicked
    #   FormalCommitBox.clicked → _on_formal_box_clicked
    #   FormalCommitBox.double_clicked → _on_formal_box_double_clicked
    #   FormalCommitBox.context_menu → _on_formal_box_context_menu
    #
    # IncomingTabMixin._build_incoming_tab:
    #   trial_check_btn.clicked → _check_trial    (TrialMixin)
    #   trial_action_widget btns → _confirm_and_execute (TrialMixin)
    #
    # TrialMixin._refresh_trial_boxes:
    #   IncomingChangeCard.selected → _on_trial_box_clicked
    #
    # MainWindow:
    #   Escape QShortcut → _back_to_list
    #   breadcrumb linkActivated → _back_to_list
    #   sidebar_toggle/sidebar_collapse → _toggle_sidebar
    #   settings_btn → _open_settings

    def showEvent(self, event):
        super().showEvent(event)
        self._update_action_bar()

    def _log(self, msg: str):
        if self.state.main_window:
            self.state.main_window._log_bar(msg)

    def _on_stage_changed(self, stage):
        """core 状态变更 → 集中更新 GUI 按钮状态（线程安全）"""
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self._apply_stage(stage))

    def _apply_stage(self, stage):
        """在主线程执行按钮状态更新"""
        busy = stage in (SessionStage.SCANNING, SessionStage.COMMITTING,
                         SessionStage.SYNCING, SessionStage.PUSHING,
                         SessionStage.TRIAL_CHECKING)
        s = self.state
        if busy:
            s.sync_btn.setEnabled(False)
            s.push_btn.setEnabled(False)
            s.merge_btn.setEnabled(False)
            s.delete_formal_btn.setEnabled(False)
            s.trial_check_btn.setEnabled(False)
        elif stage == SessionStage.IDLE or stage == SessionStage.SELECTING:
            self._refresh_button_states()
        elif stage == SessionStage.TRIAL_REVIEWING:
            s.trial_check_btn.setEnabled(True)

    def _refresh_button_states(self):
        """从 session 数据推导按钮 enabled 状态"""
        s = self.state
        # merge: 有选中的 workspace commit
        s.merge_btn.setEnabled(len(s.session.selected_workspace) >= 1)
        # sync: 有选中的 formal commit 且未 synced
        if s.selected_formal is not None and s.selected_formal < len(s.session.formal_commits):
            fc = s.session.formal_commits[s.selected_formal]
            s.sync_btn.setEnabled(not fc.synced)
        else:
            s.sync_btn.setEnabled(False)
        # delete: 有选中的 formal commit
        s.delete_formal_btn.setEnabled(s.selected_formal is not None)
        # push: 有 synced 未 pushed 的 formal commit
        s.push_btn.setEnabled(any(fc.synced and not fc.pushed for fc in s.session.formal_commits))
        # trial check: 始终可用（除非 busy）
        s.trial_check_btn.setEnabled(True)

    def _start_scan(self):
        s = self.state
        if s._scanning:
            return
        s._scanning = True
        if s.main_window:
            s.main_window._set_state("scanning")
        self._log(_tr("scan.start", "Scanning workspace..."))
        s.scan_worker = ScanWorker(s.session)
        s._scan_thread = QThread()
        s.scan_worker.moveToThread(s._scan_thread)
        s._scan_thread.started.connect(s.scan_worker.run)
        s.scan_worker.finished.connect(self._on_scan_finished)
        s.scan_worker.finished.connect(s._scan_thread.quit)
        s._scan_thread.start()

    def _on_scan_finished(self, entries, summary: str):
        s = self.state
        s._scanning = False
        self._log(summary)
        self._populate_file_tree()
        self._refresh_workspace_boxes()
        self._refresh_formal_boxes()
        s._scan_done = True
        if s.main_window:
            s.main_window._set_state("idle")

    def _clear_box_layout(self, layout):
        while layout.count() > 0:
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _setup_shortcuts(self):
        sc = (
            ("Ctrl+Shift+S", self._start_scan),
            ("Ctrl+Shift+M", self._merge_selected),
            ("Ctrl+S",       self._start_sync),
            ("Ctrl+Shift+P", self._start_push),
            ("Ctrl+Return",  self._submit_commit_message),
        )
        for key, cb in sc:
            QShortcut(QKeySequence(key), self, cb)

    def _edit_paths(self):
        s = self.state
        dlg = _ProjectEditDialog(self, s.project, existing_names=[p.name for p in s.config.projects])
        if dlg.exec() == QDialog.Accepted:
            updated = dlg.get_project()
            s.project.name = updated.name
            s.project.workspace_path = updated.workspace_path
            s.project.backup_path = updated.backup_path
            s.project.trial_path = updated.trial_path
            s.project.commit_format = updated.commit_format
            ConfigManager.save(s.config)
            self._log(_tr("config.updated", "项目配置已更新"))
