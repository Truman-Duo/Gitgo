"""ProjectList panel — 项目管理首页（P5: 同步状态 / 错误徽章 / 定时刷新）"""
from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QDialog, QHeaderView, QLabel, QMenu,
                               QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)
from typing import Optional
from backend.core.config import Config, ConfigManager, ProjectConfig
from themes import get_theme
from backend.models import RepoNode, SyncStatus
from backend.core.i18n import _tr
from .project_edit_dialog import _ProjectEditDialog

class _AddProjectRow(QWidget):
    """带 hover 效果的 "+" 添加行"""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        from themes import get_theme
        t = get_theme()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setAlignment(Qt.AlignCenter)
        plus_label = QLabel("＋")
        plus_label.setAlignment(Qt.AlignCenter)
        plus_label.setTextInteractionFlags(Qt.NoTextInteraction)
        plus_label.setStyleSheet(f"font-size: 22px; font-weight: bold; color: {t.txt3};")
        hint_label = QLabel(_tr("project.add_hint", "添加项目"))
        hint_label.setAlignment(Qt.AlignCenter)
        hint_label.setTextInteractionFlags(Qt.NoTextInteraction)
        hint_label.setStyleSheet(f"font-size: 10px; color: {t.txt3};")
        layout.addWidget(plus_label)
        layout.addWidget(hint_label)
        self._default_ss = f"background: {t.bg2};"
        self.setStyleSheet(self._default_ss)

    def refresh_theme(self):
        """主题切换后刷新颜色"""
        from themes import get_theme
        t = get_theme()
        self._default_ss = f"background: {t.bg2};"
        self.setStyleSheet(self._default_ss)

    def enterEvent(self, event):
        from themes import get_theme
        self.setStyleSheet(f"background: {get_theme().bg3};")
        self.setCursor(Qt.PointingHandCursor)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setStyleSheet(self._default_ss)
        self.setCursor(Qt.ArrowCursor)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)

