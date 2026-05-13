"""插件发现、加载、编排器。

搜索路径（按优先级）：
  1. ``{exe_dir}/plugins/`` — 随程序分发的插件
  2. ``~/.vernier/plugins/`` — 用户全局插件
  3. ``{workspace}/.gitgo/plugins/`` — 项目级本地插件

用法示例::

    from plugin_loader import PluginOrchestrator

    orch = PluginOrchestrator()
    orch.discover()
    enabled = orch.get_enabled_plugins(["auto-merge", "commit-msg-gen"])
    for plugin in enabled:
        ...
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from backend.core.plugin import SyncPlugin

logger = logging.getLogger("gitgo.plugin_loader")


# ── 数据结构 ─────────────────────────────────────────────────


@dataclass
class PluginInfo:
    """已发现但尚未加载的插件元信息"""
    id: str
    filepath: Path
    module_name: str = ""

    @property
    def is_package(self) -> bool:
        return self.filepath.is_dir()


# ── 发现 ─────────────────────────────────────────────────────


def _default_search_paths() -> list[Path]:
    """返回三个默认搜索路径（仅存在的目录）。"""
    paths: list[Path] = []

    # 1. 内置插件（exe 打包的 plugins/ 目录或脚本同目录）
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            p1 = Path(meipass) / "plugins"
            if p1.exists():
                paths.append(p1)
    else:
        p1 = Path(__file__).parent / "plugins"
        if p1.exists():
            paths.append(p1)

    # 2. 用户全局 ~/.vernier/plugins/
    p2 = Path.home() / ".vernier" / "plugins"
    if p2.exists():
        paths.append(p2)

    return paths


def _scan_plugin_dir(plugin_dir: Path) -> list[PluginInfo]:
    """扫描单个目录下的所有插件文件/包。"""
    infos: list[PluginInfo] = []
    if not plugin_dir.is_dir():
        return infos

    for child in sorted(plugin_dir.iterdir()):
        # 单文件插件: xxx.py（排除 __init__）
        if child.is_file() and child.suffix == ".py" and child.stem != "__init__":
            plugin_id = child.stem.replace("_", "-")
            infos.append(PluginInfo(id=plugin_id, filepath=child))
        # 包插件: xxx/__init__.py
        elif child.is_dir() and (child / "__init__.py").exists():
            plugin_id = child.name.replace("_", "-")
            infos.append(PluginInfo(id=plugin_id, filepath=child))

    return infos


def discover(search_paths: list[Path] | None = None) -> dict[str, PluginInfo]:
    """扫描所有搜索路径，返回 {plugin_id: PluginInfo}。

    重复 ID 按路径优先级覆盖（先扫描的路径优先级高）。
    """
    if search_paths is None:
        search_paths = _default_search_paths()

    registry: dict[str, PluginInfo] = {}
    for sp in search_paths:
        for info in _scan_plugin_dir(sp):
            registry[info.id] = info  # 后扫描的不会覆盖已存在的
    return registry


# ── 加载 ─────────────────────────────────────────────────────


def _load_plugin(info: PluginInfo) -> SyncPlugin | None:
    """加载单个插件，返回插件实例。失败返回 None。"""
    try:
        if info.is_package:
            spec = importlib.util.spec_from_file_location(
                f"gitgo_plugin_{info.id}",
                info.filepath / "__init__.py",
                submodule_search_locations=[str(info.filepath)],
            )
        else:
            spec = importlib.util.spec_from_file_location(
                f"gitgo_plugin_{info.id}",
                str(info.filepath),
            )

        if spec is None or spec.loader is None:
            logger.warning("无法加载插件 %s: spec 为空", info.id)
            return None

        # 使用独立模块加载，不影响主程序命名空间
        mod = importlib.util.module_from_spec(spec)

        # 先注入 __file__ / __package__ 等属性
        if info.is_package:
            mod.__package__ = f"gitgo_plugin_{info.id}"
            mod.__path__ = [str(info.filepath)]
        else:
            mod.__package__ = ""

        mod.__file__ = str(info.filepath)

        # 执行模块
        spec.loader.exec_module(mod)

        # 查找 plugin_class
        plugin_class = getattr(mod, "plugin_class", None)
        if plugin_class is None:
            logger.warning("插件 %s 缺少 plugin_class 变量", info.id)
            return None

        if not isinstance(plugin_class, type) or not issubclass(plugin_class, SyncPlugin):
            logger.warning("插件 %s 的 plugin_class 不是 SyncPlugin 子类", info.id)
            return None

        instance: SyncPlugin = plugin_class()
        if not instance.name:
            instance.name = info.id
        return instance

    except Exception:
        logger.warning("加载插件 %s 失败", info.id, exc_info=True)
        return None


# ── 编排器 ───────────────────────────────────────────────────


class PluginOrchestrator:
    """插件编排器 — 管理插件的发现、加载和钩子分发。"""

    def __init__(self) -> None:
        self._discovered: dict[str, PluginInfo] = {}  # id → info
        self._instances: dict[str, SyncPlugin] = {}  # id → instance
        self._search_paths: list[Path] = list(_default_search_paths())

    def add_search_path(self, path: str | Path) -> None:
        """添加额外搜索路径（如项目级 .gitgo/plugins/）。"""
        p = Path(path).resolve()
        if p.is_dir() and p not in self._search_paths:
            self._search_paths.append(p)

    def discover(self) -> dict[str, PluginInfo]:
        """（重新）扫描所有搜索路径。"""
        self._discovered = discover(self._search_paths)
        return self._discovered

    def get_enabled_instances(
        self, plugin_ids: list[str]
    ) -> list[SyncPlugin]:
        """根据项目配置的 ``plugin_ids`` 返回已加载的插件实例列表。"""
        result: list[SyncPlugin] = []
        for pid in plugin_ids:
            if pid in self._instances:
                result.append(self._instances[pid])
                continue

            info = self._discovered.get(pid)
            if info is None:
                logger.info("插件 %s 未找到，跳过", pid)
                continue

            instance = _load_plugin(info)
            if instance is not None:
                self._instances[pid] = instance
                result.append(instance)
            else:
                logger.warning("插件 %s 加载失败", pid)

        return result

    # ── 钩子分发 ─────────────────────────────────────────

    def on_scan_complete(
        self, plugin_ids: list[str], entries: list[dict]
    ) -> list[dict]:
        """链式传递：插件A的输出 → 插件B的输入。"""
        result = entries
        for p in self.get_enabled_instances(plugin_ids):
            try:
                ret = p.on_scan_complete(result)
                if ret is not None:
                    result = ret if ret else entries  # 空列表 = 回退原始
            except Exception:
                logger.warning("插件 %s on_scan_complete 异常", p.name, exc_info=True)
        return result

    def on_commit_select(
        self, plugin_ids: list[str], commits: list[dict]
    ) -> set[int]:
        """合并去重：所有插件的推荐索引合并为 set。"""
        indexes: set[int] = set()
        for p in self.get_enabled_instances(plugin_ids):
            try:
                ret = p.on_commit_select(commits)
                if ret:
                    indexes.update(ret)
            except Exception:
                logger.warning("插件 %s on_commit_select 异常", p.name, exc_info=True)
        return indexes

    def on_commit_message(
        self,
        plugin_ids: list[str],
        selected: list[dict],
        project_config: dict,
    ) -> str | None:
        """优先级取第一个非 None 返回值。"""
        for p in self.get_enabled_instances(plugin_ids):
            try:
                ret = p.on_commit_message(selected, project_config)
                if ret is not None:
                    return ret
            except Exception:
                logger.warning("插件 %s on_commit_message 异常", p.name, exc_info=True)
        return None

    def on_sync_start(
        self, plugin_ids: list[str], entries: list[dict], message: str
    ) -> str | None:
        """全部放行才放行，任一返回非空则中断。"""
        for p in self.get_enabled_instances(plugin_ids):
            try:
                ret = p.on_sync_start(entries, message)
                if ret:
                    return ret
            except Exception:
                logger.warning("插件 %s on_sync_start 异常", p.name, exc_info=True)
        return None

    def on_sync_complete(
        self, plugin_ids: list[str], result: dict
    ) -> None:
        """全部执行，不聚合。"""
        for p in self.get_enabled_instances(plugin_ids):
            try:
                p.on_sync_complete(result)
            except Exception:
                logger.warning("插件 %s on_sync_complete 异常", p.name, exc_info=True)

    def on_push_start(
        self, plugin_ids: list[str]
    ) -> str | None:
        """全部放行才放行。"""
        for p in self.get_enabled_instances(plugin_ids):
            try:
                ret = p.on_push_start()
                if ret:
                    return ret
            except Exception:
                logger.warning("插件 %s on_push_start 异常", p.name, exc_info=True)
        return None

    def on_push_complete(
        self, plugin_ids: list[str], result: dict
    ) -> None:
        """全部执行，不聚合。"""
        for p in self.get_enabled_instances(plugin_ids):
            try:
                p.on_push_complete(result)
            except Exception:
                logger.warning("插件 %s on_push_complete 异常", p.name, exc_info=True)


# ── 全局单例 ────────────────────────────────────────────────


_orch: PluginOrchestrator | None = None


def get_orchestrator() -> PluginOrchestrator:
    """获取全局共享的 PluginOrchestrator 单例。"""
    global _orch
    if _orch is None:
        _orch = PluginOrchestrator()
        _orch.discover()
    return _orch
