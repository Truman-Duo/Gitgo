"""Harness Plugin Package — SignalBus 可插拔治理插件。

每个 HarnessPlugin 子类消费 GovernanceSignal，返回 HarnessResult。
通过 contract.yaml 的 harness.plugins 段启用/禁用。

内置插件:
- PreDispatchGuard: 工具调用前检查（危险工具前置条件 + contract drift 文件保护）
- CompletionGuard: 任务完成前验证（必要工具 + rejection 指令处理）
- RetentionAdvisor: 上下文裁剪时保留高优先级治理信息
"""

from backend.core.loop.harness.pre_dispatch import PreDispatchGuard
from backend.core.loop.harness.completion import CompletionGuard
from backend.core.loop.harness.retention import RetentionAdvisor

__all__ = ["PreDispatchGuard", "CompletionGuard", "RetentionAdvisor"]