class ProjectListPanel(QWidget):
    """项目列表首页 — 像微信好友列表，点击进入操作界面"""

    project_selected = Signal(object)  # ProjectConfig

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        title = QLabel(_tr("project.list_title", "项目列表"))
        title.setStyleSheet("font-size: 20px; font-weight: bold; margin-bottom: 10px; margin-left: 4px;")
        layout.addWidget(title)

        # "+" 添加行
        self.add_row = _AddProjectRow()
        self.add_row.clicked.connect(self._add_project)
        layout.addWidget(self.add_row)

        # 项目表格（四列：项目名 / 备注 / 同步状态 / 最后同步）
        self.project_table = QTableWidget()
        self.project_table.setColumnCount(4)
        self.project_table.setHorizontalHeaderLabels([
            _tr("project.name_header", "项目名"),
            _tr("project.col_note", "备注"),
            _tr("project.col_status", "状态"),
            _tr("project.col_last_sync", "最后同步"),
        ])
        self.project_table.setShowGrid(False)
        self.project_table.setAlternatingRowColors(True)
        hh = self.project_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Fixed)
        hh.setSectionResizeMode(3, QHeaderView.Fixed)
        hh.resizeSection(0, 200)
        hh.resizeSection(2, 60)
        hh.resizeSection(3, 150)
        self.project_table.verticalHeader().setVisible(False)
        self.project_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.project_table.setFocusPolicy(Qt.NoFocus)
        self.project_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.project_table.customContextMenuRequested.connect(self._on_context_menu)
        self.project_table.cellDoubleClicked.connect(self._on_double_click)
        self.project_table.cellChanged.connect(self._on_note_changed)
        layout.addWidget(self.project_table)

        # 滚动条：默认隐藏，鼠标移入显示，离开 2s 后隐藏
        self.project_table.verticalScrollBar().setStyleSheet("width: 0px;")
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._hide_scrollbar)
        self.project_table.viewport().installEventFilter(self)

        # P5: 定时刷新同步状态（每 30s）
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_statuses)
        self._refresh_timer.start(30000)

        self._refresh_table()

    def _refresh_table(self):
        self._last_sync_map = self._load_last_sync()
        n = len(self.config.projects)
        self.project_table.setRowCount(n)

        for i, p in enumerate(self.config.projects):
            row = i
            # Col 0: 项目名
            name_item = QTableWidgetItem(p.name or _tr("project.unnamed", "(未命名)"))
            name_item.setData(Qt.UserRole, i)
            name_item.setTextAlignment(Qt.AlignCenter)
            name_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.project_table.setItem(row, 0, name_item)

            # Col 1: 备注（可双击编辑）
            note_item = QTableWidgetItem(p.note)
            note_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable)
            self.project_table.setItem(row, 1, note_item)

            # Col 2: 同步状态（彩色状态点）
            st = p.sync_status
            status_text = {
                SyncStatus.VALID: _tr("project.status_valid", "✓"),
                SyncStatus.EMPTY: _tr("project.status_empty", "✗"),
                SyncStatus.MISSING: _tr("project.status_missing", "—"),
            }.get(st, "?")
            sync_item = QTableWidgetItem(status_text)
            sync_item.setTextAlignment(Qt.AlignCenter)
            sync_item.setFlags(Qt.ItemIsEnabled)
            sync_fg = {
                SyncStatus.VALID: "#1d9e75",
                SyncStatus.EMPTY: "#e24b4a",
                SyncStatus.MISSING: "#888780",
            }.get(st, "#888780")
            sync_item.setForeground(QColor(sync_fg))
            sync_item.setFont(QFont("", 14, QFont.Bold))
            sync_item.setToolTip({
                SyncStatus.VALID: _tr("project.tt_valid", "配置有效"),
                SyncStatus.EMPTY: _tr("project.tt_empty", "备份路径无效或不是 git 仓库"),
                SyncStatus.MISSING: _tr("project.tt_missing", "备份路径未配置"),
            }.get(st, ""))
            self.project_table.setItem(row, 2, sync_item)

            # Col 3: 最后同步时间
            last_sync = self._last_sync_map.get(p.name, "")
            ts_item = QTableWidgetItem(last_sync)
            ts_item.setTextAlignment(Qt.AlignCenter)
            ts_item.setFlags(Qt.ItemIsEnabled)
            ts_item.setForeground(QColor("#888780"))
            ts_item.setFont(QFont("", 9))
            self.project_table.setItem(row, 3, ts_item)

            self._style_project_row(row, p)

    def _on_note_changed(self, row: int, col: int):
        if col != 1:
            return
        idx = row
        if 0 <= idx < len(self.config.projects):
            item = self.project_table.item(row, 1)
            if item:
                self.config.projects[idx].note = item.text()
                ConfigManager.save(self.config)

    # ── P5: 同步状态 / 最后同步 / 定时刷新 ─────────────────

    def _load_last_sync(self) -> dict[str, str]:
        """从 HistoryManager 加载每个项目的最新同步时间"""
        try:
            from backend.core.history import HistoryManager
            entries = HistoryManager.load()
        except Exception:
            return {}
        result: dict[str, str] = {}
        for he in entries:
            ts = he.timestamp[:19] if he.timestamp else ""
            if he.project_name not in result:
                result[he.project_name] = ts
            else:
                # 保留最新的一条
                if ts > result[he.project_name]:
                    result[he.project_name] = ts
        return result

    def _refresh_statuses(self):
        """定时刷新：更新状态列和最后同步时间列（不重建表格）"""
        sync_map = self._load_last_sync()
        for i, p in enumerate(self.config.projects):
            st = p.sync_status
            # Col 2: 状态
            status_item = self.project_table.item(i, 2)
            if status_item:
                status_text = {
                    SyncStatus.VALID: "✓",
                    SyncStatus.EMPTY: "✗",
                    SyncStatus.MISSING: "—",
                }.get(st, "?")
                status_item.setText(status_text)
                fg = {
                    SyncStatus.VALID: "#1d9e75",
                    SyncStatus.EMPTY: "#e24b4a",
                    SyncStatus.MISSING: "#888780",
                }.get(st, "#888780")
                status_item.setForeground(QColor(fg))
                status_item.setToolTip({
                    SyncStatus.VALID: _tr("project.tt_valid", "配置有效"),
                    SyncStatus.EMPTY: _tr("project.tt_empty", "备份路径无效或不是 git 仓库"),
                    SyncStatus.MISSING: _tr("project.tt_missing", "备份路径未配置"),
                }.get(st, ""))
            # Col 3: 最后同步
            ts_item = self.project_table.item(i, 3)
            if ts_item:
                last_sync = sync_map.get(p.name, "")
                ts_item.setText(last_sync)

    def _style_project_row(self, row: int, project: ProjectConfig):
        """根据同步状态设置行样式（alpha 着色，兼容暗/亮主题）"""
        st = project.sync_status
        bg = {
            SyncStatus.VALID: QColor(76, 175, 130, 35),
            SyncStatus.EMPTY: QColor(226, 75, 74, 35),
            SyncStatus.MISSING: QColor(128, 128, 128, 18),
        }.get(st, QColor(0, 0, 0, 0))
        for col in range(4):
            item = self.project_table.item(row, col)
            if item:
                item.setBackground(bg)

    def _hide_scrollbar(self):
        self.project_table.verticalScrollBar().setStyleSheet("width: 0px;")

    def eventFilter(self, obj, event):
        if obj == self.project_table.viewport():
            if event.type() == QEvent.Enter:
                self._scroll_timer.stop()
                self.project_table.verticalScrollBar().setStyleSheet("width: 8px;")
            elif event.type() == QEvent.Leave:
                self._scroll_timer.start(2000)
            elif event.type() == QEvent.MouseButtonPress:
                if not self.project_table.indexAt(event.pos()).isValid():
                    self.project_table.setFocus()  # 夺走编辑器焦点触发 commit
            elif event.type() == QEvent.Wheel:
                self._scroll_timer.stop()
                self.project_table.verticalScrollBar().setStyleSheet("width: 8px;")
        return super().eventFilter(obj, event)

    def _on_context_menu(self, pos):
        row = self.project_table.rowAt(pos.y())
        if row < 0:
            return
        project_index = row
        if project_index >= len(self.config.projects):
            return
        self.project_table.selectRow(row)
        menu = QMenu(self)
        open_action = menu.addAction(_tr("project.open_context", "打开项目"))
        edit_action = menu.addAction(_tr("project.edit_context", "编辑项目"))
        delete_action = menu.addAction(_tr("project.delete_context", "删除项目"))
        action = menu.exec(self.project_table.viewport().mapToGlobal(pos))
        if action == open_action:
            self._enter_project(project_index)
        elif action == edit_action:
            self._edit_project_at(project_index)
        elif action == delete_action:
            self._remove_project_at(project_index)

    def _selected_row(self) -> int | None:
        rows = set()
        for item in self.project_table.selectedItems():
            rows.add(item.row())
        return next(iter(rows)) if rows else None

    def _on_double_click(self, row: int, col: int):
        self._enter_project(row)

    def _enter_project(self, row: int):
        if 0 <= row < len(self.config.projects):
            self.project_selected.emit(self.config.projects[row])

    def _add_project(self):
        dlg = _ProjectEditDialog(self, existing_names=[p.name for p in self.config.projects])
        if dlg.exec() == QDialog.Accepted:
            pc = dlg.get_project()
            self.config.projects.append(pc)
            ConfigManager.save(self.config)
            self._refresh_table()

    def _edit_project_at(self, idx: int):
        pc = self.config.projects[idx]
        dlg = _ProjectEditDialog(self, pc, existing_names=[p.name for p in self.config.projects])
        if dlg.exec() == QDialog.Accepted:
            self.config.projects[idx] = dlg.get_project()
            ConfigManager.save(self.config)
            self._refresh_table()

    def _remove_project_at(self, idx: int):
        pc = self.config.projects[idx]
        reply = QMessageBox.question(
            self,
            _tr("dialog.confirm_delete", "确认删除"),
            _tr("project.confirm_delete", "删除项目「{name}」？\n（仅移除配置记录，不影响目录文件）").format(name=pc.name),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.config.projects.pop(idx)
            ConfigManager.save(self.config)
            self._refresh_table()

# ── 工作区操作面板 ────────────────────────────────────────────

