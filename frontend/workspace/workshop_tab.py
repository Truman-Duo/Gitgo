"""WorkshopTabMixin — Workshop Tab + 底部操作行"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar,
                               QPushButton, QScrollArea, QSplitter,
                               QTextEdit, QVBoxLayout, QWidget)
from backend.core.i18n import _tr


class WorkshopTabMixin:
    """Workshop Tab 构建：三栏 QSplitter + CommitCanvas + 消息区"""

    def _build_workshop_tab(self) -> QWidget:
        from ..widgets import CommitCanvas
        from themes import get_theme

        w = QWidget()
        lo = QHBoxLayout(w)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(0)
        self.ws_hsplitter = QSplitter(Qt.Horizontal)
        self.ws_hsplitter.setChildrenCollapsible(False)
        self.ws_hsplitter.setHandleWidth(4)
        self.ws_hsplitter.addWidget(self._build_explorer_panel())
        self.center_splitter = QSplitter(Qt.Vertical)
        self.center_splitter.setChildrenCollapsible(False)
        self.center_splitter.setHandleWidth(4)

        # 提交卡片区
        commit_frame = QFrame()
        commit_frame.setFrameShape(QFrame.Shape.NoFrame)
        commit_frame.setMinimumHeight(100)
        commit_fl = QVBoxLayout(commit_frame)
        commit_fl.setContentsMargins(6, 4, 6, 4)
        commit_fl.setSpacing(4)

        # 统一 commit canvas（内置标题行，与 columns 共享同一 widget 树，天然对齐）
        self.state.commit_scroll = QScrollArea()
        self.state.commit_scroll.setObjectName("commit_scroll")
        self.state.commit_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.state.commit_scroll.setWidgetResizable(True)
        self.state.commit_canvas = CommitCanvas()
        self.state.commit_canvas.ws_hdr.setText(_tr("ws.header", "Workspace commits"))
        self.state.commit_canvas.fm_hdr.setText(_tr("fm.header", "Formal commits"))
        self.state.commit_scroll.setWidget(self.state.commit_canvas)
        commit_fl.addWidget(self.state.commit_scroll, 1)

        # 引用
        self.state.ws_hdr = self.state.commit_canvas.ws_hdr
        self.state.fm_hdr = self.state.commit_canvas.fm_hdr
        self.state.ws_box_layout = self.state.commit_canvas.ws_layout
        self.state.fm_box_layout = self.state.commit_canvas.fm_layout
        self.state.ws_container = self.state.commit_canvas.ws_column
        self.state.fm_container = self.state.commit_canvas.fm_column
        self.ws_scroll = self.state.commit_scroll
        self.fm_scroll = self.state.commit_scroll
        self.center_splitter.addWidget(commit_frame)

        # 消息区
        msg_frame = QFrame()
        msg_frame.setFrameShape(QFrame.Shape.NoFrame)
        msg_frame.setMinimumHeight(24)
        msg_fl = QVBoxLayout(msg_frame)
        msg_fl.setContentsMargins(6, 1, 6, 0)
        msg_fl.setSpacing(0)
        self.state.msg_label = QLabel(_tr("commit.msg_label", "Commit message"))
        self.state.msg_label.setObjectName("msg_label")
        msg_fl.addWidget(self.state.msg_label)
        msg_row = QHBoxLayout()
        msg_row.setSpacing(4)
        self.state.msg_box = QTextEdit()
        self.state.msg_box.setPlaceholderText(_tr("commit.placeholder", "输入提交信息..."))
        self.state.msg_box.setMinimumHeight(24)
        msg_row.addWidget(self.state.msg_box, 1)
        self.state.msg_submit_btn = QPushButton(_tr("commit.submit", "提交"))
        self.state.msg_submit_btn.setProperty("variant", "primary")
        self.state.msg_submit_btn.clicked.connect(self._submit_commit_message)
        msg_row.addWidget(self.state.msg_submit_btn)
        msg_fl.addLayout(msg_row, 1)  # stretch=1 让 textbox 随 splitter 拉伸
        self.center_splitter.addWidget(msg_frame)
        self.center_splitter.setSizes([360, 60])

        ctr_widget = QWidget()
        ctr_layout = QVBoxLayout(ctr_widget)
        ctr_layout.setContentsMargins(6, 4, 6, 4)
        ctr_layout.setSpacing(4)
        ctr_layout.addWidget(self.center_splitter, 1)
        self._build_workshop_bottom_row(ctr_layout)
        self.ws_hsplitter.addWidget(ctr_widget)

        self.ws_hsplitter.addWidget(self._build_right_panel(with_diff=True, store_refs=True))
        self.ws_hsplitter.setStretchFactor(0, 0)
        self.ws_hsplitter.setStretchFactor(1, 1)
        self.ws_hsplitter.setStretchFactor(2, 0)
        self.ws_hsplitter.setSizes([138, 600, 250])
        lo.addWidget(self.ws_hsplitter)

        # 滚动 / resize 联动 — 去抖 timer，避免多次排队
        from PySide6.QtCore import QTimer
        self.state._line_timer = QTimer()
        self.state._line_timer.setSingleShot(True)
        self.state._line_timer.setInterval(16)
        self.state._line_timer.timeout.connect(self._refresh_commit_lines)
        self.state.commit_scroll.verticalScrollBar().valueChanged.connect(self.state._line_timer.start)
        self.state.commit_canvas._line_refresh_cb = self._refresh_commit_lines

        return w

    def _build_workshop_bottom_row(self, ctr_layout):
        from themes import get_theme
        t = get_theme()
        row = QFrame()
        lo = QHBoxLayout(row)
        lo.setContentsMargins(0, 4, 0, 4)
        lo.setSpacing(6)

        self.state.merge_btn = QPushButton(_tr("action.merge", "合并"))
        self.state.merge_btn.setEnabled(False)
        self.state.merge_btn.setProperty("variant", "secondary")
        self.state.merge_btn.clicked.connect(self._merge_selected)
        lo.addWidget(self.state.merge_btn)

        self.state.sync_btn = QPushButton(_tr("action.sync", "Sync"))
        self.state.sync_btn.setEnabled(False)
        self.state.sync_btn.setProperty("variant", "secondary")
        self.state.sync_btn.clicked.connect(self._start_sync)
        lo.addWidget(self.state.sync_btn)

        self.state.push_btn = QPushButton(_tr("action.push", "Push"))
        self.state.push_btn.setEnabled(False)
        self.state.push_btn.setProperty("variant", "secondary")
        self.state.push_btn.clicked.connect(self._start_push)
        lo.addWidget(self.state.push_btn)

        self.state.delete_formal_btn = QPushButton("✕")
        self.state.delete_formal_btn.setFixedWidth(26)
        self.state.delete_formal_btn.setEnabled(False)
        self.state.delete_formal_btn.setProperty("variant", "secondary")
        self.state.delete_formal_btn.clicked.connect(self._delete_selected_formal)
        self.state.delete_formal_btn.setToolTip(_tr("action.delete_formal", "删除选中的正式 commit"))
        lo.addWidget(self.state.delete_formal_btn)

        self.state.progress_bar = QProgressBar()
        self.state.progress_bar.setFixedHeight(8)
        self.state.progress_bar.setTextVisible(False)
        self.state.progress_bar.setFixedWidth(120)
        lo.addWidget(self.state.progress_bar)

        self.state.progress_label = QLabel("")
        lo.addWidget(self.state.progress_label)
        lo.addStretch()

        self.state.sel_info = QLabel("")
        self.state.sel_info.setObjectName("sel_info")
        lo.addWidget(self.state.sel_info)

        ctr_layout.addWidget(row)
