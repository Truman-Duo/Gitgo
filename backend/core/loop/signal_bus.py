"""SignalBus — HarnessPlugin 信号分发总线。

参考 PluginOrchestrator 的插拔设计，提供:
- HarnessPlugin ABC: 治理信号消费插件的基类
- SignalBus: 按 context 路由信号到订阅插件
- from_contract(): 从 contract.yaml 加载插件配置

context 类型:
- "pre_dispatch": 工具调用前 — PreDispatchGuard 在此消费
- "completion": 任务完成前 — CompletionGuard 在此消费
- "retention": 上下文裁剪时 — RetentionAdvisor 在此消费
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.loop.signals import GovernanceSignal, HarnessResult
    from backend.core.loop.models import AgentProcess


class HarnessPlugin(ABC):
    """治理信号消费插件基类。

    类似 SyncPlugin 但专门消费 GovernanceSignal。
    每个插件声明自己订阅的信号源和严重级别。

    子类必须实现:
    - name: 插件名（用于 contract.yaml 引用）
    - description: 一句话描述
    - on_signals(): 消费信号，返回 HarnessResult
    """

    name: str = ""
    description: str = ""

    # 订阅的信号源（空 = 全部）
    subscribed_sources: list[str] = []
    # 订阅的最低严重级别（空 = 全部）
    subscribed_severities: list[str] = []

    @abstractmethod
    def on_signals(
        self,
        signals: list["GovernanceSignal"],
        process: "AgentProcess",
    ) -> "HarnessResult":
        """消费治理信号，返回决策。

        Args:
            signals: 归一化后的 GovernanceSignal 列表
            process: 当前 AgentProcess

        Returns:
            HarnessResult: 决策结果（allow / block / warn / suggest）
        """
        ...

    def accepts(self, signal: "GovernanceSignal") -> bool:
        """判断此插件是否订阅了该信号。"""
        if self.subscribed_sources and signal.source not in self.subscribed_sources:
            return False
        if self.subscribed_severities and signal.severity.value not in self.subscribed_severities:
            return False
        return True


@dataclass
class DispatchResult:
    """SignalBus.dispatch() 的聚合结果。"""

    allowed: bool = True
    blocked_by: list[str] = field(default_factory=list)  # 阻断插件名
    warnings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    missing_tools: list[str] = field(default_factory=list)
    nudge_texts: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return not self.allowed


class SignalBus:
    """GovernanceSignal → HarnessPlugin 的分发总线。

    用法:
        bus = SignalBus([PreDispatchGuard(), CompletionGuard()])
        signals = normalizer.normalize(policy_results, ...)

        # 工具调前检查
        result = bus.dispatch(signals, process, context="pre_dispatch")
        if result.blocked:
            ...  # 阻止工具调用

        # 完成前检查
        result = bus.dispatch(signals, process, context="completion")
        if result.missing_tools:
            ...  # 提示缺失工具
    """

    # context → 默认插件角色映射
    CONTEXT_PLUGIN_ROLES = {
        "pre_dispatch": ["pre_dispatch_guard"],
        "completion": ["completion_guard"],
        "retention": ["retention_advisor"],
    }

    def __init__(self, plugins: list[HarnessPlugin] | None = None):
        self._plugins: dict[str, HarnessPlugin] = {}
        if plugins:
            for p in plugins:
                self.register(p)

    def register(self, plugin: HarnessPlugin) -> None:
        """注册一个 HarnessPlugin。"""
        self._plugins[plugin.name] = plugin

    def unregister(self, name: str) -> None:
        """移除一个 HarnessPlugin。"""
        self._plugins.pop(name, None)

    def dispatch(
        self,
        signals: list["GovernanceSignal"],
        process: "AgentProcess",
        context: str = "pre_dispatch",
        plugin_names: list[str] | None = None,
    ) -> DispatchResult:
        """按 context 分发信号到相关插件。

        Args:
            signals: GovernanceSignal 列表
            process: 当前 AgentProcess
            context: "pre_dispatch" | "completion" | "retention"
            plugin_names: 指定要运行的插件名（空 = 按 context 角色自动选择）

        Returns:
            DispatchResult: 聚合所有插件的结果
        """
        from backend.core.loop.signals import SignalCategory

        # 确定要运行的插件
        if plugin_names:
            active = [self._plugins[n] for n in plugin_names if n in self._plugins]
        else:
            role_names = self.CONTEXT_PLUGIN_ROLES.get(context, [])
            active = [self._plugins[n] for n in role_names if n in self._plugins]

        if not active:
            return DispatchResult()

        result = DispatchResult()

        for plugin in active:
            # 过滤此插件订阅的信号
            relevant = [s for s in signals if plugin.accepts(s)]
            if not relevant:
                continue

            plugin_result = plugin.on_signals(relevant, process)

            if plugin_result.blocked:
                result.allowed = False
                result.blocked_by.append(plugin.name)

            result.warnings.extend(plugin_result.warnings)
            result.suggestions.extend(plugin_result.suggestions)
            result.missing_tools.extend(plugin_result.missing_tools)
            if plugin_result.nudge_text:
                result.nudge_texts.append(plugin_result.nudge_text)

        return result

    def check_tool(
        self,
        tool_name: str,
        args: dict,
        process: "AgentProcess",
        signals: list["GovernanceSignal"] | None = None,
    ) -> dict:
        """Per-tool pre-dispatch 检查。迭代所有带 check_tool 的已注册插件。

        首个返回 blocked 的插件结果即为最终结果。
        无插件实现 check_tool 时默认放行。
        """
        for plugin in self._plugins.values():
            if hasattr(plugin, "check_tool"):
                result = plugin.check_tool(tool_name, args, process, signals=signals)
                if not result.get("allowed", True):
                    return result
        return {"allowed": True}

    @classmethod
    def from_contract(cls, workspace_path) -> "SignalBus":
        """从 contract.yaml 加载 HarnessPlugin 配置。

        contract.yaml 格式:
            harness:
              plugins:
                enabled: ["pre_dispatch_guard", "completion_guard"]
                disabled: ["retention_advisor"]
                config:
                  pre_dispatch_guard:
                    severity_threshold: "high"
              user_plugins:
                - "plugins/my_guard.py"

        无 harness 段时 fallback 到全部内置插件。
        """
        from pathlib import Path
        from backend.core.loop.harness.registry import get_plugin_registry, load_user_plugin

        bus = cls()

        # 加载 contract.yaml
        contract_path = Path(workspace_path) / ".gitgo" / "contract.yaml"
        harness_cfg: dict = {}
        try:
            import yaml
            raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
            harness_cfg = (raw or {}).get("harness", {})
        except Exception:
            pass

        plugins_cfg = harness_cfg.get("plugins", {})
        enabled = plugins_cfg.get("enabled", [])
        disabled = plugins_cfg.get("disabled", [])
        plugin_configs = plugins_cfg.get("config", {})

        # Fallback: 无 harness 段 → 启用全部内置插件
        if not enabled and not disabled:
            enabled = ["pre_dispatch_guard", "completion_guard", "retention_advisor"]

        registry = get_plugin_registry()
        for name, plugin_cls in registry.items():
            if disabled and name in disabled:
                continue
            if enabled and name not in enabled:
                continue
            instance = plugin_cls()
            if name in plugin_configs and hasattr(instance, 'configure'):
                instance.configure(plugin_configs[name])
            bus.register(instance)

        # 加载用户自定义插件
        for user_path in harness_cfg.get("user_plugins", []):
            resolved = _resolve_plugin_path(workspace_path, user_path)
            user_instance = load_user_plugin(resolved)
            if user_instance:
                bus.register(user_instance)

        return bus


def _resolve_plugin_path(workspace_path, user_path: str) -> str:
    """解析用户插件路径。

    支持:
    - 绝对路径: /home/user/plugins/foo.py
    - ~ 展开: ~/.gitgo/plugins/foo.py
    - 相对路径: plugins/foo.py (相对于 workspace_path)
    """
    from pathlib import Path
    p = Path(user_path)
    if p.is_absolute():
        return str(p)
    if str(p).startswith("~"):
        return str(p.expanduser())
    return str(Path(workspace_path) / p)
