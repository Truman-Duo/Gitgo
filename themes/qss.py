"""QSS 动态生成 — 根据主题颜色令牌运行时插值"""


def build_qss(t) -> str:
    """根据 ThemeColors 实例生成完整 QSS 字符串"""
    return f"""
/* ── Root ───────────────────────────────────────────── */
QMainWindow, QWidget {{ background-color: {t.bg}; color: {t.txt}; }}
QStackedWidget#content_area {{ background-color: {t.bg}; }}

/* ── Buttons: base = ghost ──────────────────────────── */
QPushButton {{
    background: transparent; border: none; color: {t.txt3};
    padding: 2px 8px; border-radius: 4px; font-size: 11px;
}}
QPushButton:hover {{ background: {t.bg3}; color: {t.txt2}; }}
QPushButton[variant="ghost"] {{
    background: transparent; border: none; color: {t.txt3};
    font-size: 11px; padding: 2px 7px; border-radius: 3px;
}}
QPushButton[variant="ghost"]:hover {{
    background: {t.bg3}; color: {t.txt2};
}}
QPushButton:pressed {{ background: {t.bg3}; }}
QPushButton:disabled {{ color: {t.txt3}; }}

/* ── Buttons: secondary ─────────────────────────────── */
QPushButton[variant="secondary"] {{
    background: {t.bg2}; border: 0.5px solid {t.bdr2};
    color: {t.txt2}; padding: 4px 11px; border-radius: 5px; font-size: 12px;
}}
QPushButton[variant="secondary"]:hover {{ background: {t.blue_bg}; border-color: {t.blue}; color: {t.blue_txt}; }}
QPushButton[variant="secondary"]:disabled {{ opacity: 0.35; }}

/* ── Buttons: primary ───────────────────────────────── */
QPushButton[variant="primary"] {{
    background: {t.blue_bg}; border: 0.5px solid {t.blue_bdr};
    color: {t.blue_txt}; padding: 5px 13px;
    border-radius: 5px; font-size: 12px; font-weight: 500;
}}

/* ── Buttons: info / success / danger ────────────────── */
QPushButton[variant="info"] {{
    background: {t.blue_bg}; border: .5px solid {t.blue};
    color: {t.blue_txt}; font-weight: 500;
    border-radius: 8px; padding: 8px; font-size: 11px;
}}
QPushButton[variant="success"] {{
    background: {t.success_bg}; border: .5px solid {t.success};
    color: {t.success_txt}; font-weight: 500;
    border-radius: 8px; padding: 8px; font-size: 11px;
}}
QPushButton[variant="danger"] {{
    background: {t.danger_bg}; border: .5px solid {t.danger};
    color: {t.danger_txt}; font-weight: 500;
    border-radius: 8px; padding: 8px; font-size: 11px;
}}

QPushButton[variant="danger"][objectName="delete_btn"] {{
    font-size: 13px; padding: 2px 4px; border-radius: 4px;
}}
QPushButton[variant="danger"][objectName="delete_btn"]:hover {{
    background: {t.danger_bg}; border-color: {t.danger}; color: {t.danger_txt};
}}

/* ── Tab bar ────────────────────────────────────────── */
QTabBar {{ background: {t.bg}; border-bottom: 0.5px solid {t.bdr}; }}
QTabBar::tab {{
    background: transparent; color: {t.txt2};
    padding: 7px 14px; border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -0.5px; font-size: 12px;
}}
QTabBar::tab:selected {{
    color: {t.txt}; font-weight: 500;
    border-bottom: 2px solid {t.blue};
}}
QTabBar::tab:hover:!selected {{
    color: {t.txt}; background: {t.bg2};
}}

/* ── Scrollbar ──────────────────────────────────────── */
QScrollBar:vertical {{
    background: transparent; width: 6px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {t.bdr2}; border-radius: 3px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {t.txt3}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0; background: none;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
}}

/* ── Scroll Area ────────────────────────────────────── */
/* QScrollArea 不再设全局边框；各使用处通过 setFrameShape 自行控制 */

/* ── Splitter ───────────────────────────────────────── */
QSplitter::handle {{ background-color: {t.bdr}; }}

/* ── Frame / Separator ──────────────────────────────── */
QFrame[frameShape="4"] {{ color: {t.bdr}; border: none; max-height: 1px; }}
QFrame[frameShape="6"] {{ border: .5px solid {t.bdr}; border-radius: 4px; }}

/* ── Inputs ─────────────────────────────────────────── */
QPlainTextEdit {{ border: .5px solid {t.bdr}; border-radius: 4px; background-color: {t.bg_input}; color: {t.txt}; }}
QTextEdit {{ border: .5px solid {t.bdr}; border-radius: 4px; background-color: {t.bg_input}; color: {t.txt}; }}
QLineEdit {{ border: .5px solid {t.bdr2}; border-radius: 3px; padding: 4px; background-color: {t.bg_input}; color: {t.txt}; }}
QComboBox {{ border: .5px solid {t.bdr2}; border-radius: 3px; padding: 4px; background-color: {t.bg_input}; color: {t.txt}; min-width: 120px; }}
QComboBox::drop-down {{ border: none; }}
QComboBox QAbstractItemView {{ background-color: {t.bg}; color: {t.txt}; selection-background-color: {t.blue_bg}; }}

/* ── Labels / Checkbox ──────────────────────────────── */
QLabel {{ color: {t.txt}; }}
QCheckBox {{ color: {t.txt}; spacing: 4px; }}

/* ── Group / Table / Dialog / Progress ──────────────── */
QGroupBox {{ font-weight: bold; border: .5px solid {t.bdr2}; border-radius: 6px; margin-top: 10px; padding-top: 14px; color: {t.txt}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {t.accent}; }}
QTableWidget {{ border: .5px solid {t.bdr2}; gridline-color: {t.bdr}; }}
QTableWidget::item:selected {{ background-color: {t.blue_bg}; color: {t.txt}; }}
QHeaderView::section {{ background-color: {t.bg2}; border: .5px solid {t.bdr}; padding: 4px; color: {t.txt2}; font-weight: normal; }}
QDialog {{ background-color: {t.bg}; }}
QProgressBar {{ border: .5px solid {t.bdr2}; border-radius: 4px; text-align: center; height: 18px; color: {t.txt}; }}
QProgressBar::chunk {{ background-color: {t.accent}; border-radius: 3px; }}

/* ── Action bar ─────────────────────────────────────── */
QFrame#action_bar {{ background: {t.bg2}; border-bottom: .5px solid {t.bdr}; }}
QFrame#action_bar_sep {{ background: {t.bdr}; max-width: 1px; }}

/* ── Commit Canvas ──────────────────────────────────── */
QWidget#commit_canvas {{ background: {t.bg}; }}
QScrollArea#commit_scroll {{ border: none; background: {t.bg}; }}

/* ── Workspace Commit Box ──────────────────────────── */
QFrame#ws_card {{
    background: {t.bg}; border: 0.5px solid {t.bdr}; border-radius: 5px;
}}
QFrame#ws_card:hover {{
    background: {t.bg2}; border-color: {t.bdr2};
}}
QFrame#ws_card[selected="true"] {{
    background: {t.blue_bg}; border-color: {t.blue};
}}
QFrame#ws_card[merged="true"] {{
    background: {t.bg3}; border-color: {t.bdr2};
}}
QLabel#ws_badge {{
    font-size: 9px; font-weight: 500; color: {t.blue_txt}; background: transparent;
}}
QLabel#ws_summary {{
    font-size: 11px; color: {t.txt}; background: transparent; padding-right: 22px;
}}
QLabel#ws_meta {{
    font-size: 10px; color: {t.txt3}; background: transparent;
}}
QLabel#ws_check {{
    font-size: 10px; color: transparent; background: transparent;
}}
QLabel#ws_check[checked="true"] {{
    color: {t.blue_txt};
}}
QFrame#ws_card[merged="true"] QLabel#ws_badge,
QFrame#ws_card[merged="true"] QLabel#ws_summary,
QFrame#ws_card[merged="true"] QLabel#ws_meta {{
    color: {t.txt3};
}}

/* ── Formal Commit Box ─────────────────────────────── */
QFrame#fm_card {{
    background: {t.bg}; border: 0.5px solid {t.bdr}; border-radius: 5px;
    border-left-width: 3px; border-left-color: {t.blue};
}}
QFrame#fm_card:hover {{
    background: {t.bg2};
}}
/* synced — lowest state priority */
QFrame#fm_card[synced="true"] {{
    background: {t.success_bg};
    border-left-color: {t.success};
}}
/* pushed */
QFrame#fm_card[pushed="true"] {{
    background: {t.success_bg};
    border-left-color: {t.success_txt};
}}
/* incoming — overrides synced/pushed left border */
QFrame#fm_card[incoming="true"] {{
    border-left-color: {t.amber};
}}
/* selected — highest priority, must come last */
QFrame#fm_card[selected="true"] {{
    background: {t.bg}; border-color: {t.blue};
    border-left-color: {t.blue};
}}
QLabel#fm_title {{
    font-size: 12px; color: {t.txt}; background: transparent;
}}
QLabel#fm_sub {{
    font-size: 10px; color: {t.txt3}; background: transparent;
}}

/* ── Sidebar ────────────────────────────────────────── */
QWidget#sidebar_wrap {{ background: {t.bg2}; }}
QWidget#sidebar {{ background: {t.bg2}; border-left: .5px solid {t.bdr}; }}

/* ── Explorer panel ─────────────────────────────────── */
QFrame#explorer_panel {{ background: {t.bg}; border-right: 0.5px solid {t.bdr}; }}
QWidget#explorer_nodes {{ border-top: .5px solid {t.bdr}; padding: 5px 10px; font-size: 10px; color: {t.txt3}; }}
QLabel#explorer_header {{
    font-size: 10px; font-weight: 500; padding: 7px 10px;
    color: {t.txt3}; border-bottom: .5px solid {t.bdr}; background: {t.bg};
}}
QTreeWidget {{ border: none; padding: 4px 0; background: {t.bg}; }}
QTreeWidget::item {{ padding: 3px 10px; font-size: 12px; color: {t.txt2}; }}
QTreeWidget::item:hover {{ background: {t.bg2}; }}

/* ── Diff / Node panels ─────────────────────────────── */
QFrame#diff_panel {{ border-left: .5px solid {t.bdr}; background: {t.bg}; }}
QFrame#node_panel {{ border-top: .5px solid {t.bdr}; background: {t.bg}; }}
QLabel#diff_header, QLabel#node_header {{
    font-size: 10px; font-weight: 500; padding: 6px 10px;
    color: {t.txt3}; border-bottom: .5px solid {t.bdr}; background: {t.bg};
}}

/* ── Commit area (Workshop) ─────────────────────────── */
QLabel#ws_hdr {{
    font-size: 10px; font-weight: 500; color: {t.txt2};
    padding-left: 10px;
}}
QLabel#fm_hdr {{
    font-size: 10px; font-weight: 500; color: {t.txt2};
    padding-left: 16px;
}}
QLabel#msg_label {{
    font-size: 10px; color: {t.txt3};
}}
QLabel#sel_info {{
    font-size: 11px; color: {t.txt3};
}}

/* ── Incoming / Trial panel ─────────────────────────── */
QWidget#incoming_left_panel {{
    border-right: .5px solid {t.bdr}; background: {t.bg};
}}
QLabel#incoming_info_bar {{
    padding: 6px 12px; background: {t.bg2};
    border-bottom: .5px solid {t.bdr}; font-size: 11px; color: {t.txt2};
}}
QLabel#trial_status {{
    padding: 4px 12px; font-size: 10px; color: {t.txt3};
}}
QWidget#trial_zone {{ background: {t.bg}; }}
QWidget#trial_detail_card {{
    background: {t.bg}; border: .5px solid {t.bdr2};
    border-radius: 8px;
}}
QWidget#trial_detail_card:hover {{
    border-color: {t.bdr2}; background: {t.bg2};
}}
QWidget#trial_detail_hdr {{
    background: {t.bg2}; border-bottom: .5px solid {t.bdr};
}}
QLabel#trial_zone_label {{
    font-size: 12px; font-weight: 500; color: {t.txt2};
}}
QLabel#trial_detail_meta {{
    font-size: 11px; color: {t.txt2};
}}
QLabel#trial_detail_title {{
    font-size: 13px; font-weight: 500;
}}
"""
