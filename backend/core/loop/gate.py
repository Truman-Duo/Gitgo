"""RingGate — 每次 tool dispatch 前在进程边界上执行的权限检查。"""

from dataclasses import dataclass
from backend.core.loop.models import AgentProcess, RingLevel


@dataclass
class GateResult:
    allowed: bool
    reason: str = ""
    error: str = ""
    message: str = ""


class RingGate:
    """权限检查 gate。"""

    def check(self, process: AgentProcess, tool_name: str) -> GateResult:
        """在 tool dispatch 前检查权限。"""

        # ring 0: 全通
        if process.ring_level == RingLevel.RING_0:
            return GateResult(allowed=True, reason="ring_0_bypass")

        # ring 3: 检查 tool 是否在 registry 里
        if process.tool_registry is None:
            return GateResult(
                allowed=False, error="NO_REGISTRY",
                message="No tool registry assigned to this process.",
            )

        if not process.tool_registry.has(tool_name):
            return GateResult(
                allowed=False, error="TOOL_NOT_IN_REGISTRY",
                message=(
                    f"Tool '{tool_name}' is not in your registry. "
                    f"Your tools: {process.tool_registry.list_all()}"
                ),
            )

        # ring 3: 不能调 ring 0 工具
        if process.tool_registry.is_ring_0(tool_name):
            return GateResult(
                allowed=False, error="RING_0_REQUIRED",
                message=(
                    f"'{tool_name}' requires ring 0. "
                    f"Request escalation to parent {process.parent_id}."
                ),
            )

        return GateResult(allowed=True, reason="ring_3_allowed")
