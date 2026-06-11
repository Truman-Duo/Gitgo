"""ExplorerMixin + _BranchLineStyle — 文件树、Diff、节点状态"""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPen
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
                               QProxyStyle, QStyle, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)
from backend.core.config import ProjectConfig
from backend.core import get_file_diff
from backend.core.i18n import _tr
from backend.core.scanner import FileScanner
from themes import get_theme


class _BranchLineStyle(QProxyStyle):
    """QTreeWidget 引导线绘制"""

    def drawPrimitive(self, element, option, painter, widget=None):
        if element == QStyle.PE_IndicatorBranch:
            super().drawPrimitive(element, option, painter, widget)
            painter.save()
            c = QColor(get_theme()["bdr2"])
            c.setAlpha(80)
            pen = QPen(c)
            pen.setWidth(1)
            painter.setPen(pen)
            xc = option.rect.center().x()
            yt = option.rect.top()
            yb = option.rect.bottom()
            ym = option.rect.center().y()

            # 检查 item 是否有子节点且已展开
            has_children = bool(option.state & QStyle.State_Children)
            item_expanded = False
            if widget:
                item = widget.itemAt(option.rect.center())
                if item and item.childCount() > 0:
                    item_expanded = item.isExpanded()

            if has_children and item_expanded:
                painter.drawLine(xc, ym, xc, yb)
            if option.state & QStyle.State_Sibling:
                painter.drawLine(xc, yt, xc, ym)
            if option.state & QStyle.State_Item:
                painter.drawLine(xc, ym, option.rect.right(), ym)
            painter.restore()
        else:
            super().drawPrimitive(element, option, painter, widget)


