"""GUI 桌面界面 - PySide6"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from config import Config, ConfigManager, ProjectConfig
from core import (
    CommitInfo,
    FileEntry,
    _find_next_number,
    build_commit_template,
    compare_files,
    get_exclude_patterns,
    get_git_log,
    push_to_backup,
    scan_workspace,
    sync_to_backup,
    validate_commit_message,
)


# ── 数据模型 ────────────────────────────────────────────────


@dataclass
class FormalCommit:
    message: str
    number: int
    prefix: str
    synced: bool = False
    pushed: bool = False
    created_at: str = ""


# ── 自定义 Box 控件 ──────────────────────────────────────────


class CommitBox(QFrame):
    """可点击选中的 commit box 控件"""

    clicked = Signal(int)

    def __init__(self, index: int, title: str, subtitle: str, parent=None):
        super().__init__(parent)
        self._idx = index
        self._selected = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(48)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)

        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(self.title_label)

        self.sub_label = QLabel(subtitle)
        self.sub_label.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(self.sub_label)

        self._update_style()

    def mousePressEvent(self, event):
        self.clicked.emit(self._idx)
        super().mousePressEvent(event)

    @property
    def selected(self) -> bool:
        return self._selected

    @selected.setter
    def selected(self, value: bool):
        self._selected = value
        self._update_style()

    def _update_style(self):
        raise NotImplementedError


class WorkspaceCommitBox(CommitBox):
    """工作区 commit box — 白底灰边，选中时蓝底"""

    def _update_style(self):
        if self._selected:
            self.setStyleSheet(
                "WorkspaceCommitBox { background-color: #e3f2fd; border: 2px solid #1976d2; border-radius: 4px; }"
            )
        else:
            self.setStyleSheet(
                "WorkspaceCommitBox { background-color: #fafafa; border: 1px solid #e0e0e0; border-radius: 4px; }"
            )

    def set_merged(self):
        self.setStyleSheet(
            "WorkspaceCommitBox { background-color: #eeeeee; border: 1px solid #ccc; border-radius: 4px; }"
        )
        self.title_label.setStyleSheet("font-weight: bold; font-size: 12px; color: #999;")
        self.sub_label.setStyleSheet("color: #bbb; font-size: 11px;")


class FormalCommitBox(CommitBox):
    """正式 commit box — 蓝边，synced 时绿边"""

    def __init__(self, index: int, title: str, subtitle: str, parent=None):
        self._synced = False
        self._pushed = False
        super().__init__(index, title, subtitle, parent)

    def set_synced(self, val: bool):
        self._synced = val
        self._update_style()

    def set_pushed(self, val: bool):
        self._pushed = val
        self._update_style()

    def _update_style(self):
        if self._selected:
            bg = "#e3f2fd; border: 2px solid #1976d2"
        elif self._pushed:
            bg = "#e8f5e9; border: 1px solid #2e7d32"
        elif self._synced:
            bg = "#f1f8e9; border: 1px solid #4caf50"
        else:
            bg = "#e3f2fd; border: 1px solid #90caf9"
        self.setStyleSheet(
            f"FormalCommitBox {{ background-color: {bg}; border-radius: 4px; }}"
        )


# ── 后台工作线程 ──────────────────────────────────────────


class SyncWorker(QObject):
    """在后台线程执行同步操作"""

    progress = Signal(int, int, str)
    finished = Signal(bool, str)

    def __init__(self, workspace_path: str, backup_path: str,
                 config: Config, entries: list[FileEntry], msg: str):
        super().__init__()
        self.workspace_path = workspace_path
        self.backup_path = backup_path
        self.config = config
        self.entries = entries
        self.msg = msg

    @Slot()
    def run(self):
        def _progress(current, total, text=""):
            self.progress.emit(current, total, text)

        success = sync_to_backup(
            self.entries, self.msg,
            self.workspace_path, self.backup_path,
            _progress,
        )
        self.finished.emit(success, "同步完成" if success else "同步失败")


class ScanWorker(QObject):
    """后台扫描工作线程"""

    progress = Signal(int, int, str)
    finished = Signal(list, str)
    files_scanned = Signal(int)

    def __init__(self, config: Config, workspace_path: str, backup_path: str):
        super().__init__()
        self.config = config
        self.workspace_path = workspace_path
        self.backup_path = backup_path

    @Slot()
    def run(self):
        ws = Path(self.workspace_path).resolve()
        exclude = get_exclude_patterns(self.config, ws)

        def _progress(current, total, text=""):
            self.progress.emit(current, total, text)

        files = scan_workspace(ws, exclude)
        self.files_scanned.emit(len(files))

        if not files:
            self.finished.emit([], "工作区无文件")
            return

        if not self.backup_path:
            self.finished.emit([], "未配置备份路径")
            return

        entries = compare_files(ws, Path(self.backup_path), files, _progress)
        new = sum(1 for e in entries if e.status == "new")
        mod = sum(1 for e in entries if e.status == "modified")
        same = sum(1 for e in entries if e.status == "same")
        renamed = sum(1 for e in entries if e.status == "renamed")
        summary = f"对比完成: {new} 新增, {mod} 修改, {same} 相同, {renamed} 重命名"
        self.finished.emit(entries, summary)


class PushWorker(QObject):
    """后台 push 工作线程"""

    progress = Signal(int, int, str)
    finished = Signal(bool, str)

    def __init__(self, backup_path: str):
        super().__init__()
        self.backup_path = backup_path

    @Slot()
    def run(self):
        def _progress(current, total, text=""):
            self.progress.emit(current, total, text)

        success = push_to_backup(self.backup_path, progress_callback=_progress)
        self.finished.emit(success, "Push 成功" if success else "Push 失败")


# ── 项目列表面板 ────────────────────────────────────────────


class ProjectListPanel(QWidget):
    """项目列表首页 — 像微信好友列表，点击进入操作界面"""

    project_selected = Signal(object)  # ProjectConfig

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)

        title = QLabel("sync_tool — 项目列表")
        title.setStyleSheet("font-size: 20px; font-weight: bold; margin-bottom: 10px;")
        layout.addWidget(title)

        sub = QLabel("选择一个项目进入同步操作界面，或添加新项目")
        sub.setStyleSheet("color: gray; margin-bottom: 16px;")
        layout.addWidget(sub)

        # 项目表格
        self.project_table = QTableWidget()
        self.project_table.setColumnCount(4)
        self.project_table.setHorizontalHeaderLabels(["项目名", "工作区路径", "备份路径", "操作"])
        self.project_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.project_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.project_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.project_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.project_table.verticalHeader().setVisible(False)
        self.project_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.project_table.cellDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.project_table)

        # 按钮行
        btn_row = QHBoxLayout()
        self.add_btn = QPushButton("+ 添加项目")
        self.add_btn.clicked.connect(self._add_project)
        self.edit_btn = QPushButton("编辑")
        self.edit_btn.clicked.connect(self._edit_project)
        self.edit_btn.setEnabled(False)
        self.remove_btn = QPushButton("删除")
        self.remove_btn.clicked.connect(self._remove_project)
        self.remove_btn.setEnabled(False)
        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.edit_btn)
        btn_row.addWidget(self.remove_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # 选中行时启用编辑/删除按钮
        self.project_table.itemSelectionChanged.connect(self._on_selection_changed)

        self._refresh_table()

    def _refresh_table(self):
        self.project_table.setRowCount(len(self.config.projects))
        for i, p in enumerate(self.config.projects):
            name_item = QTableWidgetItem(p.name or "(未命名)")
            name_item.setData(Qt.UserRole, i)  # 存索引
            self.project_table.setItem(i, 0, name_item)
            self.project_table.setItem(i, 1, QTableWidgetItem(p.workspace_path or "(使用当前目录)"))
            self.project_table.setItem(i, 2, QTableWidgetItem(p.backup_path or "未设置"))

            enter_btn = QPushButton("进入")
            enter_btn.clicked.connect(lambda checked, row=i: self._enter_project(row))
            self.project_table.setCellWidget(i, 3, enter_btn)

    def _on_selection_changed(self):
        has_sel = len(self.project_table.selectedItems()) > 0
        self.edit_btn.setEnabled(has_sel)
        self.remove_btn.setEnabled(has_sel)

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
        dlg = _ProjectEditDialog(self)
        if dlg.exec() == QDialog.Accepted:
            pc = dlg.get_project()
            self.config.projects.append(pc)
            ConfigManager.save(self.config)
            self._refresh_table()

    def _edit_project(self):
        row = self._selected_row()
        if row is None:
            return
        pc = self.config.projects[row]
        dlg = _ProjectEditDialog(self, pc)
        if dlg.exec() == QDialog.Accepted:
            updated = dlg.get_project()
            self.config.projects[row] = updated
            ConfigManager.save(self.config)
            self._refresh_table()

    def _remove_project(self):
        row = self._selected_row()
        if row is None:
            return
        pc = self.config.projects[row]
        reply = QMessageBox.question(
            self, "确认删除",
            f"删除项目「{pc.name}」？\n（仅移除配置记录，不影响目录文件）",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.config.projects.pop(row)
            ConfigManager.save(self.config)
            self._refresh_table()


class _ProjectEditDialog(QDialog):
    """添加/编辑项目的对话框"""

    def __init__(self, parent=None, project: Optional[ProjectConfig] = None):
        super().__init__(parent)
        self.setWindowTitle("编辑项目" if project else "添加项目")
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如: MyApp")
        form.addRow("项目名:", self.name_edit)

        ws_layout = QHBoxLayout()
        self.ws_edit = QLineEdit()
        self.ws_edit.setPlaceholderText("工程版目录路径（留空使用当前目录）")
        ws_btn = QPushButton("浏览...")
        ws_btn.clicked.connect(lambda: self._browse(self.ws_edit))
        ws_layout.addWidget(self.ws_edit)
        ws_layout.addWidget(ws_btn)
        form.addRow("工作区路径:", ws_layout)

        bk_layout = QHBoxLayout()
        self.bk_edit = QLineEdit()
        self.bk_edit.setPlaceholderText("正式版备份仓库路径")
        bk_btn = QPushButton("浏览...")
        bk_btn.clicked.connect(lambda: self._browse(self.bk_edit))
        bk_layout.addWidget(self.bk_edit)
        bk_layout.addWidget(bk_btn)
        form.addRow("备份路径:", bk_layout)

        layout.addLayout(form)
        layout.addSpacing(10)

        btn_row = QHBoxLayout()
        self.ok_btn = QPushButton("确认")
        self.ok_btn.clicked.connect(self._on_ok)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(self.ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        if project:
            self.name_edit.setText(project.name)
            self.ws_edit.setText(project.workspace_path)
            self.bk_edit.setText(project.backup_path)
            self._original = project
        else:
            self._original = None

    def _browse(self, edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "选择目录")
        if d:
            edit.setText(d)

    def _on_ok(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "项目名不能为空")
            return
        self.accept()

    def get_project(self) -> ProjectConfig:
        pc = ProjectConfig(
            name=self.name_edit.text().strip(),
            workspace_path=self.ws_edit.text().strip(),
            backup_path=self.bk_edit.text().strip(),
        )
        # 保留原有项目的其他设置（commit_format, force_exclude, sync_base）
        if self._original:
            pc.commit_format = self._original.commit_format
            pc.force_exclude = self._original.force_exclude
            pc.sync_base = self._original.sync_base
        return pc


# ── 工作区操作面板 ────────────────────────────────────────────


class WorkspacePanel(QWidget):
    """项目的工作区操作界面 — 扫描/对比/commit/同步/push"""

    back_requested = Signal()

    def __init__(self, config: Config, project: ProjectConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.project = project
        self.entries: list[FileEntry] = []
        self.commits: list[CommitInfo] = []
        self.formal_commits: list[FormalCommit] = []
        self.selected_workspace: set[int] = set()
        self.selected_formal: int | None = None

        self._init_ui()

    # ── 界面构建 ────────────────────────────────────────────

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 顶部导航栏
        nav = QHBoxLayout()
        back_btn = QPushButton("← 返回项目列表")
        back_btn.clicked.connect(self.back_requested.emit)
        nav.addWidget(back_btn)

        proj_label = QLabel(f"当前项目: {self.project.name}")
        proj_label.setStyleSheet("font-weight: bold; font-size: 13px; margin-left: 10px;")
        nav.addWidget(proj_label)

        # 状态栏信息
        ws_txt = self.project.workspace_path or str(Path.cwd())
        bk_txt = self.project.backup_path or "未配置"
        status_info = QLabel(f"  工作区: {ws_txt}  |  备份: {bk_txt}")
        status_info.setStyleSheet("color: gray; font-size: 11px;")
        nav.addWidget(status_info, 1)
        layout.addLayout(nav)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #ddd;")
        layout.addWidget(sep)

        # 主内容区：左右分割
        splitter = QSplitter()

        # ===== 左侧: 文件列表 =====
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        file_label = QLabel("文件列表（勾选要同步的文件）")
        file_label.setStyleSheet("font-weight: bold;")
        left_layout.addWidget(file_label)

        self.file_table = QTableWidget()
        self.file_table.setColumnCount(4)
        self.file_table.setHorizontalHeaderLabels(["选择", "状态", "文件路径", "备注"])
        self.file_table.horizontalHeader().setStretchLastSection(True)
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.file_table.verticalHeader().setVisible(False)
        left_layout.addWidget(self.file_table)

        btn_row = QHBoxLayout()
        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.clicked.connect(self._select_all)
        self.deselect_all_btn = QPushButton("取消全选")
        self.deselect_all_btn.clicked.connect(self._deselect_all)
        self.filter_skip_btn = QPushButton("跳过相同文件")
        self.filter_skip_btn.clicked.connect(self._filter_skip_same)
        btn_row.addWidget(self.select_all_btn)
        btn_row.addWidget(self.deselect_all_btn)
        btn_row.addWidget(self.filter_skip_btn)
        btn_row.addStretch()
        left_layout.addLayout(btn_row)

        splitter.addWidget(left_widget)

        # ===== 右侧: 操作面板 =====
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # 步骤 1: 扫描
        scan_group = QGroupBox("步骤 1: 扫描与对比")
        scan_layout = QHBoxLayout(scan_group)
        self.scan_btn = QPushButton("扫描对比")
        self.scan_btn.clicked.connect(self._start_scan)
        scan_layout.addWidget(self.scan_btn)
        self.scan_status = QLabel("就绪")
        scan_layout.addWidget(self.scan_status, 1)
        right_layout.addWidget(scan_group)

        # 步骤 2: Commit 整合
        commit_group = QGroupBox("步骤 2: Commit 整合")
        commit_layout = QVBoxLayout(commit_group)

        ws_label = QLabel("工作区 Commits（点击选中，Ctrl/Shift 多选，然后合并）")
        ws_label.setStyleSheet("font-weight: bold; font-size: 11px;")
        commit_layout.addWidget(ws_label)

        ws_toolbar = QHBoxLayout()
        self.refresh_btn = QPushButton("刷新列表")
        self.refresh_btn.clicked.connect(self._refresh_workspace_boxes)
        self.merge_btn = QPushButton("合并选中为正式 Commit")
        self.merge_btn.clicked.connect(self._merge_selected)
        self.merge_btn.setEnabled(False)
        ws_toolbar.addWidget(self.refresh_btn)
        ws_toolbar.addWidget(self.merge_btn)
        ws_toolbar.addStretch()
        commit_layout.addLayout(ws_toolbar)

        self.ws_scroll = QScrollArea()
        self.ws_scroll.setWidgetResizable(True)
        self.ws_scroll.setMaximumHeight(180)
        self.ws_container = QWidget()
        self.ws_box_layout = QVBoxLayout(self.ws_container)
        self.ws_box_layout.setSpacing(4)
        self.ws_box_layout.setContentsMargins(0, 0, 0, 0)
        self.ws_box_layout.addStretch()
        self.ws_scroll.setWidget(self.ws_container)
        commit_layout.addWidget(self.ws_scroll)

        formal_label = QLabel("正式 Commits（选中后执行同步）")
        formal_label.setStyleSheet("font-weight: bold; font-size: 11px; margin-top: 6px;")
        commit_layout.addWidget(formal_label)

        formal_toolbar = QHBoxLayout()
        self.delete_formal_btn = QPushButton("删除选中")
        self.delete_formal_btn.clicked.connect(self._delete_selected_formal)
        self.delete_formal_btn.setEnabled(False)
        formal_toolbar.addWidget(self.delete_formal_btn)
        formal_toolbar.addStretch()
        commit_layout.addLayout(formal_toolbar)

        self.formal_scroll = QScrollArea()
        self.formal_scroll.setWidgetResizable(True)
        self.formal_scroll.setMaximumHeight(150)
        self.formal_container = QWidget()
        self.formal_box_layout = QVBoxLayout(self.formal_container)
        self.formal_box_layout.setSpacing(4)
        self.formal_box_layout.setContentsMargins(0, 0, 0, 0)
        self.formal_box_layout.addStretch()
        self.formal_scroll.setWidget(self.formal_container)
        commit_layout.addWidget(self.formal_scroll)

        right_layout.addWidget(commit_group)

        # 步骤 3: 执行
        exec_group = QGroupBox("步骤 3: 执行同步")
        exec_layout = QVBoxLayout(exec_group)

        self.progress_label = QLabel("就绪")
        exec_layout.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        exec_layout.addWidget(self.progress_bar)

        exec_btn_row = QHBoxLayout()
        self.sync_btn = QPushButton("Sync 到备份仓库")
        self.sync_btn.clicked.connect(self._start_sync)
        self.sync_btn.setEnabled(False)
        exec_btn_row.addWidget(self.sync_btn)

        self.push_btn = QPushButton("Push 到 GitHub")
        self.push_btn.clicked.connect(self._start_push)
        self.push_btn.setEnabled(False)
        exec_btn_row.addWidget(self.push_btn)

        self.config_btn = QPushButton("配置路径")
        self.config_btn.clicked.connect(self._edit_paths)
        exec_btn_row.addWidget(self.config_btn)

        exec_layout.addLayout(exec_btn_row)
        right_layout.addWidget(exec_group)

        # 日志
        log_label = QLabel("日志输出:")
        log_label.setStyleSheet("font-weight: bold; margin-top: 4px;")
        right_layout.addWidget(log_label)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumHeight(120)
        right_layout.addWidget(self.log_output)

        splitter.addWidget(right_widget)
        splitter.setSizes([450, 650])
        layout.addWidget(splitter)

    # ── 日志 ─────────────────────────────────────────────

    def _log(self, msg: str):
        self.log_output.appendPlainText(msg)
        cursor = self.log_output.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log_output.setTextCursor(cursor)

    # ── 文件列表操作 ─────────────────────────────────────

    def _populate_file_table(self, entries: list[FileEntry]):
        self.file_table.setRowCount(len(entries))
        for i, e in enumerate(entries):
            cb = QCheckBox()
            cb.setChecked(e.selected)
            cb.stateChanged.connect(lambda state, idx=i: self._toggle_file(idx, state))
            self.file_table.setCellWidget(i, 0, cb)

            status_colors = {
                "new": "green", "modified": "gold",
                "same": "gray", "renamed": "cyan",
            }
            status_item = QTableWidgetItem(e.status.upper())
            status_item.setForeground(status_colors.get(e.status, "white"))
            self.file_table.setItem(i, 1, status_item)

            path_item = QTableWidgetItem(e.rel_path)
            self.file_table.setItem(i, 2, path_item)

            note = ""
            if e.status == "renamed" and e.old_path:
                note = f"← {e.old_path}"
            elif e.status == "same":
                note = "内容相同"
            self.file_table.setItem(i, 3, QTableWidgetItem(note))

    def _toggle_file(self, idx: int, state: int):
        if 0 <= idx < len(self.entries):
            self.entries[idx].selected = bool(state)

    def _select_all(self):
        for e in self.entries:
            if e.status != "same":
                e.selected = True
        self._refresh_table_checks()

    def _deselect_all(self):
        for e in self.entries:
            e.selected = False
        self._refresh_table_checks()

    def _filter_skip_same(self):
        for e in self.entries:
            if e.status == "same":
                e.selected = False
        self._refresh_table_checks()

    def _refresh_table_checks(self):
        for i, e in enumerate(self.entries):
            w = self.file_table.cellWidget(i, 0)
            if w and isinstance(w, QCheckBox):
                w.blockSignals(True)
                w.setChecked(e.selected)
                w.blockSignals(False)

    # ── 扫描 ─────────────────────────────────────────────

    def _start_scan(self):
        self.scan_btn.setEnabled(False)
        self.scan_status.setText("扫描中...")
        self.progress_label.setText("正在扫描...")
        self.progress_bar.setValue(0)
        self._log("开始扫描工作区...")

        ws = self.project.workspace_path or str(Path.cwd())
        bk = self.project.backup_path

        self.scan_worker = ScanWorker(self.config, ws, bk)
        self.scan_thread = QThread()
        self.scan_worker.moveToThread(self.scan_thread)
        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_worker.finished.connect(self._on_scan_finished)
        self.scan_worker.progress.connect(self._on_scan_progress)
        self.scan_worker.finished.connect(self.scan_thread.quit)
        self.scan_worker.finished.connect(self.scan_worker.deleteLater)
        self.scan_thread.finished.connect(self.scan_thread.deleteLater)
        self.scan_thread.start()

    def _on_scan_progress(self, current: int, total: int, msg: str):
        if total > 0:
            self.progress_bar.setValue(int(current / total * 100))
        self.scan_status.setText(f"扫描: {current}/{total}")

    def _on_scan_finished(self, entries: list[FileEntry], summary: str):
        self.entries = entries
        self._populate_file_table(entries)
        self.scan_status.setText(summary)
        self.scan_btn.setEnabled(True)
        self._log(summary)
        self._log(f"待处理文件: {len(entries)}")
        self._refresh_workspace_boxes()

    # ── Workspace Box ────────────────────────────────────

    def _clear_box_layout(self, layout: QVBoxLayout):
        while layout.count() > 0:
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _refresh_workspace_boxes(self):
        ws = Path(self.project.workspace_path or Path.cwd()).resolve()
        commits = get_git_log(ws, self.project.sync_base or None)
        self.commits = commits

        self._clear_box_layout(self.ws_box_layout)

        if not commits:
            label = QLabel("  无新 commit")
            label.setStyleSheet("color: gray; padding: 8px;")
            self.ws_box_layout.addWidget(label)
            self._log("未检测到新 commit")
        else:
            for i, c in enumerate(commits):
                scope = f"({c.scope})" if c.scope else ""
                title = f"[{c.type}{scope}] {c.subject[:60]}"
                subtitle = f"{c.hash[:8]}"
                box = WorkspaceCommitBox(i, title, subtitle, self.ws_container)
                box.clicked.connect(self._on_workspace_box_clicked)
                self.ws_box_layout.addWidget(box)
            self._log(f"发现 {len(commits)} 个 workspace commit")
            self._update_workspace_box_styles()

        self.ws_box_layout.addStretch()

    def _on_workspace_box_clicked(self, index: int):
        modifiers = QApplication.keyboardModifiers()
        if modifiers == Qt.ControlModifier:
            if index in self.selected_workspace:
                self.selected_workspace.discard(index)
            else:
                self.selected_workspace.add(index)
        elif modifiers == Qt.ShiftModifier and self.selected_workspace:
            last = max(self.selected_workspace)
            start, end = (last, index) if last < index else (index, last)
            for i in range(start, end + 1):
                self.selected_workspace.add(i)
        else:
            self.selected_workspace = {index}
        self._update_workspace_box_styles()
        self.merge_btn.setEnabled(len(self.selected_workspace) >= 2)

    def _update_workspace_box_styles(self):
        for i in range(self.ws_box_layout.count()):
            item = self.ws_box_layout.itemAt(i)
            w = item.widget()
            if isinstance(w, WorkspaceCommitBox):
                w.selected = w._idx in self.selected_workspace

    # ── Formal Box ───────────────────────────────────────

    def _refresh_formal_boxes(self):
        self._clear_box_layout(self.formal_box_layout)
        if not self.formal_commits:
            label = QLabel("  暂无正式 commit")
            label.setStyleSheet("color: gray; padding: 8px;")
            self.formal_box_layout.addWidget(label)
        else:
            for i, fc in enumerate(self.formal_commits):
                header = fc.message.split("\n")[0]
                synced_str = "已同步" if fc.synced else "未同步"
                pushed_str = " 已推送" if fc.pushed else ""
                box = FormalCommitBox(i, header, f"{synced_str}{pushed_str}", self.formal_container)
                box.set_synced(fc.synced)
                box.set_pushed(fc.pushed)
                box.clicked.connect(self._on_formal_box_clicked)
                self.formal_box_layout.addWidget(box)
            self._update_formal_box_styles()
        self.formal_box_layout.addStretch()

    def _on_formal_box_clicked(self, index: int):
        if self.selected_formal == index:
            self.selected_formal = None
        else:
            self.selected_formal = index
        self._update_formal_box_styles()
        self.delete_formal_btn.setEnabled(self.selected_formal is not None)
        if self.selected_formal is not None:
            fc = self.formal_commits[self.selected_formal]
            self.sync_btn.setEnabled(not fc.synced)
        else:
            self.sync_btn.setEnabled(False)

    def _update_formal_box_styles(self):
        for i in range(self.formal_box_layout.count()):
            item = self.formal_box_layout.itemAt(i)
            w = item.widget()
            if isinstance(w, FormalCommitBox):
                w.selected = (w._idx == self.selected_formal)

    def _delete_selected_formal(self):
        if self.selected_formal is None:
            return
        idx = self.selected_formal
        fc = self.formal_commits[idx]
        reply = QMessageBox.question(
            self, "确认删除",
            f"删除正式 commit「{fc.message.split(chr(10))[0][:50]}」？\n（仅移除本地记录，不影响备份仓库）",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.formal_commits.pop(idx)
            self.selected_formal = None
            self.sync_btn.setEnabled(False)
            self.delete_formal_btn.setEnabled(False)
            self._refresh_formal_boxes()
            self._log("已删除正式 commit")

    # ── 合并 ─────────────────────────────────────────────

    def _merge_selected(self):
        if len(self.selected_workspace) < 2:
            return

        selected_indices = sorted(self.selected_workspace)
        selected_commits = [self.commits[i] for i in selected_indices]
        template = build_commit_template(selected_commits, self.config)

        dialog = QDialog(self)
        dialog.setWindowTitle("编辑正式 Commit Message")
        dialog.setMinimumSize(550, 350)
        dlg_layout = QVBoxLayout(dialog)
        dlg_layout.addWidget(QLabel("请编辑正式 commit message（首行格式: [PREFIX-N] type: subject）："))
        editor = QTextEdit()
        editor.setPlainText(template)
        dlg_layout.addWidget(editor)
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("确认")
        cancel_btn = QPushButton("取消")
        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        dlg_layout.addLayout(btn_layout)
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)

        if dialog.exec() == QDialog.Accepted:
            msg = editor.toPlainText().strip()
            err = validate_commit_message(msg)
            if err:
                QMessageBox.warning(self, "格式错误", err)
                return

            prefix = self.project.commit_format.get("prefix", "PROJ")
            number_start = self.project.commit_format.get("number_start", 0)
            max_n = number_start
            for fc in self.formal_commits:
                if fc.number > max_n:
                    max_n = fc.number
            repo_max = _find_next_number(self.project.backup_path, prefix)
            next_n = max(max_n, repo_max)

            fc = FormalCommit(
                message=msg, number=next_n, prefix=prefix,
                created_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            )
            self.formal_commits.append(fc)
            self._refresh_formal_boxes()
            self.selected_formal = len(self.formal_commits) - 1
            self._on_formal_box_clicked(self.selected_formal)
            self._log(f"正式 commit 已创建: [{prefix}-{fc.number}]")

            for i in range(self.ws_box_layout.count()):
                item = self.ws_box_layout.itemAt(i)
                w = item.widget()
                if isinstance(w, WorkspaceCommitBox) and w._idx in self.selected_workspace:
                    w.set_merged()
                    w.selected = False
            self.selected_workspace = set()
            self.merge_btn.setEnabled(False)

    # ── Sync ─────────────────────────────────────────────

    def _start_sync(self):
        if self.selected_formal is None:
            return

        selected_entries = [e for e in self.entries if e.selected]
        if not selected_entries:
            QMessageBox.warning(self, "提示", "没有选中任何文件，请在文件列表中勾选需要同步的文件")
            return

        fc = self.formal_commits[self.selected_formal]
        ws = self.project.workspace_path or str(Path.cwd())
        bk = self.project.backup_path

        self.sync_btn.setEnabled(False)
        self.push_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_label.setText("正在同步到备份仓库...")
        self._log("开始同步...")

        self.sync_worker = SyncWorker(ws, bk, self.config, self.entries, fc.message)
        self.sync_thread = QThread()
        self.sync_worker.moveToThread(self.sync_thread)
        self.sync_thread.started.connect(self.sync_worker.run)
        self.sync_worker.progress.connect(self._on_sync_progress)
        self.sync_worker.finished.connect(self._on_sync_finished)
        self.sync_worker.finished.connect(self.sync_thread.quit)
        self.sync_worker.finished.connect(self.sync_worker.deleteLater)
        self.sync_thread.finished.connect(self.sync_thread.deleteLater)
        self.sync_thread.start()

    def _on_sync_progress(self, current: int, total: int, msg: str):
        if total > 0:
            self.progress_bar.setValue(int(current / total * 100))
        if msg:
            self.progress_label.setText(msg)
            self._log(msg)

    def _on_sync_finished(self, success: bool, msg: str):
        self.progress_bar.setValue(100 if success else 0)
        self._log(msg)

        if success and self.selected_formal is not None:
            fc = self.formal_commits[self.selected_formal]
            fc.synced = True

            # 更新 sync_base
            ws = Path(self.project.workspace_path or Path.cwd()).resolve()
            try:
                result = subprocess.run(
                    ["git", "-C", str(ws), "rev-parse", "HEAD"],
                    capture_output=True, text=True, encoding="utf-8", timeout=15,
                )
                if result.returncode == 0:
                    self.project.sync_base = result.stdout.strip()
                    ConfigManager.save(self.config)
            except (subprocess.TimeoutExpired, OSError):
                pass

            self._refresh_formal_boxes()
            self.selected_formal = len(self.formal_commits) - 1
            self._on_formal_box_clicked(self.selected_formal)

            if any(fc.synced for fc in self.formal_commits):
                self.push_btn.setEnabled(True)
            self.progress_label.setText("同步成功！现在可以 Push 到 GitHub")
            QMessageBox.information(self, "同步成功", "同步完成！\n现在可以点击「Push 到 GitHub」推送远程。")
        else:
            self.progress_label.setText("同步失败，请检查日志")
            self.sync_btn.setEnabled(self.selected_formal is not None)
            QMessageBox.critical(self, "同步失败", "同步过程中出现错误，请检查日志")

    # ── Push ─────────────────────────────────────────────

    def _start_push(self):
        target = None
        for i, fc in enumerate(self.formal_commits):
            if fc.synced and not fc.pushed:
                target = i
                break
        if target is None:
            QMessageBox.information(self, "提示", "没有待 push 的正式 commit")
            return

        self.sync_btn.setEnabled(False)
        self.push_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_label.setText("正在 push 到远程...")
        self._log("开始 push...")

        self.push_worker = PushWorker(self.project.backup_path)
        self.push_thread = QThread()
        self.push_worker.moveToThread(self.push_thread)
        self.push_thread.started.connect(self.push_worker.run)
        self.push_worker.progress.connect(self._on_push_progress)
        self.push_worker.finished.connect(self._on_push_finished)
        self.push_worker.finished.connect(self.push_thread.quit)
        self.push_worker.finished.connect(self.push_worker.deleteLater)
        self.push_thread.finished.connect(self.push_thread.deleteLater)
        self.push_thread.start()

    def _on_push_progress(self, current: int, total: int, msg: str):
        if msg:
            self.progress_label.setText(msg)
            self._log(msg)

    def _on_push_finished(self, success: bool, msg: str):
        self.progress_bar.setValue(100 if success else 0)
        self.progress_label.setText(msg)
        self._log(msg)

        if success:
            for fc in self.formal_commits:
                if fc.synced and not fc.pushed:
                    fc.pushed = True
            self._refresh_formal_boxes()
            if self.selected_formal is not None:
                self._on_formal_box_clicked(self.selected_formal)
            QMessageBox.information(self, "Push 成功", "已推送到远程 GitHub！")
        else:
            QMessageBox.critical(self, "Push 失败", "请检查网络连接或远程仓库权限")

        self.sync_btn.setEnabled(
            self.selected_formal is not None
            and not self.formal_commits[self.selected_formal].synced
        )
        has_pending = any(fc.synced and not fc.pushed for fc in self.formal_commits)
        self.push_btn.setEnabled(has_pending)

    # ── 编辑路径 ─────────────────────────────────────────

    def _edit_paths(self):
        dlg = _ProjectEditDialog(self, self.project)
        if dlg.exec() == QDialog.Accepted:
            updated = dlg.get_project()
            self.project.name = updated.name
            self.project.workspace_path = updated.workspace_path
            self.project.backup_path = updated.backup_path
            self.project.commit_format = updated.commit_format
            ConfigManager.save(self.config)
            self._log("项目配置已更新")


# ── 主窗口 ────────────────────────────────────────────────


class MainWindow(QMainWindow):
    """主窗口 — 用 QStackedWidget 切项目列表 ↔ 工作区"""

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.workspace: WorkspacePanel | None = None

        self.setWindowTitle("sync_tool — 同步工具")
        self.setMinimumSize(1000, 650)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.project_list = ProjectListPanel(self.config)
        self.project_list.project_selected.connect(self._open_project)
        self.stack.addWidget(self.project_list)  # index 0

    def _open_project(self, project: ProjectConfig):
        # 移除旧的 workspace（如果有）
        if self.workspace:
            self.stack.removeWidget(self.workspace)
            self.workspace.deleteLater()

        self.workspace = WorkspacePanel(self.config, project)
        self.workspace.back_requested.connect(self._back_to_list)
        self.stack.addWidget(self.workspace)  # index 1
        self.stack.setCurrentIndex(1)

    def _back_to_list(self):
        if self.workspace:
            self.stack.removeWidget(self.workspace)
            self.workspace.deleteLater()
            self.workspace = None
        self.stack.setCurrentIndex(0)
        # 刷新项目列表（可能配置被修改了）
        self.project_list._refresh_table()


# ── 应用入口 ──────────────────────────────────────────────


def _fix_qt_env():
    """修复 Qt 环境：ANGLE (D3D11) 后端 + 路径配置，防止 Win11 segfault"""
    if not getattr(sys, 'frozen', False):
        return
    mei = getattr(sys, '_MEIPASS', None)
    if not mei:
        return

    pyside_dir = os.path.join(mei, "PySide6")
    plugin_dir = os.path.join(pyside_dir, "plugins")
    os.environ["QT_PLUGIN_PATH"] = plugin_dir
    os.environ["QT_QPA_PLATFORM"] = "windows"
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = os.path.join(plugin_dir, "platforms")
    if pyside_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = pyside_dir + os.pathsep + os.environ.get("PATH", "")

    os.environ["QT_OPENGL"] = "angle"
    os.environ["QT_ANGLE_PLATFORM"] = "d3d11"

    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseOpenGLES)
    except Exception:
        pass


def entry():
    _fix_qt_env()

    try:
        app = QApplication(sys.argv)
        app.setApplicationName("sync_tool")

        config = ConfigManager.load()

        window = MainWindow(config)
        window.show()
        sys.exit(app.exec())
    except Exception as e:
        import traceback
        log = Path(os.environ.get("TEMP", ".")) / "sync_tool_crash.log"
        log.write_text(
            f"sync_tool GUI crash at {datetime.now()}\n"
            f"{traceback.format_exc()}",
            encoding="utf-8",
        )
        if getattr(sys, "frozen", False):
            try:
                QMessageBox.critical(None, "sync_tool - 崩溃", str(e))
            except Exception:
                pass
        raise
