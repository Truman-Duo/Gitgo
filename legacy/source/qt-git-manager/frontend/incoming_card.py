"""IncomingChangeCard — Incoming tab 的 trial commit 卡片"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect,
                               QLabel, QVBoxLayout)
from themes import get_theme


class IncomingChangeCard(QFrame):
    """三行结构：hash · title · meta + Done 态支持"""

    selected = Signal(int)

    def __init__(self, index: int, hash_short: str, title: str, meta: str, parent=None):
        super().__init__(parent)
        self._idx = index
        self._done = False
        self.setObjectName("inc_card")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(64)

        lo = QVBoxLayout(self)
        lo.setContentsMargins(10, 8, 10, 8)
        lo.setSpacing(2)

        self.hash_lbl = QLabel(hash_short)
        self.hash_lbl.setObjectName("inc_hash")
        lo.addWidget(self.hash_lbl)

        self.title_lbl = QLabel(title)
        self.title_lbl.setObjectName("inc_title")
        self.title_lbl.setWordWrap(True)
        lo.addWidget(self.title_lbl)

        self.meta_lbl = QLabel(meta)
        self.meta_lbl.setObjectName("inc_meta")
        lo.addWidget(self.meta_lbl)

        self._done_badge = None
        self._apply_style()

    def mousePressEvent(self, event):
        if not self._done:
            self.selected.emit(self._idx)
        super().mousePressEvent(event)

    def mark_done(self, action: str):
        """action: 'accepted' | 'promoted' | 'discarded'"""
        self._done = True
        self.setEnabled(False)
        effect = QGraphicsOpacityEffect(self)
        effect.setOpacity(0.45)
        self.setGraphicsEffect(effect)

        t = get_theme()
        colors = {
            "accepted":  (t.success_bg,  t.success_txt,  "✓ accepted"),
            "promoted":  (t.blue_bg,     t.blue_txt,     "↗ promoted"),
            "discarded": (t.danger_bg,   t.danger_txt,   "✕ discarded"),
        }
        bg, fg, text = colors.get(action, colors["discarded"])
        badge = QLabel(text)
        badge.setStyleSheet(
            f"font-size:9px; font-weight:500; padding:1px 5px;"
            f"border-radius:3px; background:{bg}; color:{fg};"
        )
        self.layout().addWidget(badge)
        self._done_badge = badge

    def _apply_style(self):
        t = get_theme()
        self.setStyleSheet(
            f"QFrame#inc_card {{"
            f"  background: {t.bg}; border: 0.5px solid {t.bdr}; border-radius: 5px;"
            f"}}"
            f"QLabel#inc_hash {{ font-size:10px; font-family:Courier New; color:{t.txt3}; background:transparent; }}"
            f"QLabel#inc_title {{ font-size:12px; color:{t.txt}; background:transparent; margin:2px 0; }}"
            f"QLabel#inc_meta {{ font-size:10px; color:{t.txt3}; background:transparent; }}"
        )

    def enterEvent(self, event):
        if not self._done:
            t = get_theme()
            self.setStyleSheet(
                f"QFrame#inc_card {{"
                f"  background: {t.bg2}; border: 0.5px solid {t.bdr2}; border-radius: 5px;"
                f"}}"
                f"QLabel#inc_hash {{ font-size:10px; font-family:Courier New; color:{t.txt3}; background:transparent; }}"
                f"QLabel#inc_title {{ font-size:12px; color:{t.txt}; background:transparent; margin:2px 0; }}"
                f"QLabel#inc_meta {{ font-size:10px; color:{t.txt3}; background:transparent; }}"
            )
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self._done:
            self._apply_style()
        super().leaveEvent(event)