class ExplorerMixin:
    """文件树 + Diff 预览 + 节点状态面板"""

    def _active_tree(self):
        """返回当前活跃的文件树（内嵌或全局 LSB）"""
        return (self.state.file_tree
                or getattr(self.state, 'global_lsb_file_tree', None))

    def _active_diff_preview(self):
        """返回当前活跃的差异预览控件"""
        return (self.state.diff_preview
                or getattr(self.state, 'global_lsb_diff_preview', None))

    def _build_explorer_panel(self) -> QWidget:
        t = get_theme()
        w = QFrame()
        w.setObjectName("explorer_panel")
        w.setFrameShape(QFrame.Shape.NoFrame)
        w.setMinimumWidth(100)
        lo = QVBoxLayout(w)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(0)
        self.explorer_header = QLabel(_tr("explorer.header", "EXPLORER"))
        self.explorer_header.setObjectName("explorer_header")
        _f = self.explorer_header.font()
        _f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
        self.explorer_header.setFont(_f)
        lo.addWidget(self.explorer_header)
        self.state.file_tree = QTreeWidget()
        self.state.file_tree.setHeaderHidden(True)
        self.state.file_tree.setIndentation(16)
        self.state.file_tree.setAnimated(True)
        self.state.file_tree.setRootIsDecorated(True)
        self.state.file_tree.setStyle(_BranchLineStyle(self.state.file_tree.style()))
        self.state.file_tree.itemClicked.connect(self._on_tree_item_clicked)
        lo.addWidget(self.state.file_tree, 1)
        self.explorer_nodes = QWidget()
        self.explorer_nodes.setObjectName("explorer_nodes")
        nl = QVBoxLayout(self.explorer_nodes)
        nl.setContentsMargins(10, 5, 10, 5)
        nl.setSpacing(4)
        t = get_theme()
        for label, color in [
            (_tr("node.workspace", "· workspace"), t.blue),
            (_tr("node.release", "· release"), t.teal),
            (_tr("node.trial", "· trial"), t.amber),
        ]:
            row = QHBoxLayout()
            dot = QLabel("●")
            dot.setStyleSheet(f"color:{color};font-size:7px;")
            row.addWidget(dot)
            row.addWidget(QLabel(label))
            row.addStretch()
            nl.addLayout(row)
        lo.addWidget(self.explorer_nodes)
        return w

    def _auto_load_file_tree(self):
        try:
            scanner = FileScanner(self.state.project)
            tree = scanner.scan_tree()
            import sys
            print(f"[LOG] auto_load: found {len(tree)} top-level items, ws_path={self.state.project.workspace_path}", file=sys.stderr, flush=True)
            self._populate_file_tree_from_scanner(tree)
        except Exception as e:
            import sys
            print("[LOG] Explorer._auto_load_file_tree failed: " + str(e), file=sys.stderr, flush=True)
            import traceback
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(None, "auto_load 失败", f"{e}\n\n{traceback.format_exc()}")

    def _populate_file_tree_from_scanner(self, tree: list):
        file_tree = self._active_tree()
        if not file_tree:
            return
        file_tree.clear()

        def add_nodes(parent, entries):
            for entry in entries:
                item = QTreeWidgetItem([entry.name])
                item.setData(0, Qt.UserRole, entry.rel_path)
                if entry.is_dir:
                    item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
                if parent is file_tree:
                    file_tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                add_nodes(item, entry.children)

        add_nodes(file_tree, tree)

    def _make_section_header(self, title: str, badge_text: str = "",
                              badge_bg: str = "", badge_fg: str = "") -> QTreeWidgetItem:
        """创建带角标的 section header item"""
        t = get_theme()
        file_tree = self._active_tree()
        if not file_tree:
            return QTreeWidgetItem()
        item = QTreeWidgetItem([title])
        item.setData(0, Qt.UserRole, "section_header")
        item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
        item.setForeground(0, QColor(t.txt3))
        if badge_text:
            badge = QLabel(badge_text)
            badge.setStyleSheet(
                f"font-size:9px; font-weight:500; padding:1px 5px; border-radius:3px; "
                f"background:{badge_bg or t.bg3}; color:{badge_fg or t.txt3};")
            file_tree.setItemWidget(item, 0, self._make_header_row(title, badge))
        return item

    def _make_header_row(self, title: str, badge: QLabel) -> QWidget:
        t = get_theme()
        w = QWidget()
        lo = QHBoxLayout(w)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(4)
        lbl = QLabel(title)
        lbl.setStyleSheet(
            f"font-size:10px; font-weight:500; color:{t.txt3}; letter-spacing:0.4px;")
        lo.addWidget(lbl)
        lo.addWidget(badge)
        lo.addStretch()
        return w

    def add_incoming_section(self, change):
        """Accept 后追加 Incoming 文件区段到文件树"""
        t = get_theme()
        file_tree = self._active_tree()
        if not file_tree:
            return
        hdr = self._make_section_header(
            "Incoming", "trial",
            badge_bg=t.blue_bg, badge_fg=t.blue_txt,
        )
        file_tree.addTopLevelItem(hdr)
        # 填充文件标记
        item = QTreeWidgetItem(hdr, ["cherry-pick pending"])
        item.setData(0, Qt.UserRole, f"incoming:{change.hash[:7]}")
        badge = QLabel("IN")
        badge.setStyleSheet(
            f"font-size:9px; font-weight:500; padding:1px 4px; border-radius:3px; "
            f"background:{t.blue_bg}; color:{t.blue_txt};")
        file_tree.setItemWidget(item, 0, badge)
        item.setBackground(0, QColor(t.blue_bg))
        hdr.setExpanded(True)

    def _populate_file_tree(self):
        file_tree = self._active_tree()
        if not file_tree:
            return
        entries = self.state.session.entries
        if not entries:
            return
        status_map = {e.rel_path: e.status for e in entries}

        def update_node(item):
            path = item.data(0, Qt.UserRole)
            if path and path != "section_header" and path in status_map:
                status = status_map[path]
                badge = "N" if status == "new" else ("M" if status == "modified" else "")
                name = path.rsplit("/", 1)[-1] if "/" in path else path
                txt = f"{name}  {badge}" if badge else name
                item.setText(0, txt)
                if badge:
                    item.setForeground(0, QColor(get_theme().success_txt if badge == "N" else get_theme().amber_txt))
            for i in range(item.childCount()):
                update_node(item.child(i))

        for i in range(file_tree.topLevelItemCount()):
            update_node(file_tree.topLevelItem(i))

    def _on_tree_item_clicked(self, item, col):
        path = item.data(0, Qt.UserRole)
        if path:
            self._show_diff_by_path(path)

    def _show_diff_by_path(self, rel_path: str):
        diff_preview = self._active_diff_preview()
        if not diff_preview:
            return
        entry = next((e for e in self.state.session.entries if e.rel_path == rel_path), None)
        if not entry or not self.state.project.backup_path:
            diff_preview.setPlainText(
                _tr("file.diff_no_backup", "未配置备份路径") if not self.state.project.backup_path
                else _tr("file.no_diff", "（无差异）"))
            return
        diff_text = get_file_diff(entry, Path(self.state.project.workspace_path),
                                  Path(self.state.project.backup_path))
        diff_preview.setPlainText(diff_text or _tr("file.no_diff", "（无差异）"))

    def _update_node_status(self, project: ProjectConfig):
        t = get_theme()
        layouts = getattr(self.state, '_node_layouts', [])
        if not layouts:
            return

        nodes = [
            ("workspace", project.workspace_path or "—", t.blue),
            ("release", project.backup_path or "—", t.teal),
            ("trial", project.trial_path or _tr("trial.unconfigured", "unconfigured"), t.amber),
        ]

        for ncl in layouts:
            while ncl.count():
                item = ncl.takeAt(0)
                w = item.widget()
                if w:
                    w.deleteLater()
            ncl.addStretch()

            for label, path, color in nodes:
                row = QHBoxLayout()
                dot = QLabel("●")
                dot.setStyleSheet(f"color:{color};font-size:14px;")
                row.addWidget(dot)
                nm = QLabel(label)
                nm.setStyleSheet(f"font-size:11px;font-weight:bold;color:{t.txt2};")
                row.addWidget(nm)
                row.addStretch()
                ncl.insertLayout(ncl.count() - 1, row)

                path_lbl = QLabel(str(path))
                path_lbl.setStyleSheet(f"font-size:10px;color:{t.txt3};padding-left:20px;")
                path_lbl.setWordWrap(True)
                ncl.insertWidget(ncl.count() - 1, path_lbl)

                spacer = QWidget()
                spacer.setFixedHeight(8)
                ncl.insertWidget(ncl.count() - 1, spacer)
