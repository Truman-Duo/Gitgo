"""GUI 桌面界面 — 薄入口，逻辑在 frontend/ 包中"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from frontend import MainWindow
from backend.core.config import Config, ConfigManager
from backend.core.i18n import load_language
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication


def _fix_qt_env():
    """修复 PyInstaller 打包后的 ANGLE/D3D 问题"""
    pass  # Qt 平台插件自动处理


# ── 应用入口 ──────────────────────────────────────────────


def _fix_qt_env():
    """修复 Qt 环境：ANGLE (D3D11) 后端 + 路径配置，防止 Win11 segfault"""
    if not getattr(sys, 'frozen', False):
        return
    mei = getattr(sys, '_MEIPASS', None)
    if not mei:
        return

    pyside_dir = os.path.join(mei, "PySide6")
    plugin_dir = os.path.join(pyside_dir, "plugins")
    os.environ["QT_PLUGIN_PATH"] = plugin_dir
    os.environ["QT_QPA_PLATFORM"] = "windows"
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = os.path.join(plugin_dir, "platforms")
    if pyside_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = pyside_dir + os.pathsep + os.environ.get("PATH", "")

    os.environ["QT_OPENGL"] = "angle"
    os.environ["QT_ANGLE_PLATFORM"] = "d3d11"

    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseOpenGLES)
    except Exception:
        pass


def entry():
    _fix_qt_env()

    try:
        app = QApplication(sys.argv)
        app.setApplicationName("gitgo")
        ico_path = Path(__file__).parent / "gitgo_icon.png"
        if ico_path.exists():
            app.setWindowIcon(QIcon(str(ico_path)))

        config = ConfigManager.load()
        if config.language:
            from backend.core.i18n import load_language
            load_language(config.language)

        window = MainWindow(config)
        window.show()
        sys.exit(app.exec())
    except Exception as e:
        import traceback
        log = Path(os.environ.get("TEMP", ".")) / "gitgo_crash.log"
        log.write_text(
            f"gitgo GUI crash at {datetime.now()}\n"
            f"{traceback.format_exc()}",
            encoding="utf-8",
        )
        if getattr(sys, "frozen", False):
            try:
                QMessageBox.critical(None, _tr("app.crash_title", "gitgo - 崩溃"), str(e))
            except Exception:
                pass
        raise
