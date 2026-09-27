"""ThemeMixin — 主题颜色刷新"""
from themes import get_theme


class ThemeMixin:
    """主题刷新方法"""

    def _refresh_workshop_styles(self):
        """集中刷新 Workshop 底部按钮样式"""
        t = get_theme()
        if hasattr(self.state, 'progress_bar'):
            self.state.progress_bar.setStyleSheet(
                f"QProgressBar{{border:.5px solid {t.bdr2};border-radius:4px;background:{t.bg3};}}"
                f"QProgressBar::chunk{{background:{t.accent};border-radius:3px;}}"
            )
        if hasattr(self.state, 'progress_label'):
            self.state.progress_label.setStyleSheet(f"font-size:10px;color:{t.txt3};")

    def _apply_theme_colors(self):
        self._refresh_workshop_styles()

        # 主题切换时强制所有 CommitBox 重读 QSS（unpolish/polish）
        for layout_name in ("ws_box_layout", "fm_box_layout"):
            lo = getattr(self.state, layout_name, None)
            if lo is None:
                continue
            for i in range(lo.count()):
                item = lo.itemAt(i)
                w = item.widget()
                if w is not None:
                    w.style().unpolish(w)
                    w.style().polish(w)

        # Incoming Tab 样式刷新（bridge dot 等）
        if hasattr(self, '_refresh_incoming_styles'):
            self._refresh_incoming_styles()
