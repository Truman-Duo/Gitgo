"""国际化模块 — 语言文件加载与翻译函数"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

_locale_data: dict[str, str] = {}
_current_lang: str = "zh"


def _default_locale_dir() -> Path:
    """返回 locales 目录路径（兼容 PyInstaller 冻结模式）"""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).parent
    return base / "locales"


def load_language(lang: str, locale_dir: Optional[Path] = None) -> None:
    """加载指定语言的翻译文件"""
    global _locale_data, _current_lang
    _current_lang = lang

    if locale_dir is None:
        locale_dir = _default_locale_dir()

    filepath = locale_dir / f"{lang}.json"
    if filepath.exists():
        with open(filepath, "r", encoding="utf-8") as f:
            _locale_data = json.load(f)
    else:
        _locale_data = {}


def _tr(key: str, default: Optional[str] = None) -> str:
    """翻译：key → 当前语言的文字。找不到时返回 default 或 key 本身。"""
    if key in _locale_data:
        return _locale_data[key]
    if default is not None:
        return default
    return key


def current_lang() -> str:
    return _current_lang


def available_languages(locale_dir: Optional[Path] = None) -> list[tuple[str, str]]:
    """返回可用语言列表 [(代码, 显示名), ...]"""
    if locale_dir is None:
        locale_dir = _default_locale_dir()
    meta = {
        "zh": "中文",
        "en": "English",
    }
    result: list[tuple[str, str]] = []
    if locale_dir.exists():
        for f in sorted(locale_dir.glob("*.json")):
            code = f.stem
            result.append((code, meta.get(code, code)))
    return result
