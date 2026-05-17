"""IncomingTabMixin — Incoming Tab 构建"""
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSplitter, QTabBar,
                               QTextEdit, QVBoxLayout, QWidget)
from themes import get_theme
from backend.core.i18n import _tr


class IncomingTabMixin:
    """Incoming Tab 构建：左侧 incoming 列表 + 右侧 Trial Zone"""

    def _refresh_incoming_styles(self):
        """主题切换后刷新 Incoming Tab 所有 inline 样式"""
        t = get_theme()
        # incoming dot
        if hasattr(self.state, 'incoming_dot'):
            self.state.incoming_dot.setStyleSheet(
                f"border-radius:2.5px; background:{t.amber_txt};")
        # bridge
        if hasattr(self.state, 'bridge_from'):
            self.state.bridge_from.setStyleSheet(
                f"font-size:10px; font-weight:500; padding:4px 8px; border-radius:4px; "
                f"background:{t.amber_bg}; color:{t.amber_txt}; border:0.5px solid {t.amber};")
        if hasattr(self.state, 'bridge_to'):
            self.state.bridge_to.setStyleSheet(
                f"font-size:10px; font-weight:500; padding:4px 8px; border-radius:4px; "
                f"background:{t.teal_bg}; color:{t.teal_txt}; border:0.5px solid {t.teal};")

    def _refresh_incoming_info_bar(self):
        """动态更新 Incoming 信息栏 — trial 节点地址"""
        trial = self.state.project.trial
        if trial is None:
            self.state.incoming_info_bar.setText(_tr("trial.unconfigured", "未配置试验区节点"))
            return
        fa = trial.file_access
        from backend.models import FileAccessKind
        if fa.kind == FileAccessKind.SSH:
            addr = f"{fa.user}@{fa.host}"
        else:
            addr = fa.path or _tr("trial.unconfigured", "未配置")
        self.state.incoming_info_bar.setText(f"trial · {addr}")

    def _setup_incoming_tab_button(self):
        """Incoming Tab: 橙色圆点放在 tab 文本右侧"""
        t = get_theme()
        self.state.incoming_dot = QLabel()
        self.state.incoming_dot.setFixedSize(5, 5)
        self.state.incoming_dot.setStyleSheet(f"border-radius:2.5px; background:{t.amber_txt};")
        self.state.incoming_dot.setVisible(False)
        self.state.tab_bar.setTabButton(1, QTabBar.ButtonPosition.RightSide, self.state.incoming_dot)

    def _build_incoming_tab(self) -> QWidget:
        from PySide6.QtCore import Qt as _Qt
        splitter = QSplitter(_Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(4)
        w = QWidget()
        lo = QHBoxLayout(w)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(0)
        left = QWidget()
        self.state.incoming_left_panel = left
        left.setObjectName("incoming_left_panel")
        left.setFixedWidth(240)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        hdr = QHBoxLayout()
        hdr.setContentsMargins(12, 8, 12, 8)
        hdr.addWidget(QLabel(f"<h3 style='font-size:13px;font-weight:500;margin:0'>{_tr('tab.incoming', 'Incoming')}</h3>"))
        self.state.trial_check_btn = QPushButton(_tr("trial.fetch", "Fetch"))
        self.state.trial_check_btn.setProperty("variant", "ghost")
        self.state.trial_check_btn.setStyleSheet("font-size:10px;")
        self.state.trial_check_btn.clicked.connect(self._check_trial)
        hdr.addWidget(self.state.trial_check_btn)
        ll.addLayout(hdr)
        self.state.incoming_info_bar = QLabel()
        self.state.incoming_info_bar.setObjectName("incoming_info_bar")
        self._refresh_incoming_info_bar()
        ll.addWidget(self.state.incoming_info_bar)
        self.state.trial_status = QLabel("")
        self.state.trial_status.setObjectName("trial_status")
        ll.addWidget(self.state.trial_status)
        self.state.trial_scroll = QScrollArea()
        self.state.trial_scroll.setWidgetResizable(True)
        self.state.trial_container = QWidget()
        self.state.trial_box_layout = QVBoxLayout(self.state.trial_container)
        self.state.trial_box_layout.setSpacing(4)
        self.state.trial_box_layout.setContentsMargins(6, 6, 6, 6)
        self.state.trial_box_layout.addStretch()
        self.state.trial_scroll.setWidget(self.state.trial_container)
        ll.addWidget(self.state.trial_scroll, 1)
        lo.addWidget(left)
        self.state.trial_zone = QWidget()
        self.state.trial_zone.setObjectName("trial_zone")
        tzl = QVBoxLayout(self.state.trial_zone)
        tzl.setContentsMargins(12, 12, 12, 12)
        tzl.setSpacing(8)
        self.state.trial_zone_label = QLabel(_tr("trial.zone.header", "TRIAL ZONE"))
        self.state.trial_zone_label.setObjectName("trial_zone_label")
        tzl.addWidget(self.state.trial_zone_label)
        self.state.trial_detail_card = QWidget()
        self.state.trial_detail_card.setObjectName("trial_detail_card")
        tdl = QVBoxLayout(self.state.trial_detail_card)
        tdl.setContentsMargins(0, 0, 0, 0)
        tdl.setSpacing(0)
        hdr2 = QWidget()
        self.state.trial_detail_hdr = hdr2
        hdr2.setObjectName("trial_detail_hdr")
        h2l = QVBoxLayout(hdr2)
        h2l.setContentsMargins(12, 10, 12, 10)
        self.state.trial_detail_title = QLabel("")
        self.state.trial_detail_title.setObjectName("trial_detail_title")
        self.state.trial_detail_meta = QLabel("")
        self.state.trial_detail_meta.setObjectName("trial_detail_meta")
        h2l.addWidget(self.state.trial_detail_title)
        h2l.addWidget(self.state.trial_detail_meta)
        tdl.addWidget(hdr2)
        self.state.trial_files_widget = QWidget()
        tfl = QVBoxLayout(self.state.trial_files_widget)
        tfl.setContentsMargins(12, 8, 12, 8)
        tfl.setSpacing(2)
        tdl.addWidget(self.state.trial_files_widget)
        # 三叉按钮区
        self.state.trial_action_widget = QWidget()
        ind_actions_lo = QHBoxLayout(self.state.trial_action_widget)
        ind_actions_lo.setContentsMargins(12, 10, 12, 10)
        ind_actions_lo.setSpacing(8)
        variant_map = {"accept": "success", "promote": "info", "discard": "danger"}
        for action in ("accept", "promote", "discard"):
            label = {
                "accept": "✓ " + _tr("trial.accept", "Accept"),
                "promote": "↗ " + _tr("trial.promote", "Promote"),
                "discard": "✕ " + _tr("trial.discard", "Discard"),
            }[action]
            btn = QPushButton(label)
            btn.setProperty("variant", variant_map[action])
            btn.clicked.connect(lambda checked, a=action: self._confirm_and_execute(a))
            ind_actions_lo.addWidget(btn, 1)
        tdl.addWidget(self.state.trial_action_widget)

        # Bridge 可视化（Accept 第一步）
        t = get_theme()
        self.state.bridge_widget = QWidget()
        bridge_lo = QVBoxLayout(self.state.bridge_widget)
        bridge_lo.setContentsMargins(12, 8, 12, 0)
        bridge_lo.setSpacing(6)
        bl = QLabel("合并路径")
        bl.setStyleSheet(f"font-size:10px; font-weight:500; color:{t.txt3};")
        bridge_lo.addWidget(bl)
        bridge_row = QWidget()
        br_lo = QHBoxLayout(bridge_row)
        br_lo.setContentsMargins(0, 0, 0, 0)
        br_lo.setSpacing(0)
        self.state.bridge_from = QLabel()
        self.state.bridge_from.setStyleSheet(
            f"font-size:10px; font-weight:500; padding:4px 8px; border-radius:4px; "
            f"background:{t.amber_bg}; color:{t.amber_txt}; border:0.5px solid {t.amber};")
        br_lo.addWidget(self.state.bridge_from)
        ba = QLabel("  →  ")
        ba.setStyleSheet(f"color:{t.txt3}; font-size:11px;")
        br_lo.addWidget(ba)
        self.state.bridge_to = QLabel("release · HEAD")
        self.state.bridge_to.setStyleSheet(
            f"font-size:10px; font-weight:500; padding:4px 8px; border-radius:4px; "
            f"background:{t.teal_bg}; color:{t.teal_txt}; border:0.5px solid {t.teal};")
        br_lo.addWidget(self.state.bridge_to)
        br_lo.addStretch()
        bridge_lo.addWidget(bridge_row)
        self.state.bridge_widget.setVisible(False)
        tdl.addWidget(self.state.bridge_widget)

        # 确认框（Accept 第二步）
        self.state.confirm_widget = QWidget()
        cf_lo = QVBoxLayout(self.state.confirm_widget)
        cf_lo.setContentsMargins(12, 8, 12, 10)
        cf_lo.setSpacing(6)
        cfl = QLabel("Release 提交信息")
        cfl.setStyleSheet(f"font-size:10px; color:{t.txt3};")
        cf_lo.addWidget(cfl)
        self.state.confirm_msg_box = QTextEdit()
        self.state.confirm_msg_box.setMaximumHeight(56)
        self.state.confirm_msg_box.setMinimumHeight(36)
        cf_lo.addWidget(self.state.confirm_msg_box)
        cf_btns = QHBoxLayout()
        cf_btns.setSpacing(6)
        self.state.confirm_ok_btn = QPushButton("✓ 确认 cherry-pick 到 release")
        self.state.confirm_ok_btn.setProperty("variant", "success")
        self.state.confirm_cancel_btn = QPushButton("取消")
        self.state.confirm_cancel_btn.setProperty("variant", "secondary")
        cf_btns.addWidget(self.state.confirm_ok_btn)
        cf_btns.addWidget(self.state.confirm_cancel_btn)
        cf_btns.addStretch()
        cf_lo.addLayout(cf_btns)
        self.state.confirm_widget.setVisible(False)
        tdl.addWidget(self.state.confirm_widget)

        tzl.addWidget(self.state.trial_detail_card)
        tzl.addStretch()
        lo.addWidget(self.state.trial_zone, 1)
        splitter.addWidget(w)
        splitter.addWidget(self._build_right_panel(with_diff=True))
        splitter.setSizes([600, 250])
        return splitter
