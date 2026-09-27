"""Settings dialog"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout)
from backend.core.i18n import _tr, available_languages
from themes import get_theme

class SettingsDialog(QDialog):
    """设置对话框 — 主题/语言/动画/版本"""

    theme_changed = Signal(str)  # "light" | "dark" | "system"

    def __init__(self, parent=None, current_theme: str = "system", animation_enabled: bool = True):
        super().__init__(parent)
        self.setWindowTitle(_tr("settings.title", "设置"))
        self.setMinimumSize(450, 400)

        layout = QVBoxLayout(self)

        # 标题
        title = QLabel(_tr("settings.title", "设置"))
        title.setStyleSheet("font-size: 16px; font-weight: bold; margin-bottom: 8px;")
        layout.addWidget(title)

        # 主题设置
        theme_group = QGroupBox(_tr("settings.appearance", "外观"))
        theme_layout = QFormLayout(theme_group)
        theme_layout.setLabelAlignment(Qt.AlignRight)

        self.theme_combo = QComboBox()
        self.theme_combo.addItem(_tr("settings.system", "跟随系统"), "system")
        self.theme_combo.addItem(_tr("settings.light", "浅色模式"), "light")
        self.theme_combo.addItem(_tr("settings.dark", "深色模式"), "dark")

        theme_map = {"system": 0, "light": 1, "dark": 2}
        self.theme_combo.setCurrentIndex(theme_map.get(current_theme, 0))
        theme_layout.addRow(_tr("settings.theme", "UI 主题："), self.theme_combo)

        layout.addWidget(theme_group)

        # 语言设置
        lang_group = QGroupBox(_tr("settings.language_group", "语言"))
        lang_layout = QFormLayout(lang_group)
        lang_layout.setLabelAlignment(Qt.AlignRight)
        self.lang_combo = QComboBox()
        current_lang = parent.config.language if parent and hasattr(parent, 'config') else 'zh'
        for code, display_name in available_languages():
            self.lang_combo.addItem(display_name, code)
        self.lang_combo.setCurrentIndex(self.lang_combo.findData(current_lang))
        lang_layout.addRow(_tr("settings.language", "界面语言："), self.lang_combo)
        layout.addWidget(lang_group)

        # 动画设置
        anim_group = QGroupBox(_tr("settings.animation", "动画"))
        anim_layout = QVBoxLayout(anim_group)
        self.anim_check = QCheckBox(_tr("settings.animation_enable", "启用界面切换动画"))
        self.anim_check.setChecked(animation_enabled)
        anim_layout.addWidget(self.anim_check)
        anim_hint = QLabel(_tr("settings.animation_hint", "页面切换时播放淡入淡出效果"))
        t = get_theme()
        anim_hint.setStyleSheet(f"color: {t.txt3}; font-size: 10px;")
        anim_layout.addWidget(anim_hint)
        layout.addWidget(anim_group)

        # 版本信息
        ver_group = QGroupBox(_tr("settings.version", "版本"))
        ver_layout = QVBoxLayout(ver_group)
        ver_label = QLabel(f"gitgo {_tr('app.version', 'v0.4')}")
        ver_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        ver_layout.addWidget(ver_label)
        ver_detail = QLabel(
            "Phase 1-4: RepoNode · Adapter · SSH · Trial\n"
            "PySide6 · Rich · i18n · Plugin"
        )
        t = get_theme()
        ver_detail.setStyleSheet(f"color: {t.txt3}; font-size: 10px;")
        ver_layout.addWidget(ver_detail)
        layout.addWidget(ver_group)

        layout.addStretch()

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        ok_btn = QPushButton(_tr("settings.ok", "确认"))
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton(_tr("settings.cancel", "取消"))
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    @property
    def selected_theme(self) -> str:
        return self.theme_combo.currentData()

    @property
    def selected_language(self) -> str:
        return self.lang_combo.currentData()

    @property
    def animation_enabled(self) -> bool:
        return self.anim_check.isChecked()


def _detect_system_theme() -> str:
    """检测 Windows 系统主题，返回 'dark' 或 'light'。"""
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

