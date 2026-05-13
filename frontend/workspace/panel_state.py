"""PanelState — WorkspacePanel 跨 Mixin 共享状态的显式容器。

每个字段标注了写入方（Producer）和消费方（Consumer），
未来修改时可直接从字段注释追踪影响范围。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import (QHBoxLayout, QLabel, QProgressBar,
                                    QScrollArea, QStackedWidget, QTabBar,
                                    QTextEdit, QTreeWidget, QVBoxLayout, QWidget)
    from backend.core.config import Config, ProjectConfig
    from backend.core.sync_session import SyncSession
    from ..workers import (ScanWorker, SyncWorker, PushWorker,
                           TrialCheckWorker, TriageWorker)
    from ..widgets import CommitCanvas


class PanelState:
    """WorkspacePanel 的所有跨 Mixin 共享状态。

    rule: self.state.xxx — 任何被 2+ 个 Mixin 访问的属性必须在此定义。
    """

    def __init__(self):
        # ── 业务状态 (P: panel.py) ──────────────────────────
        self.config: Config | None = None           # P: panel, C: all
        self.project: ProjectConfig | None = None   # P: panel, C: all
        self.main_window = None                     # P: panel, C: panel._log
        self.session: SyncSession | None = None     # P: panel, C: ALL mixins

        # ── 选中状态 ───────────────────────────────────────
        self.selected_formal: int | None = None     # P: panel.init, C: commits/syncpush/trial/panel
        self.selected_incoming: int | None = None   # P: panel.init, C: trial/incoming
        self._last_triage_action: str = ""          # P: trial, C: trial

        # ── 流程状态 ───────────────────────────────────────
        self._scan_done: bool = False               # P: panel, C: panel
        self._scanning: bool = False                # P: panel, C: builder._on_action_extra
        self._merging: bool = False                 # P: commits, C: commits
        self._merge_indices: set[int] = set()       # P: commits, C: commits

        # ── remote 数据 ────────────────────────────────────
        self._remote_data: dict = {}                # P: remotes, C: remotes

        # ── 后台 Worker (跨 mixin 生命周期管理) ────────────
        self.scan_worker: ScanWorker | None = None      # P: panel, C: panel
        self._scan_thread: QThread | None = None        # P: panel, C: panel
        self.sync_worker: SyncWorker | None = None      # P: syncpush, C: syncpush/panel
        self.sync_thread: QThread | None = None         # P: syncpush, C: syncpush/panel
        self.push_worker: PushWorker | None = None       # P: syncpush, C: syncpush/panel
        self.push_thread: QThread | None = None          # P: syncpush, C: syncpush/panel
        self.trial_worker: TrialCheckWorker | None = None   # P: trial, C: trial/panel
        self.trial_thread: QThread | None = None           # P: trial, C: trial/panel
        self.triage_worker: TriageWorker | None = None      # P: trial, C: trial/panel
        self.triage_thread: QThread | None = None           # P: trial, C: trial/panel

        # ── UI: Tab + Stack (P: builder, C: panel/builder) ──
        self.tab_bar: QTabBar | None = None           # P: builder._init_ui
        self.tab_stack: QStackedWidget | None = None  # P: builder._init_ui
        self.action_lo: QHBoxLayout | None = None     # P: builder._build_action_bar

        # ── UI: Workshop Tab 按钮 (P: workshop_tab, C: panel/syncpush/commits) ──
        self.merge_btn = None          # QPushButton
        self.sync_btn = None           # QPushButton
        self.push_btn = None           # QPushButton
        self.delete_formal_btn = None  # QPushButton
        self.progress_bar: QProgressBar | None = None
        self.progress_label: QLabel | None = None
        self.sel_info: QLabel | None = None

        # ── UI: Workshop Tab 消息区 (P: workshop_tab, C: commits) ──
        self.msg_box: QTextEdit | None = None
        self.msg_submit_btn = None     # QPushButton
        self.msg_label: QLabel | None = None

        # ── UI: Commit Canvas (P: workshop_tab, C: commits) ──
        self.commit_canvas: CommitCanvas | None = None
        self.commit_scroll: QScrollArea | None = None
        self.ws_hdr = None             # QWidget
        self.fm_hdr = None             # QWidget
        self.ws_box_layout: QVBoxLayout | None = None
        self.fm_box_layout: QVBoxLayout | None = None
        self.ws_container: QWidget | None = None
        self.fm_container: QWidget | None = None

        # ── UI: Explorer (P: explorer, C: panel) ─────────────
        self.file_tree: QTreeWidget | None = None
        self.diff_panel = None         # QFrame
        self.diff_header = None        # QLabel
        self.diff_preview = None       # QPlainTextEdit
        self._branch_style = None      # _BranchLineStyle

        # ── UI: Node 面板 (P: builder, C: panel) ─────────────
        self._node_layouts: list = []  # list[QVBoxLayout]
        self.node_content: QWidget | None = None
        self.node_content_layout: QVBoxLayout | None = None

        # ── UI: Remotes Tab (P: builder, C: remotes) ─────────
        self.remotes_container: QWidget | None = None
        self.remotes_layout: QVBoxLayout | None = None

        # ── UI: History Tab (P: builder, C: history) ─────────
        self.hist_container: QWidget | None = None
        self.hist_layout: QVBoxLayout | None = None

        # ── UI: Incoming Tab (P: incoming_tab, C: trial/incoming) ──
        self.incoming_dot: QLabel | None = None
        self.incoming_left_panel = None
        self.incoming_info_bar: QLabel | None = None
        self.trial_check_btn = None    # QPushButton
        self.trial_status: QLabel | None = None
        self.trial_scroll: QScrollArea | None = None
        self.trial_container: QWidget | None = None
        self.trial_box_layout: QVBoxLayout | None = None
        self.trial_zone: QWidget | None = None
        self.trial_zone_label: QLabel | None = None
        self.trial_detail_card: QWidget | None = None
        self.trial_detail_hdr = None
        self.trial_detail_title: QLabel | None = None
        self.trial_detail_meta: QLabel | None = None
        self.trial_files_widget: QWidget | None = None
        self.trial_action_widget: QWidget | None = None
        self.bridge_widget: QWidget | None = None
        self.bridge_from: QLabel | None = None
        self.bridge_to: QLabel | None = None
        self.confirm_widget: QWidget | None = None
        self.confirm_msg_box: QTextEdit | None = None
        self.confirm_ok_btn = None     # QPushButton
        self.confirm_cancel_btn = None # QPushButton
        self._incoming_setup_done: bool = False

        # ── Misc (P: workshop_tab, C: commits) ───────────────
        self._line_timer = None        # QTimer
