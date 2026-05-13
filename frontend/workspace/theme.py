"""ThemeMixin — 主题颜色刷新"""
from themes import get_theme


class ThemeMixin:
    """主题刷新方法"""

    def _refresh_workshop_styles(self):
        """集中刷新 Workshop 底部按钮样式"""
        t = get_theme()
        btn_ss = (
            f"QPushButton{{font-size:11px;padding:3px 10px;border-radius:4px;"
            f"background:{t.bg2};border:.5px solid {t.bdr2};color:{t.txt2};}}"
            f"QPushButton:hover{{background:{t.blue_bg};border-color:{t.blue};color:{t.blue_txt};}}"
            f"QPushButton:disabled{{background:{t.bg3};color:{t.txt3};border-color:{t.bdr};}}"
        )
        del_btn_ss = (
            f"QPushButton{{font-size:13px;padding:2px 4px;border-radius:4px;"
            f"background:{t.bg2};border:.5px solid {t.bdr2};color:{t.txt3};}}"
            f"QPushButton:hover{{background:#3a1a1a;border-color:#d32f2f;color:#ef5350;}}"
            f"QPushButton:disabled{{background:{t.bg3};color:{t.txt3};border-color:{t.bdr};}}"
        )
        if hasattr(self.state, 'merge_btn'):       self.state.merge_btn.setStyleSheet(btn_ss)
        if hasattr(self.state, 'sync_btn'):        self.state.sync_btn.setStyleSheet(btn_ss)
        if hasattr(self.state, 'push_btn'):        self.state.push_btn.setStyleSheet(btn_ss)
        if hasattr(self.state, 'delete_formal_btn'): self.state.delete_formal_btn.setStyleSheet(del_btn_ss)
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
