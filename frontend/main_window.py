"""MainWindow — 主窗口"""
import os
import sys
from pathlib import Path
from PySide6.QtCore import Qt, QPropertyAnimation, QTimer
from PySide6.QtGui import QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout,
                               QLabel, QMessageBox, QMainWindow, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)
from backend.core.config import Config, ConfigManager, ProjectConfig
from backend.core.i18n import _tr, load_language, available_languages
from themes import get_theme, set_theme, get_qss
from .project_list import ProjectListPanel
from .workspace import WorkspacePanel
from .settings import SettingsDialog


def _detect_system_theme() -> str:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return "light" if value else "dark"
    except Exception:
        return "light"


class MainWindow(QMainWindow):
    """主窗口 — 全局导航 / QStackedWidget 切换 / 侧边栏 / 状态栏"""

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.workspace = None
        self._sidebar_open = True
        self._page_anim = None

        self.setWindowTitle(_tr("app.title", "gitgo — 同步工具"))
        self.setMinimumSize(800, 500)
        self.resize(1200, 750)
        ico_path = Path(__file__).parent / "gitgo_icon.png"
        if ico_path.exists():
            self.setWindowIcon(QIcon(str(ico_path)))
        self._apply_theme()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Toolbar ──
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(10, 4, 10, 4)
        self.breadcrumb = QLabel('<span style="font-weight:500;font-size:13px">gitgo</span>')
        self.breadcrumb.setTextFormat(Qt.RichText)
        self.breadcrumb.setOpenExternalLinks(False)
        self.breadcrumb.linkActivated.connect(self._on_breadcrumb_click)
        toolbar.addWidget(self.breadcrumb)
        toolbar.addStretch()
        self.state_pill = QLabel()
        self.state_pill.setVisible(False)
        toolbar.addWidget(self.state_pill)
        root.addLayout(toolbar)

        # ── separator ──
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setMaximumHeight(1)
        root.addWidget(sep)

        # ── Content: stack(center) + sidebar(right) ──
        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        self.stack = QStackedWidget()
        self.stack.setObjectName("content_area")
        content.addWidget(self.stack, 1)

        self.sidebar_wrap = QWidget()
        self.sidebar_wrap.setObjectName("sidebar_wrap")
        self.sidebar_wrap.setMinimumWidth(16)
        swl = QHBoxLayout(self.sidebar_wrap)
        swl.setContentsMargins(0, 0, 0, 0)
        swl.setSpacing(0)

        self.sidebar_toggle = QPushButton("❮")
        self.sidebar_toggle.setObjectName("sidebar_toggle")
        self.sidebar_toggle.setFixedWidth(0)
        self.sidebar_toggle.setFlat(True)
        self.sidebar_toggle.clicked.connect(self._toggle_sidebar)
        swl.addWidget(self.sidebar_toggle)

        self.settings_sidebar = QWidget()
        self.settings_sidebar.setObjectName("sidebar")
        self.settings_sidebar.setFixedWidth(45)
        sl = QVBoxLayout(self.settings_sidebar)
        sl.setContentsMargins(0, 6, 0, 6)
        sl.setAlignment(Qt.AlignTop)

        self.sidebar_collapse = QPushButton("❯")
        self.sidebar_collapse.setObjectName("sidebar_collapse")
        self.sidebar_collapse.setFlat(True)
        self.sidebar_collapse.clicked.connect(self._toggle_sidebar)
        sl.addWidget(self.sidebar_collapse)

        sl.addStretch()

        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setObjectName("sidebar_settings")
        self.settings_btn.setFlat(True)
        self.settings_btn.clicked.connect(self._open_settings)
        sl.addWidget(self.settings_btn)

        swl.addWidget(self.settings_sidebar)
        content.addWidget(self.sidebar_wrap)
        root.addLayout(content, 1)

        # ── Log bar ──
        self.log_bar = QLabel()
        self.log_bar.setVisible(False)
        root.addWidget(self.log_bar)

        # ── Status bar ──
        self.status_bar = QWidget()
        self.status_bar.setObjectName("status_bar")
        self.status_bar.setVisible(False)
        root.addWidget(self.status_bar)
        sb_layout = QHBoxLayout(self.status_bar)
        sb_layout.setContentsMargins(10, 3, 10, 3)
        self.sb_ws = QLabel()
        self.sb_bk = QLabel()
        self.sb_tl = QLabel()
        self.sb_branch = QLabel()
        sb_font = QFont("Courier New", 10)
        self.sb_branch.setFont(sb_font)
        sb_layout.addWidget(self.sb_ws)
        sb_layout.addWidget(QLabel("·"))
        sb_layout.addWidget(self.sb_bk)
        sb_layout.addWidget(QLabel("·"))
        sb_layout.addWidget(self.sb_tl)
        sb_layout.addStretch()
        sb_layout.addWidget(self.sb_branch)

        # ── Esc shortcut ──
        esc_s = QShortcut(QKeySequence("Escape"), self, self._back_to_list)
        esc_s.setContext(Qt.ApplicationShortcut)

        # ── Project list ──
        self.project_list = ProjectListPanel(self.config)
        self.project_list.project_selected.connect(self._open_project)
        self.stack.addWidget(self.project_list)

    def _on_breadcrumb_click(self, url):
        import sys
        print("[LOG] Breadcrumb.clicked", file=sys.stderr, flush=True)
        self._back_to_list()

    def _resolve_theme(self) -> str:
        if self.config.theme == "system":
            return _detect_system_theme()
        return self.config.theme

    def _apply_theme(self):
        resolved = self._resolve_theme()
        set_theme(resolved)
        app = QApplication.instance()
        app.setStyleSheet(get_qss(resolved))

    def _animate_page(self, target_widget: QWidget):
        if not self.config.animation:
            return
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        effect = QGraphicsOpacityEffect(target_widget)
        target_widget.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity")
        anim.setDuration(200)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        self._page_anim = anim
        def _safe_clear_effect(w):
            try:
                if w:
                    w.setGraphicsEffect(None)
            except RuntimeError:
                pass
        anim.finished.connect(lambda w=target_widget: _safe_clear_effect(w))
        anim.start()

    def _apply_theme_colors(self):
        t = get_theme()
        self.log_bar.setStyleSheet(
            f"background:{t.bg2};padding:4px 12px;font-size:11px;"
            f"font-family:'Courier New',monospace;color:{t.txt2};")
        self.status_bar.setStyleSheet(
            f"QWidget#status_bar{{background:{t.bg};border-top:.5px solid {t.bdr};"
            f"padding:3px 12px;font-size:11px;color:{t.txt2};}}")
        if self.workspace:
            name = self.workspace.project.name
            self.breadcrumb.setText(
                f'<a style="color:{t.accent};text-decoration:none" href="back">'
                f'{_tr("bc.all_projects", "所有项目")}</a>'
                f' <span style="color:{t.txt3};font-size:11px">›</span> '
                f'<strong style="color:{t.txt}">{name}</strong>')
        self._set_state("idle")
        if hasattr(self.project_list, 'add_row'):
            self.project_list.add_row.refresh_theme()

    def _set_state(self, state: str):
        if not hasattr(self, 'state_pill'):
            return
        t = get_theme()
        colors = {
            "idle": (t["pill_idle_bg"], t["pill_idle_fg"], _tr("state.idle", "就绪")),
            "scanning": (t["pill_busy_bg"], t["pill_busy_fg"], _tr("state.scanning", "扫描中")),
            "syncing": (t["pill_busy_bg"], t["pill_busy_fg"], _tr("state.syncing", "同步中")),
            "pushing": (t["pill_busy_bg"], t["pill_busy_fg"], _tr("state.pushing", "推送中")),
        }
        bg, fg, txt = colors.get(state, colors["idle"])
        self.state_pill.setStyleSheet(
            f"font-size:10px;padding:1px 6px;border-radius:3px;font-weight:500;"
            f"background:{bg};color:{fg};")
        self.state_pill.setText(txt)

    def _toggle_sidebar(self):
        import sys
        self._sidebar_open = not self._sidebar_open
        print("[LOG] Sidebar.toggle open=" + str(self._sidebar_open), file=sys.stderr, flush=True)
        if self._sidebar_open:
            self.settings_sidebar.setVisible(True)
            self.sidebar_toggle.setFixedWidth(0)
        else:
            self.settings_sidebar.setVisible(False)
            self.sidebar_toggle.setFixedWidth(24)

    def _open_settings(self):
        import sys
        print("[LOG] Settings.open", file=sys.stderr, flush=True)
        dialog = SettingsDialog(self, current_theme=self.config.theme, animation_enabled=self.config.animation)
        if dialog.exec() == QDialog.Accepted:
            theme = dialog.selected_theme
            animation = dialog.animation_enabled
            lang_code = dialog.selected_language
            print("[LOG] Settings.accepted theme=" + str(theme) + " lang=" + str(lang_code) + " anim=" + str(animation), file=sys.stderr, flush=True)
            self.config.theme = theme
            self._apply_theme()
            self._apply_theme_colors()
            if self.workspace:
                self.workspace._apply_theme_colors()
            self.config.animation = animation
            lang = lang_code
            if lang != self.config.language:
                self.config.language = lang
                load_language(lang)
                ConfigManager.save(self.config)
                QMessageBox.information(self, _tr("settings.title", "设置"), "语言切换将在下次启动时完全生效。\nLanguage change will fully apply on next restart.")
            else:
                ConfigManager.save(self.config)

    def _open_project(self, project: ProjectConfig):
        import sys, datetime as _dt
        _p = lambda m: print("[" + _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3] + "] OPEN: " + m, file=sys.stderr, flush=True)
        _p("start: " + project.name)
        err_log = Path.home() / "gitgo_open_project_error.log"
        try:
            err_log.write_text(f"[open_project] called for {project.name} at {_dt.datetime.now()}\n", encoding="utf-8")
        except Exception:
            pass
        try:
            if self.workspace:
                _p("cleaning old workspace")
                try:
                    self.workspace.back_requested.disconnect(self._back_to_list)
                except (TypeError, RuntimeError):
                    pass
                self.stack.removeWidget(self.workspace)
                self.workspace.hide()
                self.workspace.setParent(None)
                _p("old workspace cleaned")

            t = get_theme()
            self.breadcrumb.setText(
                f'<span style="font-size:12px;color:{t.txt3};font-weight:400">'
                f'{_tr("bc.all_projects", "所有项目")}</span>'
                f'<span style="color:{t.txt3};margin:0 5px;font-size:11px">/</span>'
                f'<span style="font-size:13px;font-weight:500;color:{t.txt}">{project.name}</span>')
            self.state_pill.setVisible(True)
            self.log_bar.setVisible(True)
            self.status_bar.setVisible(True)
            self._update_status_bar(project)

            self.workspace = WorkspacePanel(self.config, project, self)
            self._apply_theme_colors()  # after workspace creation, sets clickable breadcrumb
            self.workspace.back_requested.connect(self._back_to_list)
            self.stack.addWidget(self.workspace)
            self.stack.setCurrentIndex(1)
            self._animate_page(self.workspace)
            ws = self.workspace
            QTimer.singleShot(0, lambda w=ws: w and w._update_action_bar())
            print("[LOG] MainWindow._open_project: SUCCESS", file=sys.stderr, flush=True)
        except Exception:
            import traceback
            traceback.print_exc(file=sys.stderr)
            try:
                err_log.write_text(
                    f"[open_project] Error at {_dt.datetime.now()}:\n"
                    f"Error opening project {project.name}:\n"
                    f"{traceback.format_exc()}", encoding="utf-8")
            except Exception:
                pass

    def _back_to_list(self):
        import sys, datetime
        _p = lambda m: print("[" + datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3] + "] BACK: " + m, file=sys.stderr, flush=True)
        _p("START")
        if hasattr(self, '_page_anim') and self._page_anim:
            try:
                self._page_anim.stop()
            except RuntimeError:
                pass
            self._page_anim = None
        _p("anim_ok")
        if self.workspace:
            ws_ref = self.workspace
            self.workspace = None
            _p("got_ws_ref")
            if hasattr(ws_ref, 'commit_scroll'):
                try:
                    ws_ref.commit_scroll.verticalScrollBar().valueChanged.disconnect()
                except (TypeError, RuntimeError):
                    pass
            _p("signals_disconnected")
            self.stack.setCurrentIndex(0)
            _p("setCurrentIndex_0")
            self.stack.removeWidget(ws_ref)
            _p("removeWidget_ok")
            ws_ref.hide()
            ws_ref.setParent(None)
            _p("hidden_unparented")
        try:
            t = get_theme()
            self.breadcrumb.setText(
                f'<span style="font-weight:500;font-size:13px;color:{t.txt}">gitgo</span>')
            if hasattr(self, 'state_pill'):
                self.state_pill.setVisible(False)
            if hasattr(self, 'log_bar'):
                self.log_bar.setVisible(False)
            if hasattr(self, 'status_bar'):
                self.status_bar.setVisible(False)
            self.project_list._refresh_table()
            _p("ui_updated")
        except Exception:
            import traceback
            _p("ERROR: " + str(traceback.format_exc()))
        _p("END")

    def _update_status_bar(self, project: ProjectConfig):
        t = get_theme()
        ws_kind = "SSH" if project.workspace.file_access.kind.value == "ssh" else "local"
        bk_kind = "SSH" if project.release.file_access.kind.value == "ssh" else "local"
        tl_kind = "SSH" if (project.trial and project.trial.file_access.kind.value == "ssh") else "local"
        self.sb_ws.setText(f'<span style="color:{t.blue};font-size:8px">●</span> workspace · {ws_kind}')
        self.sb_bk.setText(f'<span style="color:{t.teal};font-size:8px">●</span> release · {bk_kind}')
        self.sb_tl.setText(f'<span style="color:{t.amber};font-size:8px">●</span> trial · {tl_kind}')
        self.sb_branch.setText("⎇ main")

    def _log_bar(self, msg: str):
        self.log_bar.setText(f"› {msg}")
