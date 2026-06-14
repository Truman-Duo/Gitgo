"""GlobalLSB — 项目级左侧常驻面板：文件树 + 差异预览，跨 Tab 常驻"""
from pathlib import Path
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QFrame, QLabel, QPlainTextEdit,
                               QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)
from backend.core import get_file_diff
from backend.core.i18n import _tr
from backend.core.scanner import FileScanner
from themes import get_theme


class GlobalLSB(QWidget):
    """项目级左侧边栏：文件树 + 差异预览。

    通过 set_context() 切换上下文：
      - "workspace" → 显示 workspace 文件树
      - "incoming"  → 显示 trial 文件树
      - "hidden"    → 清空并隐藏文件树
    """

    file_clicked = Signal(str)  # rel_path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("global_lsb")
        self.setMinimumWidth(100)
        self.setFixedWidth(138)
        self._context = "workspace"
        self._init_ui()

    def _init_ui(self):
        t = get_theme()

        self.setStyleSheet(
            f"QWidget#global_lsb{{background:{t.bg};border-right:.5px solid {t.bdr};}}")

        lo = QVBoxLayout(self)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(0)

        # 文件树区
        explorer = QFrame()
        explorer.setObjectName("explorer_panel")
        explorer.setFrameShape(QFrame.Shape.NoFrame)
        el = QVBoxLayout(explorer)
        el.setContentsMargins(0, 0, 0, 0)
        el.setSpacing(0)

        self.header = QLabel(_tr("explorer.header", "EXPLORER"))
        self.header.setObjectName("explorer_header")
        _f = self.header.font()
        _f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
        self.header.setFont(_f)
        el.addWidget(self.header)

        self.file_tree = QTreeWidget()
        self.file_tree.setHeaderHidden(True)
        self.file_tree.setIndentation(16)
        self.file_tree.setAnimated(True)
        self.file_tree.setRootIsDecorated(True)
        from .workspace.explorer import _BranchLineStyle
        self.file_tree.setStyle(_BranchLineStyle(self.file_tree.style()))
        self.file_tree.itemClicked.connect(self._on_tree_item_clicked)
        el.addWidget(self.file_tree, 1)

        lo.addWidget(explorer, 1)

        # 差异预览区
        diff_panel = QFrame()
        diff_panel.setObjectName("diff_panel")
        diff_panel.setFrameShape(QFrame.Shape.NoFrame)
        diff_panel.setMinimumHeight(60)
        dl = QVBoxLayout(diff_panel)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(0)

        self.diff_header = QLabel(_tr("diff.header", "DIFF"))
        self.diff_header.setObjectName("diff_header")
        _f2 = self.diff_header.font()
        _f2.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
        self.diff_header.setFont(_f2)
        dl.addWidget(self.diff_header)

        self.diff_preview = QPlainTextEdit()
        self.diff_preview.setFrameShape(QFrame.NoFrame)
        self.diff_preview.setReadOnly(True)
        dl.addWidget(self.diff_preview, 1)

        lo.addWidget(diff_panel)

    # ── Context ───────────────────────────────────────────

    def set_context(self, context: str):
        self._context = context
        if context == "hidden":
            self.setVisible(False)
        else:
            self.setVisible(True)

    def context(self) -> str:
        return self._context

    # ── File tree loading ─────────────────────────────────

    def load_workspace_tree(self, project):
        """加载 workspace 文件树（完整扫描）"""
        from backend.core.scanner import FileScanner
        scanner = FileScanner(project)
        tree = scanner.scan_tree()
        self._populate_from_scanner(tree)
        self.setVisible(True)

    def load_trial_tree(self, project, incoming_changes: list):
        """加载 trial incoming 文件列表"""
        self.file_tree.clear()
        if not incoming_changes:
            return
        for ic in incoming_changes:
            item = QTreeWidgetItem([f"{ic.hash[:8]} {ic.message[:60]}"])
            item.setData(0, Qt.UserRole, f"incoming:{ic.hash[:7]}")
            self.file_tree.addTopLevelItem(item)

    def _populate_from_scanner(self, tree: list):
        self.file_tree.clear()

        def add_nodes(parent, entries):
            for entry in entries:
                item = QTreeWidgetItem([entry.name])
                item.setData(0, Qt.UserRole, entry.rel_path)
                if entry.is_dir:
                    item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
                if parent is self.file_tree:
                    self.file_tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                add_nodes(item, entry.children)

        add_nodes(self.file_tree, tree)

    # ── Diff ──────────────────────────────────────────────

    def _on_tree_item_clicked(self, item, col):
        path = item.data(0, Qt.UserRole)
        if path:
            self.file_clicked.emit(path)

    def show_diff(self, rel_path: str, entries: list, workspace_path: str,
                  backup_path: str):
        """显示指定文件的差异"""
        entry = next((e for e in entries if e.rel_path == rel_path), None)
        if not entry or not backup_path:
            self.diff_preview.setPlainText(
                _tr("file.diff_no_backup", "Backup path not configured")
                if not backup_path
                else _tr("file.no_diff", "(no diff)"))
            return
        diff_text = get_file_diff(entry, Path(workspace_path), Path(backup_path))
        self.diff_preview.setPlainText(diff_text or _tr("file.no_diff", "(no diff)"))
