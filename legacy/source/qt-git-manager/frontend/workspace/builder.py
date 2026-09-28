"""BuilderMixin — 核心 UI 初始化 + Action Bar + Remotes/History Tab"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QScrollArea, QSplitter,
                               QStackedWidget, QTabBar, QVBoxLayout, QWidget)
from backend.core.i18n import _tr
from themes import get_theme

from .explorer import ExplorerMixin, _BranchLineStyle  # noqa: F401
from .workshop_tab import WorkshopTabMixin
from .incoming_tab import IncomingTabMixin
from .governance import GovernanceMixin


class BuilderMixin(ExplorerMixin, WorkshopTabMixin, IncomingTabMixin, GovernanceMixin):
    """UI 构建 — 继承 Explorer/Workshop/Incoming 三个子 Mixin"""

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.state.tab_bar = QTabBar()
        self.state.tab_bar.addTab(_tr("tab.workshop", "Commit workshop"))
        self.state.tab_bar.addTab(_tr("tab.incoming", "传入"))
        self._setup_incoming_tab_button()
        self.state.tab_bar.addTab(_tr("tab.remotes", "Remotes"))
        self.state.tab_bar.addTab(_tr("tab.history", "History"))
        self.state.tab_bar.addTab(_tr("tab.governance", "Governance"))
        layout.addWidget(self.state.tab_bar)

        self._build_action_bar(layout)

        self.state.tab_stack = QStackedWidget()
        self.state.tab_stack.addWidget(self._build_workshop_tab())
        self.state.tab_stack.addWidget(self._build_incoming_tab())
        self.state.tab_stack.addWidget(self._build_remotes_tab())
        self.state.tab_stack.addWidget(self._build_history_tab())
        self.state.tab_stack.addWidget(self._build_governance_tab())
        layout.addWidget(self.state.tab_stack, 1)

        self.state.tab_bar.currentChanged.connect(self._on_tab_changed)
        self.state.tab_bar.setCurrentIndex(0)
        self._update_action_bar()
        self._apply_theme_colors()

    def _on_tab_changed(self, idx: int):
        import sys
        tabs = ["Workshop", "Incoming", "Remotes", "History", "Governance"]
        print("[LOG] Tab.switch to=" + (tabs[idx] if idx < len(tabs) else str(idx)), file=sys.stderr, flush=True)
        self.state.tab_stack.setCurrentIndex(idx)
        self._update_action_bar()

    def _build_action_bar(self, layout):
        bar = QFrame()
        bar.setObjectName("action_bar")
        self.state.action_lo = QHBoxLayout(bar)
        self.state.action_lo.setContentsMargins(8, 2, 8, 2)
        self.state.action_lo.setSpacing(6)
        layout.addWidget(bar)

    def _update_action_bar(self):
        t = get_theme()
        idx = self.state.tab_bar.currentIndex()
        conf = [
            {"undo": "action.undo_merge",  "save": "action.save_draft",     "export": "action.export_tasks", "extra": ("action.re_scan", "↻")},
            {"undo": "action.undo_decision", "save": None,        "export": "action.export_list", "extra": ("action.re_fetch", "↻")},
            {"undo": None,          "save": None,              "export": None,           "extra": ("action.refresh_all", "↻")},
            {"undo": None,          "save": None,              "export": "action.export_history", "extra": ("action.filter", "△")},
            {"undo": None,          "save": None,              "export": None,           "extra": ("action.refresh", "↻")},
        ][idx]

        while self.state.action_lo.count():
            item = self.state.action_lo.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        def _btn(text, tooltip, cb):
            b = QPushButton(text)
            b.setToolTip(tooltip)
            b.setFlat(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setProperty("variant", "ghost")
            b.clicked.connect(cb)
            return b

        def _sep():
            s = QFrame()
            s.setFrameShape(QFrame.VLine)
            s.setFixedWidth(1)
            s.setStyleSheet(f"background:{t.bdr2};")
            return s

        if conf["undo"]:
            label = _tr(conf["undo"], conf["undo"])
            self.state.action_lo.addWidget(_btn(label, label, self._on_action_undo))
            self.state.action_lo.addWidget(_sep())
        if conf["save"]:
            label = _tr(conf["save"], conf["save"])
            self.state.action_lo.addWidget(_btn(label, label, self._on_action_save))
        if conf["export"]:
            label = _tr(conf["export"], conf["export"])
            self.state.action_lo.addWidget(_btn(label, label, self._on_action_export))
        if conf["extra"]:
            self.state.action_lo.addWidget(_sep())
            tr_key, _ = conf["extra"]
            label = _tr(tr_key, tr_key)
            self.state.action_lo.addWidget(_btn(label, label, self._on_action_extra))
        self.state.action_lo.addStretch()

    def _on_action_undo(self):
        idx = self.state.tab_bar.currentIndex()
        if idx == 0:
            if self.state.selected_formal is not None:
                self._dissolve_formal_commit(self.state.selected_formal)
            else:
                self._log(_tr("action.undo_none", "没有可撤销的正式 commit"))
        elif idx == 1:
            self._log(_tr("action.undo_decision", "Undo last trial decision"))

    def _on_action_save(self):
        if self.state.tab_bar.currentIndex() == 0:
            self._submit_commit_message()
        else:
            self._log(_tr("action.save_unavailable", "Save not available for this tab"))

    def _on_action_export(self):
        self._log(_tr("action.export", "Export — not implemented in UI"))

    def _on_action_extra(self):
        import sys
        idx = self.state.tab_bar.currentIndex()
        print("[LOG] ActionBar.extra tab=" + str(idx), file=sys.stderr, flush=True)
        if idx == 0:
            self._start_scan()
        elif idx == 1:
            self._check_trial()
        elif idx == 2:
            self._populate_remotes()
        elif idx == 3:
            self._log(_tr("action.filter", "Filter — not implemented in UI"))
        elif idx == 4:
            self._load_governance_data()

    # ── 共享右侧面板（Diff + Node）────────────────────────

    def _build_right_panel(self, with_diff: bool = True, store_refs: bool = False):
        """创建右侧 Diff + Node 垂直 QSplitter。store_refs=True 时存为全局引用（仅 Workshop 调用）"""
        from PySide6.QtGui import QFont
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(4)

        if with_diff:
            dp = QFrame()
            dp.setObjectName("diff_panel")
            dp.setFrameShape(QFrame.Shape.NoFrame)
            dp.setMinimumHeight(80)
            dl = QVBoxLayout(dp)
            dl.setContentsMargins(0, 0, 0, 0)
            dl.setSpacing(0)
            dh = QLabel(_tr("diff.header", "DIFF"))
            dh.setObjectName("diff_header")
            _f = dh.font()
            _f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
            dh.setFont(_f)
            dl.addWidget(dh)
            prev = QPlainTextEdit()
            prev.setFrameShape(QFrame.NoFrame)
            prev.setReadOnly(True)
            dl.addWidget(prev, 1)
            splitter.addWidget(dp)
            if store_refs:
                self.state.diff_panel = dp
                self.state.diff_header = dh
                self.state.diff_preview = prev

        np = QFrame()
        np.setObjectName("node_panel")
        np.setFrameShape(QFrame.Shape.NoFrame)
        np.setMinimumHeight(80)
        nl2 = QVBoxLayout(np)
        nl2.setContentsMargins(0, 0, 0, 0)
        nl2.setSpacing(0)
        nh = QLabel(_tr("node.header", "NODES"))
        nh.setObjectName("node_header")
        _f2 = nh.font()
        _f2.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
        nh.setFont(_f2)
        nl2.addWidget(nh)
        nc = QWidget()
        ncl = QVBoxLayout(nc)
        ncl.setContentsMargins(10, 8, 10, 8)
        ncl.setSpacing(6)
        ncl.addStretch()
        node_scroll = QScrollArea()
        node_scroll.setWidgetResizable(True)
        node_scroll.setWidget(nc)
        nl2.addWidget(node_scroll, 1)
        splitter.addWidget(np)
        # 所有 Tab 的节点面板都存入列表，供 _update_node_status 统一刷新
        if not hasattr(self.state, '_node_layouts'):
            self.state._node_layouts = []
        self.state._node_layouts.append(ncl)
        self.state.node_content = nc
        self.state.node_content_layout = ncl

        splitter.setSizes([150, 100] if with_diff else [250])
        return splitter

    # ── Remotes Tab ─────────────────────────────────────────

    def _build_remotes_tab(self) -> QWidget:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(4)
        w = QWidget()
        lo = QHBoxLayout(w)
        lo.setContentsMargins(12, 12, 12, 12)
        lo.setSpacing(8)
        self.state.remotes_container = QWidget()
        self.state.remotes_layout = QVBoxLayout(self.state.remotes_container)
        self.state.remotes_layout.setSpacing(8)
        self.state.remotes_layout.setContentsMargins(0, 0, 0, 0)
        self.state.remotes_layout.addStretch()
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self.state.remotes_container)
        lo.addWidget(sc, 1)
        splitter.addWidget(w)
        splitter.addWidget(self._build_right_panel(with_diff=False))
        splitter.setSizes([450, 200])
        self._populate_remotes()
        return splitter

    # ── History Tab ─────────────────────────────────────────

    def _build_history_tab(self) -> QWidget:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(4)
        w = QWidget()
        lo = QHBoxLayout(w)
        lo.setContentsMargins(12, 12, 12, 12)
        lo.setSpacing(8)
        self.state.hist_container = QWidget()
        self.state.hist_layout = QVBoxLayout(self.state.hist_container)
        self.state.hist_layout.setSpacing(0)
        self.state.hist_layout.setContentsMargins(0, 0, 0, 0)
        self.state.hist_layout.addStretch()
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self.state.hist_container)
        lo.addWidget(sc, 1)
        splitter.addWidget(w)
        splitter.addWidget(self._build_right_panel(with_diff=True))
        splitter.setSizes([450, 250])
        self._populate_history()
        return splitter
