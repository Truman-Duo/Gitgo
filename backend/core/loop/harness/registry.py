"""HarnessPlugin 注册表 — 内置插件发现 + 用户自定义插件加载。

类似 PluginOrchestrator 的发现机制，但针对 HarnessPlugin 体系。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.loop.signal_bus import HarnessPlugin

logger = logging.getLogger("gitgo.harness.registry")

# 内置插件注册表（name → class）
_BUILTIN: dict[str, type["HarnessPlugin"]] = {}


def _discover_builtins() -> dict[str, type["HarnessPlugin"]]:
    """扫描 harness/ 目录下的内置 HarnessPlugin 子类。"""
    from backend.core.loop.signal_bus import HarnessPlugin
    from backend.core.loop.harness.pre_dispatch import PreDispatchGuard
    from backend.core.loop.harness.completion import CompletionGuard
    from backend.core.loop.harness.retention import RetentionAdvisor

    builtins: dict[str, type[HarnessPlugin]] = {}
    for cls in [PreDispatchGuard, CompletionGuard, RetentionAdvisor]:
        if hasattr(cls, "name") and cls.name:
            builtins[cls.name] = cls
    return builtins


def get_plugin_registry() -> dict[str, type["HarnessPlugin"]]:
    """获取所有已注册的 HarnessPlugin（内置 + 用户自定义）。

    优先从缓存返回；首次调用时扫描内置插件。
    """
    global _BUILTIN
    if not _BUILTIN:
        _BUILTIN = _discover_builtins()
    return dict(_BUILTIN)


def register_plugin(cls: type["HarnessPlugin"]) -> None:
    """注册一个外部 HarnessPlugin 类。"""
    global _BUILTIN
    if not _BUILTIN:
        _BUILTIN = _discover_builtins()
    name = getattr(cls, "name", "")
    if name:
        _BUILTIN[name] = cls
        logger.info("注册 HarnessPlugin: %s", name)


def load_user_plugin(plugin_path: str | Path) -> "HarnessPlugin | None":
    """从文件路径加载用户自定义 HarnessPlugin。

    Args:
        plugin_path: .py 文件路径或包目录路径

    Returns:
        HarnessPlugin 实例，加载失败返回 None
    """
    from backend.core.loop.signal_bus import HarnessPlugin

    p = Path(plugin_path).resolve()
    if not p.exists():
        logger.warning("插件路径不存在: %s", p)
        return None

    try:
        if p.is_dir():
            spec = importlib.util.spec_from_file_location(
                f"gitgo_harness_{p.name}",
                p / "__init__.py",
                submodule_search_locations=[str(p)],
            )
        else:
            spec = importlib.util.spec_from_file_location(
                f"gitgo_harness_{p.stem}",
                str(p),
            )

        if spec is None or spec.loader is None:
            logger.warning("无法加载插件 %s: spec 为空", p)
            return None

        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        plugin_class = getattr(mod, "plugin_class", None)
        if plugin_class is None:
            logger.warning("插件 %s 缺少 plugin_class 变量", p)
            return None

        if not isinstance(plugin_class, type) or not issubclass(plugin_class, HarnessPlugin):
            logger.warning("插件 %s 的 plugin_class 不是 HarnessPlugin 子类", p)
            return None

        instance: HarnessPlugin = plugin_class()
        return instance

    except Exception:
        logger.warning("加载插件 %s 失败", p, exc_info=True)
        return None
