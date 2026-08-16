"""AgentTool —— 类型化的工具定义。

替代 dict[str, Callable]：每个工具包含 name / description / JSON Schema parameters /
execute / read_only / prepare_args。

Resource Lock 模型预留：resources 字段（未来替代 read_only 二值）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class AgentTool:
    """类型化工具定义。

    基于 pi-agent 的 AgentTool 层（name + description + parameters + execute +
    executionMode），结合 Kimi Code 的 Pydantic 泛型验证和 Reasonix 的 ReadOnly
    分区策略。去掉 pi-agent 的 ToolDefinition 层（gitgo 的 Dashboard 是独立
    进程自己渲染 UI）。

    resources 字段为未来 Resource Lock 模型预留——不传时 fallback 到 read_only。

    可调用：`tool(args)` 直接委托给 `tool.execute(args)`，兼容旧 ToolDispatcher。
    """

    name: str
    description: str                  # 给 LLM 看的一句话描述（含"何时使用"）
    parameters: dict                  # JSON Schema (properties + required)
    execute: Callable                 # 实际执行函数 (args: dict) -> dict
    read_only: bool = True            # True=可并行, False=必须串行
    prepare_args: Callable | None = None  # 可选：参数预处理（兼容不同 LLM）
    resources: list[str] | None = None    # 未来：资源锁 ["filesystem:fileA", ...]
    timeout: float = 60.0                 # v0.45: 工具超时秒数（ProcessToolRunner 使用）
    isolated: bool = False                # v0.45: True=子进程隔离执行（ProcessToolRunner）

    def __call__(self, args: dict) -> dict:
        """直接调用 AgentTool 实例 = 执行工具。兼容旧 dict[str, Callable] 接口。"""
        return self.execute(args)

    def to_openai_function(self) -> dict:
        """转换为 OpenAI function calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def canonicalize_args(self, args: dict) -> str:
        """规范化参数为字符串（用于去重/重复检测的 hash key）。"""
        import json
        return json.dumps(args, sort_keys=True, ensure_ascii=False)
