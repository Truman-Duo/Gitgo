"""HistoryMixin — 时间线样式 / 颜色编码 / 点击跳转"""
import hashlib
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMessageBox,
                               QVBoxLayout, QWidget)
from backend.core.i18n import _tr
from themes import get_theme


# 项目取色调色板
_PROJECT_COLORS = [
    "#378add", "#1d9e75", "#c98b2a", "#e24b4a",
    "#8b5cf6", "#ec4899", "#14b8a6", "#f97316",
    "#6366f1", "#84cc16", "#06b6d4", "#d946ef",
]

_ACTION_COLORS = {
    "push":    lambda t: t.blue,
    "sync":    lambda t: t.success,
    "accept":  lambda t: t.teal,
    "promote": lambda t: t.amber,
    "discard": lambda t: t.txt3,
}


def _project_color(name: str) -> str:
    n = int(hashlib.md5(name.encode()).hexdigest()[:8], 16)
    return _PROJECT_COLORS[n % len(_PROJECT_COLORS)]


def _format_date(iso_ts: str) -> str:
    """从 ISO 时间戳提取日期部分"""
    if not iso_ts:
        return ""
    return iso_ts[:10]


def _format_time(iso_ts: str) -> str:
    """从 ISO 时间戳提取时间部分"""
    if not iso_ts:
        return ""
    return iso_ts[11:19] if len(iso_ts) >= 19 else iso_ts[11:16]


class HistoryMixin:
    """同步历史 — 时间线展示 + 颜色编码 + 点击跳转"""

    def _populate_history(self):
        self._clear_box_layout(self.state.hist_layout)
        t = get_theme()

        try:
            from backend.core.history import HistoryManager
            entries = HistoryManager.load()

            if not entries:
                self._hist_no_data(t)
                return

            entries = entries[-50:]  # 最多 50 条
            last_date = ""

            for he in reversed(entries):
                date = _format_date(he.timestamp)

                # ── 日期分隔头 ──
                if date != last_date:
                    last_date = date
                    sep = QLabel(f"  {date}")
                    sep.setStyleSheet(
                        f"font-size:11px;font-weight:600;color:{t.txt3};"
                        f"padding:10px 6px 4px;")
                    self.state.hist_layout.insertWidget(
                        self.state.hist_layout.count() - 1, sep)

                # ── 条目卡片 ──
                # 颜色：按 action_type 优先，fallback 到项目色
                if he.action_type and he.action_type in _ACTION_COLORS:
                    p_color = _ACTION_COLORS[he.action_type](t)
                else:
                    p_color = _project_color(he.project_name)
                card = QFrame()
                card.setCursor(Qt.PointingHandCursor)
                card.setStyleSheet(
                    f"QFrame{{background:{t.bg};border:.5px solid {t.bdr};"
                    f"border-left:3px solid {p_color};border-radius:5px;"
                    f"margin:2px 0;}}"
                    f"QFrame:hover{{background:{t.bg2};}}")
                cl = QVBoxLayout(card)
                cl.setContentsMargins(10, 8, 10, 8)
                cl.setSpacing(2)

                # 标题行
                hdr = QHBoxLayout()
                hdr.setSpacing(6)
                dot = QLabel("●")
                dot.setStyleSheet(f"color:{p_color};font-size:8px;")
                hdr.addWidget(dot)

                msg = he.commit_message or he.project_name
                msg_lbl = QLabel(f"<b>{msg[:70]}</b>")
                msg_lbl.setStyleSheet(f"font-size:11px;color:{t.txt};")
                hdr.addWidget(msg_lbl, 1)
                cl.addLayout(hdr)

                # 元数据行
                meta_parts = []
                meta_parts.append(
                    _tr("history.project", "{name}").format(name=he.project_name))
                if he.file_count:
                    meta_parts.append(
                        _tr("history.files", "{n} files").format(n=he.file_count))
                if he.commit_hash:
                    meta_parts.append(he.commit_hash[:8])
                time_str = _format_time(he.timestamp)
                if time_str:
                    meta_parts.append(time_str)

                meta_lbl = QLabel(" · ".join(meta_parts))
                meta_lbl.setStyleSheet(
                    f"font-size:10px;color:{t.txt3};padding-left:14px;")
                cl.addWidget(meta_lbl)

                # 点击跳转
                card.mousePressEvent = lambda event, pn=he.project_name: \
                    self._on_history_click(pn)

                self.state.hist_layout.insertWidget(
                    self.state.hist_layout.count() - 1, card)

        except Exception as e:
            self._hist_no_data(t, str(e))

    def _hist_no_data(self, t, detail: str = ""):
        msg = _tr("history.no_history", "No sync history")
        if detail:
            msg += f"\n({detail})"
        noop = QLabel(msg)
        noop.setStyleSheet(
            f"color:{t.txt3};font-size:12px;padding:20px;")
        self.state.hist_layout.insertWidget(
            self.state.hist_layout.count() - 1, noop)

    def _on_history_click(self, project_name: str):
        """点击历史条目 → 查找项目并打开"""
        if not self.state.main_window:
            return

        target = None
        for p in self.state.config.projects:
            if p.name == project_name:
                target = p
                break

        if target is None:
            QMessageBox.information(
                self,
                _tr("dialog.hint", "提示"),
                _tr("history.project_not_found",
                    "项目「{name}」不存在于当前配置").format(name=project_name))
            return

        self.state.main_window._open_project(target)
