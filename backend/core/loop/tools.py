"""ToolRegistry — per-process tool set with ring-level awareness."""


class ToolRegistry:
    """Per-process tool set。fork 时由父 Agent 显式传入。"""

    # 默认 ring 0 工具——可通过 contract.yaml policy.ring_0_tools 覆盖
    DEFAULT_RING_0_TOOLS = {"sync", "push", "accept_trial",
                             "lesson_promote", "lesson_verify",
                             "modify_contract",
                             "lesson_harvest"}

    def __init__(self, tool_names: list[str],
                 ring_0_tools: set[str] | None = None):
        self._tools = set(tool_names)
        self._ring_0 = ring_0_tools or self.DEFAULT_RING_0_TOOLS

    def has(self, tool_name: str) -> bool:
        return tool_name in self._tools

    def is_ring_0(self, tool_name: str) -> bool:
        return tool_name in self._ring_0

    def list_all(self) -> list[str]:
        return sorted(self._tools)

    def add_scoped(self, tool_name: str) -> None:
        """Add a host-validated task-scoped tool without changing profiles."""
        if not tool_name or not isinstance(tool_name, str):
            raise ValueError("tool_name is required")
        self._tools.add(tool_name)

    def remove_scoped(self, tool_name: str) -> None:
        """Remove a task-scoped tool without mutating server profiles."""
        if tool_name:
            self._tools.discard(tool_name)

    @classmethod
    def from_contract(cls, workspace_path, tool_names: list[str]
                      ) -> "ToolRegistry":
        """从 contract.yaml 读 ring_0_tools 配置。"""
        from backend.core.contract import ContractManager
        from pathlib import Path
        contract = ContractManager.load(Path(workspace_path))
        ring_0 = None
        if contract:
            raw = contract.to_dict()
            policy_cfg = raw.get("policy_checks", {})
            if isinstance(policy_cfg, dict):
                cfg_ring0 = policy_cfg.get("ring_0_tools", [])
                if cfg_ring0:
                    ring_0 = set(cfg_ring0)
        return cls(tool_names, ring_0_tools=ring_0)
