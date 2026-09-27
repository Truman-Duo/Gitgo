"""CommitBox v2 — 单一 QSS 驱动，状态通过 setProperty + polish 控制"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QMenu, QPushButton, QSizePolicy, QVBoxLayout
from themes import get_theme
from backend.core.i18n import _tr


class WorkspaceCommitBox(QFrame):
    """工作区 commit box — property 驱动，无 setStyleSheet / enterEvent / leaveEvent"""

    clicked = Signal(int)

    def __init__(self, index: int, commit_type: str, summary: str, meta: str, parent=None):
        super().__init__(parent)
        self._idx = index
        self._selected = False
        self._merged = False
        self.setObjectName("ws_card")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(80)

        lo = QVBoxLayout(self)
        lo.setContentsMargins(10, 6, 28, 6)
        lo.setSpacing(2)

        self.type_lbl = QLabel(commit_type.lower())
        self.type_lbl.setObjectName("ws_badge")
        lo.addWidget(self.type_lbl)

        self.summary_lbl = QLabel(summary)
        self.summary_lbl.setObjectName("ws_summary")
        self.summary_lbl.setWordWrap(True)
        lo.addWidget(self.summary_lbl)

        self.meta_lbl = QLabel(meta)
        self.meta_lbl.setObjectName("ws_meta")
        lo.addWidget(self.meta_lbl)

        self.cb = QLabel(self)
        self.cb.setObjectName("ws_check")
        self.cb.setFixedSize(14, 14)
        self.cb.setAlignment(Qt.AlignCenter)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.cb.move(self.width() - 21, 6)

    @property
    def selected(self) -> bool:
        return self._selected

    @selected.setter
    def selected(self, value: bool):
        self._selected = value
        self.setProperty("selected", value)
        self.cb.setText("✓" if value else "")
        self.cb.setProperty("checked", value)
        self._polish_all()

    def set_merged(self):
        self._merged = True
        self._selected = False
        self.setProperty("selected", False)
        self.setProperty("merged", True)
        self.cb.setText("")
        self.cb.setProperty("checked", False)
        self._polish_all()

    def _polish_all(self):
        for w in [self, self.type_lbl, self.summary_lbl, self.meta_lbl, self.cb]:
            w.style().unpolish(w)
            w.style().polish(w)

    def mousePressEvent(self, event):
        if not self._merged:
            self.clicked.emit(self._idx)
        super().mousePressEvent(event)


class FormalCommitBox(QFrame):
    """正式 commit box — QSS border-left 替代 QPainter 竖线，:hover 替代 enterEvent"""

    clicked = Signal(int)
    double_clicked = Signal(int, str)
    context_menu = Signal(int, str)

    def __init__(self, index: int, title: str, subtitle: str, parent=None):
        super().__init__(parent)
        self._idx = index
        self._selected = False
        self._synced = False
        self._pushed = False
        self._is_incoming = False
        self.setObjectName("fm_card")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(56)

        lo = QVBoxLayout(self)
        lo.setContentsMargins(13, 6, 28, 6)
        lo.setSpacing(2)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("fm_title")
        lo.addWidget(self.title_label)

        self.sub_label = QLabel(subtitle)
        self.sub_label.setObjectName("fm_sub")
        lo.addWidget(self.sub_label)

        t = get_theme()
        self.menu_btn = QPushButton("⋯", self)
        self.menu_btn.setFixedSize(18, 18)
        self.menu_btn.setCursor(Qt.PointingHandCursor)
        self.menu_btn.setStyleSheet(
            f"QPushButton {{ background:transparent; border:none; color:{t.txt3}; "
            f"font-size:13px; border-radius:3px; padding:0; }}"
            f"QPushButton:hover {{ background:{t.bg3}; color:{t.txt}; }}"
        )
        self.menu_btn.clicked.connect(self._show_menu_from_btn)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.menu_btn.move(self.width() - 22, 5)

    def _show_menu_from_btn(self):
        menu = QMenu(self)
        menu.addAction(_tr("commit.edit_message", "编辑消息"), lambda: self.context_menu.emit(self._idx, "edit_message"))
        menu.addAction(_tr("commit.edit_number", "编辑编号"), lambda: self.context_menu.emit(self._idx, "edit_number"))
        menu.addSeparator()
        menu.addAction(_tr("commit.dissolve", "Dissolve"), lambda: self.context_menu.emit(self._idx, "dissolve"))
        menu.addAction(_tr("commit.clear_sources", "Clear sources"), lambda: self.context_menu.emit(self._idx, "clear_sources"))
        menu.exec(self.menu_btn.mapToGlobal(self.menu_btn.rect().bottomLeft()))

    def _polish_all(self):
        for w in [self, self.title_label, self.sub_label]:
            w.style().unpolish(w)
            w.style().polish(w)

    def set_selected(self, value: bool):
        self._selected = value
        self.setProperty("selected", value)
        self._polish_all()

    def set_synced(self, value: bool):
        self._synced = value
        self.setProperty("synced", value)
        self._polish_all()

    def set_pushed(self, value: bool):
        self._pushed = value
        self.setProperty("pushed", value)
        self._polish_all()

    def set_incoming(self, value: bool):
        self._is_incoming = value
        self.setProperty("incoming", value)
        self._polish_all()

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit(self._idx, "message")
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        act_edit_msg = menu.addAction(_tr("commit.edit_message", "编辑消息"))
        act_edit_num = menu.addAction(_tr("commit.edit_number", "编辑编号"))
        menu.addSeparator()
        act_dissolve = menu.addAction(_tr("commit.dissolve_long", "Dissolve（恢复 Workspace commits）"))
        act_clear = menu.addAction(_tr("commit.clear_sources_long", "Clear sources（保留 Formal，清除来源）"))
        action = menu.exec(event.globalPos())
        if action == act_edit_msg:
            self.context_menu.emit(self._idx, "edit_message")
        elif action == act_edit_num:
            self.context_menu.emit(self._idx, "edit_number")
        elif action == act_dissolve:
            self.context_menu.emit(self._idx, "dissolve")
        elif action == act_clear:
            self.context_menu.emit(self._idx, "clear_sources")
