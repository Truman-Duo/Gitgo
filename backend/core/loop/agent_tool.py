"""AgentTool —— 类型化的工具定义。

替代 dict[str, Callable]：每个工具包含 name / description / JSON Schema parameters /
execute / read_only / prepare_args。

Resource Lock 模型预留：resources 字段（未来替代 read_only 二值）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable


_OBJECT_SCHEMA_KEYS = {
    "$schema", "$id", "$defs", "definitions", "title", "description", "type",
    "properties", "patternProperties", "additionalProperties", "required",
    "dependentRequired", "propertyNames", "minProperties", "maxProperties",
    "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
}


def normalize_tool_parameters(value: object) -> dict:
    """Return a provider-safe top-level object schema for one tool.

    Early authored-tool builds accepted the convenient shorthand
    ``{"path": {"type": "string"}}`` and persisted it unchanged.  Providers
    require the top level itself to be a JSON-Schema object.  Keeping the
    repair here makes registration, checkpoint recovery and every provider
    transport share the same non-bypassable boundary.
    """
    if value is None:
        return {"type": "object", "properties": {}}
    if not isinstance(value, dict):
        raise ValueError("tool parameters must be a JSON Schema object")
    schema = dict(value)
    if not schema:
        return {"type": "object", "properties": {}}
    if "type" not in schema:
        if not any(key in _OBJECT_SCHEMA_KEYS for key in schema):
            # Backward-compatible authored-tool shorthand: every top-level
            # entry is a named input property's schema.
            if not all(isinstance(item, dict) for item in schema.values()):
                raise ValueError(
                    "tool parameter shorthand values must be JSON Schema objects"
                )
            schema = {"type": "object", "properties": schema}
        else:
            schema["type"] = "object"
    if schema.get("type") != "object":
        raise ValueError("tool parameters schema must have top-level type 'object'")
    properties = schema.get("properties")
    if properties is None:
        schema["properties"] = {}
    elif not isinstance(properties, dict):
        raise ValueError("tool parameters 'properties' must be an object")
    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ValueError("tool parameters 'required' must be an array of strings")
        missing = sorted(set(required) - set(schema["properties"]))
        if missing:
            raise ValueError(
                "tool parameters 'required' names missing from properties: "
                + ", ".join(missing)
            )
    return schema


class ToolEffect(str, Enum):
    READ = "read"
    WORKSPACE_WRITE = "workspace_write"
    PROCESS = "process"
    # Public, anonymous network retrieval is observably different from an
    # external mutation.  Keeping it separate prevents a weather lookup from
    # inheriting publish/send review gates merely because both cross a process
    # boundary.
    EXTERNAL_READ = "external_read"
    # ``EXTERNAL`` is retained as the serialized value used by older
    # checkpoints and authored tools.  It means an external mutation, not a
    # public read.
    EXTERNAL = "external"


class CancellationMode(str, Enum):
    COOPERATIVE = "cooperative"
    ISOLATED_PROCESS = "isolated_process"
    NON_INTERRUPTIBLE = "non_interruptible"


class ApprovalMode(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


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
    finalize_result: Callable | None = None  # child result -> refresh parent state
    resources: list[str] | None = None    # 未来：资源锁 ["filesystem:fileA", ...]
    timeout: float = 60.0                 # v0.45: 工具超时秒数（ProcessToolRunner 使用）
    timeout_argument: str = ""           # Host-declared argument, never guessed from user intent
    timeout_default: float = 60.0
    isolated: bool = False                # v0.45: True=子进程隔离执行（ProcessToolRunner）
    effect: ToolEffect | str = ToolEffect.READ
    cancellation: CancellationMode | str = CancellationMode.COOPERATIVE
    approval: ApprovalMode | str = ApprovalMode.ALLOW
    # High-risk tools such as an unrestricted shell must be approved for the
    # exact argument digest on every invocation.  They may not inherit the
    # broader task-scoped grant used by lower-risk external resources.
    approval_per_invocation: bool = False
    idempotent: bool = False
    runner_name: str = ""                 # isolated subprocess handler name
    composable: bool = False               # safe for define_tool step reuse
    # Host-owned declarative execution plan.  A composite tool is only a named
    # projection of existing AgentTools; ToolPipeline expands this plan and
    # sends every component through the same permission/receipt/signal path.
    composite_spec: dict | None = None

    def __post_init__(self) -> None:
        self.parameters = normalize_tool_parameters(self.parameters)
        # Backward-compatible normalization while ``read_only`` is migrated.
        if not self.read_only and self.effect == ToolEffect.READ:
            self.effect = ToolEffect.WORKSPACE_WRITE
        if self.isolated:
            self.cancellation = CancellationMode.ISOLATED_PROCESS
        elif (
            not self.read_only
            and self.cancellation == CancellationMode.COOPERATIVE
            and self.composite_spec is None
        ):
            # Existing bound callables cannot be force-stopped safely. Record
            # the truth until they are migrated behind ProcessToolRunner.
            self.cancellation = CancellationMode.NON_INTERRUPTIBLE
        self.effect = ToolEffect(self.effect)
        self.cancellation = CancellationMode(self.cancellation)
        self.approval = ApprovalMode(self.approval)
        if self.isolated and not self.runner_name:
            self.runner_name = self.name

    @property
    def guarantees_cancellation(self) -> bool:
        """Whether stop can terminate work that has already started."""
        return self.cancellation in (
            CancellationMode.COOPERATIVE,
            CancellationMode.ISOLATED_PROCESS,
        )

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
